try:
    import cPickle as pickle
except ImportError:
    import pickle
import json
import multiprocessing
import os
import socket
import sys
import tempfile
import time
import traceback
from collections import OrderedDict
from shutil import rmtree

import dill

from django.conf import settings
from django.core.handlers.wsgi import WSGIRequest


PY3 = sys.version_info[0] == 3

# PyPy 2 doesn't allow importing of the reduction module because of a platform
# mismatch issue.  We prefer to use it so try and import it in any case. There
# is a file-based fallback code path.
PIPES_POSSIBLE = False
try:
    from multiprocessing import reduction
    PIPES_POSSIBLE = True
except ImportError:
    pass

# Python 3 and PyPy 3 allows checking for SCM_RIGHTS. Without these rights
# pipes aren't possible.
if PY3 and not hasattr(socket, "SCM_RIGHTS"):
    PIPES_POSSIBLE = False

# Python 3.5 deprecates the reduce_connection function. Until I figure out how
# to do it the new way provide these functions.
if PIPES_POSSIBLE and not hasattr(reduction, "reduce_connection"):

    def reduce_connection(conn):
        df = reduction.DupFd(conn.fileno())
        return rebuild_connection, (df, conn.readable, conn.writable)

    def rebuild_connection(df, readable, writable):
        from multiprocessing.connection import Connection
        fd = df.detach()
        return Connection(fd, readable, writable)

    reduction.reduce_connection = reduce_connection
    reduction.rebuild_connection = rebuild_connection


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


class TimeoutExceededError(Exception):
    pass


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
        global _queue

        serialization_format = kwargs.pop("serialization_format", "pickle")
        if serialization_format not in ("pickle", "json", "string"):
            raise RuntimeError(
                "Unrecognized serialization_format %s" % serialization_format
            )

        use_dill = kwargs.pop("use_dill", False)

        if self.use_pipes:
            # http://stackoverflow.com/questions/1446004/python-2-6-send-connection-object-over-queue-pipe-etc
            # expains why reduction is required.
            receiver, pipe = multiprocessing.Pipe(False)
            self.receivers[self.count] = receiver
            arg = pickle.dumps(reduction.reduce_connection(pipe))
        else:
            arg = self.path

        if not use_dill:
            _queue.put((
                self.count, arg, runnable, serialization_format, use_dill,
                args, kwargs
            ))
        else:
            _queue.put((
                self.count, arg, dill.dumps(runnable), serialization_format, use_dill,
                dill.dumps(args), dill.dumps(kwargs)
            ))

        self.count += 1

    def get(self, timeout=10.0):
        datas = [None] * self.count

        if self.use_pipes:
            for i, receiver in self.receivers.items():
                 data = receiver.recv()
                 datas[i] = data

        else:
            # Monitor directory to see if files are complete. Exhaustive checks
            # are luckily quite fast.

            # Avoid floating point operations on each loop by calculating the
            # maximum number of iterations.
            max_iterations = int(timeout / 0.001)

            while True:
                filenames = os.listdir(self.path)
                if (len(filenames) == self.count):
                    filenames.sort(key=lambda f: int(f))
                    filenames = [os.path.join(self.path, f) for f in filenames]
                    if all([os.path.getsize(f) for f in filenames]):
                        for n, filename in enumerate(filenames):
                            fp = open(filename, "r")
                            try:
                                datas[n] = fp.read()
                            finally:
                                fp.close()
                        break
                max_iterations -= 1
                if max_iterations <= 0:
                    raise TimeoutExceededError()
                time.sleep(0.001)
            rmtree(self.path)

        # Convert list and possibly raise exception
        results = []
        for data in datas:
            serialization_format = data[:6].strip()
            if serialization_format == "pickle":
                if PY3:
                    result = pickle.loads(bytes(data[6:], "ascii"))
                else:
                    result = pickle.loads(data[6:])
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
        index, pipe_or_path, runnable, serialization_format, use_dill, args, \
            kwargs = _queue.get()

        if use_pipes():
            f, a = pickle.loads(pipe_or_path)
            pipe = f(*a)
        else:
            path = pipe_or_path
            filename = os.path.join(path, str(index))

        if use_dill:
            runnable = dill.loads(runnable)
            args = dill.loads(args)

        try:
            result = runnable(*args)

            if serialization_format == "pickle":
                if PY3:
                    serialized = pickle.dumps(result, 0).decode()
                else:
                    serialized = pickle.dumps(result)
            elif serialization_format == "json":
                # We need it to be 6 chars
                serialization_format = "json  "
                serialized = json.dumps(result, indent=4)
            elif serialization_format == "string":
                serialized = result
            if use_pipes():
                pipe.send(serialization_format + serialized)
                pipe.close()
            else:
                fp = open(filename, "w")
                try:
                    fp.write(serialization_format + serialized)
                    fp.flush()
                finally:
                    fp.close()

        except Exception as exc:
            msg = traceback.format_exc()
            if PY3:
                pickled = pickle.dumps(Traceback(exc, msg), 0).decode()
            else:
                pickled = pickle.dumps(Traceback(exc, msg))
            if use_pipes():
                pipe.send(
                    serialization_format
                    + pickled
                )
                pipe.close()
            else:
                fp = open(filename, "w")
                try:
                    fp.write(
                        "pickle" \
                        + pickled
                    )
                finally:
                    fp.close()


def use_pipes():
    try:
        return getattr(settings, "MULTICORE", {}).get("pipes", True) \
            and PIPES_POSSIBLE
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

    _queue = multiprocessing.Queue()

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
