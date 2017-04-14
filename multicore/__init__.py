import multiprocessing
import os
import time
import traceback
from Queue import Empty

import dill

from django.conf import settings


default_app_config = "multicore.app.MulticoreAppConfig"
NUMBER_OF_WORKERS = multiprocessing.cpu_count()
_workers = []
_queue = multiprocessing.Manager().Queue()


class Process(multiprocessing.Process):
    """Wrap Process so exception handling propagates to parent process"""

    def __init__(self, *args, **kwargs):
        multiprocessing.Process.__init__(self, *args, **kwargs)
        self._pconn, self._cconn = multiprocessing.Pipe()
        self._exception = None

    def run(self):
        try:
            multiprocessing.Process.run(self)
            self._cconn.send(None)
        except Exception as e:
            tb = traceback.format_exc()
            self._cconn.send((e, tb))
            raise

    @property
    def exception(self):
        if self._pconn.poll():
            self._exception = self._pconn.recv()
        return self._exception


class Traceback(object):

    def __init__(self, exc, msg):
        self.exc = exc
        self.msg = msg

    def __call__(self):
        raise self.exc.__class__(self.msg)


class Task(object):

    def __new__(cls, *args, **kwargs):
        # If the load average for the last minute is larger than a defined
        # threshold then don't return a task. Note that the threshold is
        # specified as for a single core machine, so we multiply it with the
        # number of workers. "None" is the default and always allows a task
        # to be returned.
        try:
            v = settings.MULTICORE["max-load-average"]
        except (AttributeError, KeyError):
            v = None
        if (v is not None) and (os.getloadavg()[0] > v * NUMBER_OF_WORKERS):
            return None
        return super(Task, cls).__new__(cls, *args, **kwargs)

    def __init__(self, **kwargs):
        self.results = multiprocessing.Manager().Queue()
        self.count = 0

    def run(self, runnable, *args):
        _queue.put((self.count, self.results, dill.dumps(runnable), dill.dumps(args)))
        self.count += 1


    def get(self):
        li = [None] * self.count
        n = 0
        will_raise = None
        while n < self.count:
            index, result = self.results.get()
            if isinstance(result, Traceback):
                # Defer raise so our jobs are popped from the main queue
                if will_raise is None:
                    will_raise = result
            li[index] = result
            n += 1
        if will_raise is not None:
            will_raise()
        return li


def fetch_and_run():

    while True:

        # Optimization to avoid many Empty exceptions
        try:
            qs = _queue.qsize()
        except IOError:
            # Mask IO errors during reload
            qs = 0
        if not qs:
            time.sleep(0.01)
            continue

        # Fetch task and run it
        try:
            index, results, runnable, args = _queue.get(block=True, timeout=0.01)
        except Empty:
            pass
        else:
            runnable = dill.loads(runnable)
            args = dill.loads(args)
            try:
                results.put((index, runnable(*args)))
            except Exception as exc:
                msg = traceback.format_exc()
                results.put((index, Traceback(exc, msg)))


def initialize():
    """Start the queue workers if needed. Called by app.ready and possibly unit
    tests."""

    if _workers:
        return
    for i in range(0, NUMBER_OF_WORKERS):
        p = Process(target=fetch_and_run)
        _workers.append(p)
        p.start()


def shutdown():
    """Stop the queue workers. Called by unit tests."""

    for p in _workers:
        p.terminate()
