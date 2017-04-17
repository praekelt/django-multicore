import cPickle
import json
import multiprocessing
import os
import os.path
import sys
import tempfile
import time
import traceback
from collections import OrderedDict
from Queue import Empty
from shutil import rmtree

import dill

from django.conf import settings


# PyPy doesn't support the reduction function we require on Python 2.x yet but
# try and import in any case. use_pipes guards against misuse.
PIPE_REDUCTION = False
if sys.version_info[0] < 3:
    try:
        from multiprocessing import reduction
        PIPE_REDUCTION = True
    except ImportError:
        pass

default_app_config = "multicore.app.MulticoreAppConfig"
NUMBER_OF_WORKERS = multiprocessing.cpu_count()
_workers = []
_queue = None


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
        self.count = 0
        self.use_pipes = use_pipes()
        if self.use_pipes:
            self.receivers = OrderedDict()
        else:
            self.path = tempfile.mkdtemp()

    def run(self, runnable, *args, **kwargs):
        serialization_format = kwargs.pop("serialization_format", "pickle")
        if serialization_format not in ("pickle", "json", "string"):
            raise RuntimeError(
                "Unrecognized serialization_format %s" % serialization_format
            )

        if self.use_pipes:
            # http://stackoverflow.com/questions/1446004/python-2-6-send-connection-object-over-queue-pipe-etc
            # expains why reduction is required.
            receiver, pipe = multiprocessing.Pipe(False)
            self.receivers[self.count] = receiver
            if PIPE_REDUCTION:
                arg = cPickle.dumps(reduction.reduce_connection(pipe))
            else:
                arg = cPickle.dumps(pipe)
        else:
            arg = self.path
        _queue.put((
            self.count, arg, dill.dumps(runnable), serialization_format,
            dill.dumps(args), dill.dumps(kwargs)
        ))
        self.count += 1


    def get(self, timeout=10):
        datas = [None] * self.count

        if self.use_pipes:
            for i, receiver in self.receivers.items():
                data = receiver.recv()
                datas[i] = data

        else:
            # Monitor directory to see if files are complete. Exhaustive checks
            # are luckily quite fast.
            while True:
                filenames = os.listdir(self.path)
                if (len(filenames) == self.count):
                    filenames.sort()
                    filenames = [os.path.join(self.path, f) for f in filenames]
                    if all([os.path.getsize(f) for f in filenames]):
                        for n, filename in enumerate(filenames):
                            fp = open(filename, "r")
                            try:
                                datas[n] = fp.read()
                            finally:
                                fp.close()
                        break
                time.sleep(0.01)
            rmtree(self.path)

        # Convert list and possibly raise exception
        results = []
        for data in datas:
            serialization_format = data[:6].strip()
            if serialization_format == "pickle":
                result = cPickle.loads(data[6:])
            elif serialization_format == "json":
                result = json.loads(data[6:])
            else:
                result = data[6:]
            results.append(result)

            if isinstance(result, Traceback):
                result()

        return results


def fetch_and_run():
    global _queue

    while True:

        # Fetch task and run it
        try:
            index, pipe_or_path, runnable, serialization_format, args, \
                kwargs = _queue.get()
        except Empty:
            pass
        else:
            if use_pipes():
                f, a = cPickle.loads(pipe_or_path)
                pipe = f(*a)
            else:
                path = pipe_or_path
                filename = os.path.join(path, str(index))

            runnable = dill.loads(runnable)
            args = dill.loads(args)
            try:
                result = runnable(*args)

                if serialization_format == "pickle":
                    serialized = cPickle.dumps(result)
                elif serialization_format == "json":
                    # We need it to be 6 chars
                    serialization_format = "json  "
                    serialized = json.dumps(result, indent=4)
                elif serialization_format == "string":
                    serialized = result
                if use_pipes():
                    pipe.send(serialization_format + serialized)
                else:
                    fp = open(filename, "w")
                    try:
                        fp.write(serialization_format + serialized)
                    finally:
                        fp.close()

            except Exception as exc:
                msg = traceback.format_exc()
                if use_pipes():
                    pipe.send(
                        serialization_format
                        + cPickle.dumps(Traceback(exc, msg))
                    )
                else:
                    fp = open(filename, "w")
                    try:
                        fp.write(
                            "pickle" \
                            + cPickle.dumps((index, Traceback(exc, msg)))
                        )
                    finally:
                        fp.close()


def use_pipes():
    try:
        return settings.MULTICORE["pipes"]
    except (AttributeError, KeyError):
        return False


def initialize():
    """Start the queue workers if needed. Called by app.ready and possibly unit
    tests."""

    global NUMBER_OF_WORKERS
    global _queue
    global _workers

    # If we already have a queue do nothing
    if _queue is not None:
        return

    _queue = multiprocessing.Manager().Queue()

    for i in range(0, NUMBER_OF_WORKERS):
        p = Process(target=fetch_and_run)
        _workers.append(p)
        p.start()


def shutdown():
    """Stop the queue workers. Called by unit tests."""

    global _queue
    global _workers

    # Immediately set running to false so workers may exit
    for p in _workers:
        # We can't join because we have no way of notifying the worker to stop
        # looping in a clean way. todo: send a message on the queue?
        p.terminate()
        del p

    del _queue
    _queue = None
    _workers = []
