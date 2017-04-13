import multiprocessing
import time
import traceback
from Queue import Empty

import dill


default_app_config = "multicore.app.MulticoreAppConfig"
NUMBER_OF_WORKERS = multiprocessing.cpu_count()
_terminate = None
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

    def __init__(self):
        self.results = multiprocessing.Manager().Queue()
        self.count = 0

    def run(self, runnable, *args):
        _queue.put((self.results, dill.dumps(runnable), dill.dumps(args)))
        self.count += 1


    def get(self):
        li = []
        n = 0
        will_raise = None
        while n < self.count:
            result = self.results.get()
            if isinstance(result, Traceback):
                # Defer raise so our jobs are popped from the main queue
                if will_raise is None:
                    will_raise = result
            li.append(result)
            n += 1
        if will_raise is not None:
            will_raise()
        return li


def fetch_and_run():
    while not _terminate:

        # Optimization to avoid many Empty exceptions
        if not _queue.qsize():
            time.sleep(0.01)
            continue

        # Fetch task and run it
        try:
            results, runnable, args = _queue.get(block=True, timeout=0.01)
        except Empty:
            pass
        else:
            runnable = dill.loads(runnable)
            args = dill.loads(args)
            try:
                results.put(runnable(*args))
            except Exception as exc:
                msg = traceback.format_exc()
                results.put(Traceback(exc, msg))


def initialize():
    # Start the queue workers
    global _terminate
    if _terminate is None:
        _terminate = False
        for i in range(0, NUMBER_OF_WORKERS):
            p = Process(target=fetch_and_run)
            _workers.append(p)
            p.start()
