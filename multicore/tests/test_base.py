import math
import time

from django.conf import settings
from django.test import TestCase

from multicore import NUMBER_OF_WORKERS, Task, initialize, shutdown
from multicore.utils import ranges

# We unfortunately can't show a database test because the Django testing
# framework attempt to create a test database for each sub-process. Make our
# own set of users to illustrate the principle.
users = []
for i in range(100):
    users.append({"username": "user%s" % i})


def expensive_render(user):
    time.sleep(0.01)
    return user["username"]


def multi_expensive_render(start, end):
    """Do multiple expensive renders"""

    s = ""
    for user in users[start:end]:
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
        t_start = time.time()
        s_sync = ""
        for user in users:
            s_sync += expensive_render(user)
        duration_sync = time.time() - t_start

        # Async. Break into chunks of tasks.
        t_start = time.time()
        task = Task()
        for start, end in ranges(users):
            task.run(multi_expensive_render, start, end)

        s_async = "".join(task.get())
        duration_async = time.time() - t_start

        # Hopefully we're on a multicore machine :)
        self.assertEqual(s_sync, s_async)
        self.failUnless(duration_async < duration_sync)

    def test_no_deadlock(self):
        pass
