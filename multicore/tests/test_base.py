import math
import time

from django.conf import settings
from django.test import TestCase

from multicore import NUMBER_OF_WORKERS, Task, initialize, shutdown


# We unfortunately can't show a database test because the Django testing
# framework attempt to create a test database for each sub-process. Make our
# own set of users to illustrate the principle.
users = []
for i in range(100):
    users.append({"username": "user%s" % i})


def expensive_render(user):
    time.sleep(0.01)
    return user["username"]


def multi_expensive_render(offset, delta):
    """Do multiple expensive renders"""

    s = ""
    for user in users[offset:offset+delta]:
        s += expensive_render(user)
    return s


class TaskTestCase(TestCase):

    @classmethod
    def setUpClass(cls):
        super(TaskTestCase, cls).setUpClass()
        initialize()

    @classmethod
    def tearDownClass(cls):
        super(TaskTestCase, cls).tearDownClass()
        shutdown()

    def test_parallel(self):

        # Sync
        start = time.time()
        s_sync = ""
        for user in users:
            s_sync += expensive_render(user)
        duration_sync = time.time() - start

        # Async. Break into chunks of tasks.
        start = time.time()
        task = Task(ignore_load=True)
        count = len(users)
        delta = int(math.ceil(count * 1.0 / NUMBER_OF_WORKERS))
        offset = 0
        while offset < count:
            task.run(multi_expensive_render, offset, delta)
            offset += delta
        s_async = "".join(task.get())
        duration_async = time.time() - start

        # Hopefully we're on a multicore machine :)
        self.assertEqual(s_sync, s_async)
        self.failUnless(duration_async < duration_sync)

    def test_no_deadlock(self):
        pass
