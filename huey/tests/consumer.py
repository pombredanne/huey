from collections import deque
import datetime
import json
import logging
import threading
import time
import unittest

from huey import crontab
from huey import Huey
from huey.backends.dummy import DummyDataStore
from huey.backends.dummy import DummyEventEmitter
from huey.backends.dummy import DummyQueue
from huey.backends.dummy import DummySchedule
from huey.bin.huey_consumer import Consumer
from huey.bin.huey_consumer import WorkerThread
from huey.registry import registry

# Logger used by the consumer.
logger = logging.getLogger('huey.consumer')

# Store some global state.
state = {}

# Create a queue, result store, schedule and event emitter, then attach them
# to a test-only Huey instance.
test_queue = DummyQueue('test-queue')
test_result_store = DummyDataStore('test-queue')
test_schedule = DummySchedule('test-queue')
test_events = DummyEventEmitter('test-queue')
test_huey = Huey(test_queue, test_result_store, test_schedule, test_events)

# Create some test tasks.
@test_huey.task()
def modify_state(k, v):
    state[k] = v
    return v

@test_huey.task()
def blow_up():
    raise Exception('blowed up')

@test_huey.task(retries=3)
def retry_command(k, always_fail=True):
    if k not in state:
        if not always_fail:
            state[k] = 'fixed'
        raise Exception('fappsk')
    return state[k]

@test_huey.task(retries=3, retry_delay=10)
def retry_command_slow(k, always_fail=True):
    if k not in state:
        if not always_fail:
            state[k] = 'fixed'
        raise Exception('fappsk')
    return state[k]

@test_huey.periodic_task(crontab(minute='0'))
def every_hour():
    state['p'] = 'y'


# Create a log handler that will track messages generated by the consumer.
class TestLogHandler(logging.Handler):
    def __init__(self, *args, **kwargs):
        self.messages = []
        logging.Handler.__init__(self, *args, **kwargs)

    def emit(self, record):
        self.messages.append(record.getMessage())


