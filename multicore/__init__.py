try:
    import cPickle as pickle
except ImportError:
    import pickle
import json
import mmap
import multiprocessing
import os
import sys
import time
import traceback
from multiprocessing.sharedctypes import Array

import dill

from django.conf import settings


PY3 = sys.version_info[0] == 3

default_app_config = "multicore.app.MulticoreAppConfig"

# Workers is a list of processes. The workers are created in initialize.
NUMBER_OF_WORKERS = multiprocessing.cpu_count()
_workers = []

# Workers read their instructions from input buffers. The input buffers are
# created in initialize.
_input_buffers = None

# Memory map that records state of input buffers. 0 - available, 1 - job
# assigned, 2 - in progress, 3 - ready for collection.
_input_buffers_states = None

# Workers write their output to output buffers
MMAP_SIZE = 100000
_output_buffers = None

# The buffers have a large pipeline of jobs
BUFFER_DEPTH = NUMBER_OF_WORKERS * 8

# A lock required when manipulating input buffers
_lock_run = multiprocessing.Lock()
_lock_fetch = multiprocessing.Lock()


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


class NoAvailableInputBufferError(Exception):
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
        # Map buffer index to run index
        self.buffer_index_map = {}

    def run(self, runnable, *args, **kwargs):
        global _input_buffers
        global _lock_run

        serialization_format = kwargs.pop("serialization_format", "pickle")
        if serialization_format not in ("pickle", "json", "string"):
            raise RuntimeError(
                "Unrecognized serialization_format %s" % serialization_format
            )

        use_dill = kwargs.pop("use_dill", False)

        if not use_dill:
            pickled = pickle.dumps(
                (runnable, serialization_format, use_dill, args, kwargs), 0
            ).decode("utf-8")
        else:
            pickled = pickle.dumps(
                (
                    dill.dumps(runnable), serialization_format, use_dill,
                    dill.dumps(args), dill.dumps(kwargs)
                ),
                0
            ).decode("utf-8")

        _lock_run.acquire()
        try:
            # Find the first available worker
            found = False
            for index, state in enumerate(_input_buffers_states[:]):
                if state in (0, "0"):
                    found = True
                    break

            if not found:
                # Effectively cancel the rest of the task
                for i in self.buffer_index_map.keys():
                    _input_buffers_states[index] = 0 if PY3 else "0"
                raise NoAvailableInputBufferError()

            self.buffer_index_map[index] = self.count
            mm = _input_buffers[index]
            mm.seek(0)
            mm.write(("%.6d" % len(pickled) + pickled).encode("utf-8"))

            # Mark input buffer as holding a job
            _input_buffers_states[index] = 1 if PY3 else "1"

        finally:
            _lock_run.release()

        self.count += 1

    def get(self, timeout=10.0):
        SLEEP = 0.005

        # Avoid floating point operations on each loop by calculating the
        # maximum number of iterations.
        max_iterations = int(timeout / SLEEP)

        # Create sized list because we populate by index
        datas = [None] * self.count

        will_timeout = False
        fetches = self.count
        while fetches > 0:
            states = _input_buffers_states[:]

            for buf_index, run_index in self.buffer_index_map.items():
                if (datas[run_index] is None) and (states[buf_index] in (3, "3")):
                    data = _output_buffers[buf_index][:].decode("utf-8")
                    if ord(data[0]):
                        datas[run_index] = data
                        fetches -= 1

            max_iterations -= 1
            if max_iterations <= 0:
                will_timeout = True
                break

            time.sleep(SLEEP)

        # Mark the task as complete
        for index in self.buffer_index_map.keys():
            _input_buffers_states[index] = 0 if PY3 else "0"

        if will_timeout:
            raise TimeoutExceededError()

        # Convert list and possibly raise exception
        results = []
        for data in datas:
            length = int(data[:6])
            data = data[6:length+6]
            serialization_format = data[:6].strip()
            if serialization_format == "pickle":
                result = pickle.loads(data[6:].encode("utf-8"))
            elif serialization_format == "json":
                result = json.loads(data[6:])
            else:
                result = data[6:]
            results.append(result)

            if isinstance(result, Traceback):
                result()

        return results


