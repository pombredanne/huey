import datetime
import os
import pickle
import uuid
import sys
import time

from huey.exceptions import QueueWriteException, QueueReadException, \
    DataStoreGetException, DataStorePutException, DataStoreTimeout,\
    QueueException
from huey.registry import registry
from huey.utils import wrap_exception, EmptyData


class AsyncData(object):
    def __init__(self, invoker, command):
        self.invoker = invoker
        self.command = command

        self._result = EmptyData

    def _get(self):
        task_id = self.command.task_id
        if self._result is EmptyData:
            res = self.invoker._get(task_id)

            if res is not EmptyData:
                self._result = pickle.loads(res)
                return self._result
            else:
                return res
        else:
            return self._result

    def get(self, blocking=False, timeout=None, backoff=1.15, max_delay=1.0, revoke_on_timeout=False):
        if not blocking:
            res = self._get()
            if res is not EmptyData:
                return res
        else:
            start = time.time()
            delay = .1
            while self._result is EmptyData:
                if timeout and time.time() - start >= timeout:
                    if revoke_on_timeout:
                        self.revoke()
                    raise DataStoreTimeout
                if delay > max_delay:
                    delay = max_delay
                if self._get() is EmptyData:
                    time.sleep(delay)
                    delay *= backoff

            return self._result

    def revoke(self):
        self.invoker.revoke(self.command)

    def restore(self):
        self.invoker.restore(self.command)


class Invoker(object):
    """
    The :class:`Invoker` is responsible for reading and writing to the queue
    and executing messages.  It talks to the :class:`CommandRegistry` to load
    up the proper :class:`QueueCommand` for each message
    """

    def __init__(self, queue, result_store=None, task_store=None, store_none=False,
                 always_eager=False):
        self.queue = queue
        self.result_store = result_store
        self.task_store = task_store
        self.blocking = self.queue.blocking
        self.store_none = store_none
        self.always_eager = always_eager

    def _write(self, msg):
        try:
            self.queue.write(msg)
        except:
            wrap_exception(QueueWriteException)

    def _read(self):
        try:
            return self.queue.read()
        except:
            wrap_exception(QueueReadException)

    def _remove(self, msg):
        try:
            return self.queue.remove(msg)
        except:
            wrap_exception(QueueRemoveException)

    def _get(self, key, peek=False):
        try:
            if peek:
                return self.result_store.peek(key)
            else:
                return self.result_store.get(key)
        except:
            return wrap_exception(DataStoreGetException)

    def _put(self, key, value):
        try:
            return self.result_store.put(key, value)
        except:
            return wrap_exception(DataStorePutException)

    def enqueue(self, command):
        if self.always_eager:
            return command.execute()

        self._write(registry.get_message_for_command(command))

        if self.result_store:
            return AsyncData(self, command)

    def dequeue(self):
        message = self._read()
        if message:
            return registry.get_command_for_message(message)

    def execute(self, command):
        if not isinstance(command, QueueCommand):
            raise TypeError('Unknown object: %s' % command)

        result = command.execute()

        if result is None and not self.store_none:
            return

        if self.result_store and not isinstance(command, PeriodicQueueCommand):
            self._put(command.task_id, pickle.dumps(result))

        return result

    def revoke(self, command, revoke_until=None, revoke_once=False):
        if not self.task_store:
            raise QueueException('A DataStore is required to revoke commands')

        serialized = pickle.dumps((revoke_until, revoke_once))
        self._put(command.revoke_id, serialized)
        #self._remove(registry.get_message_for_command(command))

    def restore(self, command):
        self._get(command.revoke_id) # simply get and delete if there

    def is_revoked(self, command, dt=None, preserve=True):
        if not self.result_store:
            return False
        res = self._get(command.revoke_id, peek=True)
        if res is EmptyData:
            return False
        revoke_until, revoke_once = pickle.loads(res)
        if revoke_once:
            if not preserve:
                self.restore(command)
            return True
        return revoke_until is None or revoke_until > dt

    def flush(self):
        self.queue.flush()


class CommandSchedule(object):
    def __init__(self, invoker, key_name='schedule'):
        self.invoker = invoker
        self.key_name = key_name

        self.task_store = self.invoker.task_store
        self._schedule = {}

    def __contains__(self, task_id):
        return task_id in self._schedule

    def load(self):
        if self.task_store:
            serialized = self.task_store.get(self.key_name)

            if serialized and serialized is not EmptyData:
                self.load_commands(pickle.loads(serialized))

    def load_commands(self, messages):
        for cmd_string in messages:
            try:
                cmd_obj = registry.get_command_for_message(cmd_string)
                self.add(cmd_obj)
            except QueueException:
                pass

    def save(self):
        if self.task_store:
            self.task_store.put(self.key_name, self.serialize_commands())

    def serialize_commands(self):
        messages = [registry.get_message_for_command(c) for c in self.commands()]
        return pickle.dumps(messages)

    def commands(self):
        return self._schedule.values()

    def should_run(self, cmd, dt=None):
        dt = dt or datetime.datetime.now()
        return cmd.execute_time is None or cmd.execute_time <= dt

    def can_run(self, cmd, dt=None):
        return not self.invoker.is_revoked(cmd, dt, False)

    def add(self, cmd):
        if not self.is_pending(cmd):
            self._schedule[cmd.task_id] = cmd

    def remove(self, cmd):
        if self.is_pending(cmd):
            del(self._schedule[cmd.task_id])

    def is_pending(self, cmd):
        return cmd.task_id in self._schedule


class QueueCommandMetaClass(type):
    def __init__(cls, name, bases, attrs):
        """
        Metaclass to ensure that all command classes are registered
        """
        registry.register(cls)


class QueueCommand(object):
    """
    A class that encapsulates the logic necessary to 'do something' given some
    arbitrary data.  When enqueued with the :class:`Invoker`, it will be
    stored in a queue for out-of-band execution via the consumer.  See also
    the :func:`queue_command` decorator, which can be used to automatically
    execute any function out-of-band.

    Example::

    class SendEmailCommand(QueueCommand):
        def execute(self):
            data = self.get_data()
            send_email(data['recipient'], data['subject'], data['body'])

    invoker.enqueue(
        SendEmailCommand({
            'recipient': 'somebody@spam.com',
            'subject': 'look at this awesome website',
            'body': 'http://youtube.com'
        })
    )
    """

    __metaclass__ = QueueCommandMetaClass

    def __init__(self, data=None, task_id=None, execute_time=None, retries=0, retry_delay=0):
        """
        Initialize the command object with a receiver and optional data.  The
        receiver object *must* be a django model instance.
        """
        self.set_data(data)
        self.task_id = task_id or self.create_id()
        self.revoke_id = 'r:%s' % self.task_id
        self.execute_time = execute_time
        self.retries = retries
        self.retry_delay = retry_delay

    def create_id(self):
        return str(uuid.uuid4())

    def get_data(self):
        """Called by the Invoker when a command is being enqueued"""
        return self.data

    def set_data(self, data):
        """Called by the Invoker when a command is dequeued"""
        self.data = data

    def execute(self):
        """Execute any arbitary code here"""
        raise NotImplementedError

    def __eq__(self, rhs):
        return \
            self.task_id == rhs.task_id and \
            self.execute_time == rhs.execute_time and \
            type(self) == type(rhs)


class PeriodicQueueCommand(QueueCommand):
    def create_id(self):
        return registry.command_to_string(type(self))

    def validate_datetime(self, dt):
        """Validate that the command should execute at the given datetime"""
        return False