class ConsumerTestCase(unittest.TestCase):
    def setUp(self):
        global state
        state = {}

        self.orig_pc = registry._periodic_tasks
        registry._periodic_commands = [every_hour.task_class()]

        self.orig_sleep = time.sleep
        time.sleep = lambda x: None

        test_huey.queue.flush()
        test_huey.result_store.flush()
        test_huey.schedule.flush()
        test_events._events = deque()

        self.consumer = Consumer(test_huey, workers=2)
        self.consumer.create_threads()

        self.handler = TestLogHandler()
        logger.addHandler(self.handler)

    def tearDown(self):
        self.consumer.shutdown()
        logger.removeHandler(self.handler)
        registry._periodic_tasks = self.orig_pc
        time.sleep = self.orig_sleep

    def assertStatusTask(self, status_task):
        parsed = []
        i = 0
        while i < len(status_task):
            event = json.loads(test_events._events[i])
            status, task, extra = status_task[i]
            self.assertEqual(event['status'], status)
            self.assertEqual(event['id'], task.task_id)
            for k, v in extra.items():
                self.assertEqual(event[k], v)
            i += 1

    def spawn(self, func, *args, **kwargs):
        t = threading.Thread(target=func, args=args, kwargs=kwargs)
        t.start()
        return t

    def run_worker(self, task, ts=None):
        worker_t = WorkerThread(
            test_huey,
            self.consumer.default_delay,
            self.consumer.max_delay,
            self.consumer.backoff,
            self.consumer.utc,
            self.consumer._shutdown)
        ts = ts or datetime.datetime.utcnow()
        worker_t.handle_task(task, ts)

    def test_message_processing(self):
        self.consumer.worker_threads[0].start()

        self.assertFalse('k' in state)

        res = modify_state('k', 'v')
        res.get(blocking=True)

        self.assertTrue('k' in state)
        self.assertEqual(res.get(), 'v')

        self.assertEqual(len(test_events._events), 2)
        self.assertStatusTask([
            ('finished', res.task, {}),
            ('started', res.task, {}),
        ])

    def test_worker(self):
        modify_state('k', 'w')
        task = test_huey.dequeue()
        self.run_worker(task)
        self.assertEqual(state, {'k': 'w'})

    def test_worker_exception(self):
        blow_up()
        task = test_huey.dequeue()

        self.run_worker(task)
        self.assertTrue(
            'Unhandled exception in worker thread' in self.handler.messages)

        self.assertEqual(len(test_events._events), 2)
        self.assertStatusTask([
            ('error', task, {'error': True}),
            ('started', task, {}),
        ])

    def test_retries_and_logging(self):
        # this will continually fail
        retry_command('blampf')

        for i in reversed(range(4)):
            task = test_huey.dequeue()
            self.assertEqual(task.retries, i)
            self.run_worker(task)
            if i > 0:
                self.assertEqual(
                    self.handler.messages[-1],
                    'Re-enqueueing task %s, %s tries left' % (
                        task.task_id, i - 1))
                self.assertStatusTask([
                    ('enqueued', task, {}),
                    ('retrying', task, {}),
                    ('error', task,{}),
                    ('started', task, {}),
                ])
                last_idx = -2
            else:
                self.assertStatusTask([
                    ('error', task,{}),
                    ('started', task, {}),
                ])
                last_idx = -1
            self.assertEqual(self.handler.messages[last_idx],
                             'Unhandled exception in worker thread')

        self.assertEqual(test_huey.dequeue(), None)

    def test_retries_with_success(self):
        # this will fail once, then succeed
        retry_command('blampf', False)
        self.assertFalse('blampf' in state)

        task = test_huey.dequeue()
        self.run_worker(task)
        self.assertEqual(self.handler.messages, [
            'Executing %s' % task,
            'Unhandled exception in worker thread',
            'Re-enqueueing task %s, 2 tries left' % task.task_id])

        task = test_huey.dequeue()
        self.assertEqual(task.retries, 2)
        self.run_worker(task)

        self.assertEqual(state['blampf'], 'fixed')
        self.assertEqual(test_huey.dequeue(), None)

        self.assertStatusTask([
            ('finished', task, {}),
            ('started', task, {}),
            ('enqueued', task, {'retries': 2}),
            ('retrying', task, {'retries': 3}),
            ('error', task, {'error': True}),
            ('started', task, {}),
        ])

    def test_scheduling(self):
        dt = datetime.datetime(2011, 1, 1, 0, 0)
        dt2 = datetime.datetime(2037, 1, 1, 0, 0)
        ad1 = modify_state.schedule(args=('k', 'v'), eta=dt, convert_utc=False)
        ad2 = modify_state.schedule(args=('k2', 'v2'), eta=dt2, convert_utc=False)

        # dequeue the past-timestamped task and run it.
        worker = self.consumer.worker_threads[0]
        worker.check_message()

        self.assertTrue('k' in state)

        # dequeue the future-timestamped task.
        worker.check_message()

        # verify the task got stored in the schedule instead of executing
        self.assertFalse('k2' in state)

        self.assertStatusTask([
            ('scheduled', ad2.task, {}),
            ('finished', ad1.task, {}),
            ('started', ad1.task, {}),
        ])

        # run through an iteration of the scheduler
        self.consumer.scheduler_t.loop(dt)

        # our command was not enqueued and no events were emitted.
        self.assertEqual(len(test_queue._queue), 0)
        self.assertEqual(len(test_events._events), 3)

        # run through an iteration of the scheduler
        self.consumer.scheduler_t.loop(dt2)

        # our command was enqueued
        self.assertEqual(len(test_queue._queue), 1)
        self.assertEqual(len(test_events._events), 4)
        self.assertStatusTask([
            ('enqueued', ad2.task, {}),
        ])

    def test_retry_scheduling(self):
        # this will continually fail
        retry_command_slow('blampf')
        cur_time = datetime.datetime.utcnow()

        task = test_huey.dequeue()
        self.run_worker(task, ts=cur_time)
        self.assertEqual(self.handler.messages, [
            'Executing %s' % task,
            'Unhandled exception in worker thread',
            'Re-enqueueing task %s, 2 tries left' % task.task_id,
        ])

        in_11 = cur_time + datetime.timedelta(seconds=11)
        tasks_from_sched = test_huey.read_schedule(in_11)
        self.assertEqual(tasks_from_sched, [task])

        task = tasks_from_sched[0]
        self.assertEqual(task.retries, 2)
        exec_time = task.execute_time

        self.assertEqual((exec_time - cur_time).seconds, 10)
        self.assertStatusTask([
            ('scheduled', task, {
                'retries': 2,
                'retry_delay': 10,
                'execute_time': time.mktime(exec_time.timetuple())}),
            ('retrying', task, {
                'retries': 3,
                'retry_delay': 10,
                'execute_time': None}),
            ('error', task, {}),
            ('started', task, {}),
        ])

    def test_revoking_normal(self):
        # enqueue 2 normal commands
        r1 = modify_state('k', 'v')
        r2 = modify_state('k2', 'v2')

        # revoke the first *before it has been checked*
        r1.revoke()
        self.assertTrue(test_huey.is_revoked(r1.task))
        self.assertFalse(test_huey.is_revoked(r2.task))

        # dequeue a *single* message (r1)
        task = test_huey.dequeue()
        self.run_worker(task)

        self.assertEqual(len(test_events._events), 1)
        self.assertStatusTask([
            ('revoked', r1.task, {}),
        ])

        # no changes and the task was not added to the schedule
        self.assertFalse('k' in state)

        # dequeue a *single* message
        task = test_huey.dequeue()
        self.run_worker(task)

        self.assertTrue('k2' in state)

    def test_revoking_schedule(self):
        global state
        dt = datetime.datetime(2011, 1, 1)
        dt2 = datetime.datetime(2037, 1, 1)

        r1 = modify_state.schedule(args=('k', 'v'), eta=dt, convert_utc=False)
        r2 = modify_state.schedule(args=('k2', 'v2'), eta=dt, convert_utc=False)
        r3 = modify_state.schedule(args=('k3', 'v3'), eta=dt2, convert_utc=False)
        r4 = modify_state.schedule(args=('k4', 'v4'), eta=dt2, convert_utc=False)

        # revoke r1 and r3
        r1.revoke()
        r3.revoke()
        self.assertTrue(test_huey.is_revoked(r1.task))
        self.assertFalse(test_huey.is_revoked(r2.task))
        self.assertTrue(test_huey.is_revoked(r3.task))
        self.assertFalse(test_huey.is_revoked(r4.task))

        expected = [
            #state,        schedule
            ({},           0),
            ({'k2': 'v2'}, 0),
            ({'k2': 'v2'}, 1),
            ({'k2': 'v2'}, 2),
        ]

        for i in range(4):
            estate, esc = expected[i]

            # dequeue a *single* message
            task = test_huey.dequeue()
            self.run_worker(task)

            self.assertEqual(state, estate)
            self.assertEqual(len(test_huey.schedule._schedule), esc)

        # lets pretend its 2037
        future = dt2 + datetime.timedelta(seconds=1)
        self.consumer.scheduler_t.loop(future)
        self.assertEqual(len(test_huey.schedule._schedule), 0)

        # There are two tasks in the queue now (r3 and r4) -- process both.
        for i in range(2):
            task = test_huey.dequeue()
            self.run_worker(task, future)

        self.assertEqual(state, {'k2': 'v2', 'k4': 'v4'})

    def test_revoking_periodic(self):
        global state
        def loop_periodic(ts):
            self.consumer.periodic_t.loop(ts)
            for i in range(len(test_queue._queue)):
                task = test_huey.dequeue()
                self.run_worker(task, ts)

        # revoke the command once
        every_hour.revoke(revoke_once=True)
        self.assertTrue(every_hour.is_revoked())

        # it will be skipped the first go-round
        dt = datetime.datetime(2011, 1, 1, 0, 0)
        loop_periodic(dt)

        # it has not been run
        self.assertEqual(state, {})

        # the next go-round it will be enqueued
        loop_periodic(dt)

        # our command was run
        self.assertEqual(state, {'p': 'y'})

        # reset state
        state = {}

        # revoke the command
        every_hour.revoke()
        self.assertTrue(every_hour.is_revoked())

        # it will no longer be enqueued
        loop_periodic(dt)
        loop_periodic(dt)
        self.assertEqual(state, {})

        # restore
        every_hour.restore()
        self.assertFalse(every_hour.is_revoked())

        # it will now be enqueued
        loop_periodic(dt)
        self.assertEqual(state, {'p': 'y'})

        # reset
        state = {}

        # revoke for an hour
        td = datetime.timedelta(seconds=3600)
        every_hour.revoke(revoke_until=dt + td)

        loop_periodic(dt)
        self.assertEqual(state, {})

        # after an hour it is back
        loop_periodic(dt + td)
        self.assertEqual(state, {'p': 'y'})

        # our data store should reflect the delay
        task_obj = every_hour.task_class()
        self.assertEqual(len(test_huey.result_store._results), 1)
        self.assertTrue(task_obj.revoke_id in test_huey.result_store._results)