def fetch_and_run(lock):
    global _input_buffers
    global _input_buffers_states
    global _output_buffers

    SLEEP = 0.005

    while True:
        for index in range(BUFFER_DEPTH):

            # Consider input buffers known to hold an assigned job. The first
            # part checks eshews a lock because it is much faster. This second
            # part must be locked because that is cheaper than potentially
            # running a job twice.
            if _input_buffers_states[index] not in (1, "1"):
                continue

            lock.acquire()
            if _input_buffers_states[index] not in (1, "1"):
                lock.release()
                continue

            # Mark input buffer as in progress
            _input_buffers_states[index] = 2 if PY3 else "2"
            lock.release()

            data = _input_buffers[index][:].decode("utf-8")

            # Decode the bytes
            length = int(data[:6])
            data = data[6:length+6]
            runnable, serialization_format, use_dill, args, \
                kwargs = pickle.loads(data.encode("utf-8"))

            if use_dill:
                runnable = dill.loads(runnable)
                args = dill.loads(args)

            try:
                result = runnable(*args)

                if serialization_format == "pickle":
                    serialized = pickle.dumps(result, 0).decode("utf-8")
                elif serialization_format == "json":
                    # We need it to be 6 chars
                    serialization_format = "json  "
                    serialized = json.dumps(result)
                elif serialization_format == "string":
                    serialized = result

                # No need for locking because we are guaranteed to be the only
                # one writing to it. Actually, there is a very tiny chance
                # another worker performed the same job but at worst we do a
                # little extra work.
                mm = _output_buffers[index]
                mm.seek(0)
                mm.write(
                    (
                        "%.6d" % (len(serialized) + 6) \
                        + serialization_format \
                        + serialized
                    ).encode("utf-8")
                )

                # Mark input buffer as containing a result
                _input_buffers_states[index] = 3 if PY3 else "3"

            except Exception as exc:
                # No need for buffer cleanup. Get will do that.
                msg = traceback.format_exc()
                pickled = pickle.dumps(Traceback(exc, msg), 0).decode("utf-8")
                mm = _output_buffers[index]
                mm.seek(0)
                mm.write(
                    (
                        "%.6d" % (len(pickled) + 6) \
                        + "pickle" \
                        + pickled
                    ).encode("utf-8")
                )

        time.sleep(SLEEP)


def initialize():
    """Start the queue workers if needed. Called by app.ready and possibly unit
    tests."""

    global NUMBER_OF_WORKERS
    global BUFFER_DEPTH
    global _workers
    global _input_buffers
    global _input_buffers_states
    global _output_buffers
    global _lock_fetch

    # If we already have workers do nothing
    if _workers:
        return

    _input_buffers = []
    _output_buffers = []
    for i in range(BUFFER_DEPTH):
        # Input buffers are smaller than output buffers
        _input_buffers.append(mmap.mmap(-1, 10000))
        _output_buffers.append(mmap.mmap(-1, MMAP_SIZE))

    _input_buffers_states = mmap.mmap(-1, BUFFER_DEPTH)
    for i in range(BUFFER_DEPTH):
        _input_buffers_states[i] = 0 if PY3 else "0"

    for i in range(0, NUMBER_OF_WORKERS):
        p = Process(target=fetch_and_run, args=(_lock_fetch,))
        _workers.append(p)
        p.start()


def shutdown():
    """Stop the queue workers. Called by unit tests."""

    global _workers

    # Immediately set running to false so workers may exit
    for p in _workers:
        # We can't join because we have no way of notifying the worker to stop
        # looping in a clean way. todo: send a message on the queue?
        p.terminate()
        del p

    _workers = []
