try:
    import cPickle as pickle
except ImportError:
    import pickle
import ctypes
import json
import mmap
import multiprocessing
import os
import socket
import sys
import tempfile
import time
import traceback
from collections import OrderedDict
from multiprocessing.sharedctypes import Array
from shutil import rmtree

import dill

from django.conf import settings
from django.core.handlers.wsgi import WSGIRequest


PY3 = sys.version_info[0] == 3

default_app_config = "multicore.app.MulticoreAppConfig"
NUMBER_OF_WORKERS = multiprocessing.cpu_count()
_workers = []
_queue = None

#array = Array(ctypes.c_char_p, ["a"*1000, "a"*1000, "a"*1000, "a"*1000], lock=True)
#array = Array(ctypes.c_char_p, 4, lock=True)
MMAP_SIZE = 100000
array = []
for i in range(NUMBER_OF_WORKERS):
    array.append(mmap.mmap(-1, MMAP_SIZE))




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

    def run(self, runnable, *args, **kwargs):
        global _queue

        serialization_format = kwargs.pop("serialization_format", "pickle")
        if serialization_format not in ("pickle", "json", "string"):
            raise RuntimeError(
                "Unrecognized serialization_format %s" % serialization_format
            )

        use_dill = kwargs.pop("use_dill", False)
        # todo dill encoding
        pickled = pickle.dumps(
            (self.count, runnable, serialization_format, use_dill, args, kwargs), 0
        ).decode("utf-8")
        _queue[self.count].seek(0)
        _queue[self.count].write(("%.6d" % len(pickled) + pickled).encode("utf-8"))
        self.count += 1

    def get(self, timeout=10.0):
        # Avoid floating point operations on each loop by calculating the
        # maximum number of iterations.
        max_iterations = int(timeout / 0.01)
        datas = [None] * NUMBER_OF_WORKERS
        fetches = NUMBER_OF_WORKERS
        while fetches > 0:
            for index, mm in enumerate(array):
                if datas[index] is None:
                    mm.seek(0)
                    data = mm.read(MMAP_SIZE).decode("utf-8")
                    if data[0] != "\x00":
                        datas[index] = data
                        fetches -= 1

            max_iterations -= 1
            if max_iterations <= 0:
                raise TimeoutExceededError()
            time.sleep(0.01)

        # Convert list and possibly raise exception
        results = []
        for data in datas:
            length = int(data[6:12])
            data = data[:length]
            serialization_format = data[:6].strip()
            if serialization_format == "pickle":
                result = pickle.loads(data[12:].encode("utf-8"))
            elif serialization_format == "json":
                result = json.loads(data[12:])
            else:
                result = data[6:]
            results.append(result)

            if isinstance(result, Traceback):
                result()

        return results


def fetch_and_run():
    global _queue
    global array

    while True:

        for mmap in _queue:
            mmap.seek(0)
            data = mmap.read(10000).decode("utf-8")
            if data[0] not in ("", "\x00", b"\x00"):
                mmap.seek(0)
                mmap.write(b"\x00")
                length = int(data[:6])
                data = data[6:length+6]
                index, runnable, serialization_format, use_dill, args, \
                    kwargs = pickle.loads(data.encode("utf-8"))

                #if use_dill:
                #    runnable = dill.loads(runnable)
                #    args = dill.loads(args)

                try:
                    result = runnable(*args)

                    if serialization_format == "pickle":
                        serialized = pickle.dumps(result, 0).decode("utf-8")
                    elif serialization_format == "json":
                        # We need it to be 6 chars
                        serialization_format = "json  "
                        serialized = json.dumps(result, indent=4)
                    elif serialization_format == "string":
                        serialized = result

                    array[index].write((serialization_format + "%.6d" % (len(serialized) + 12) + serialized).encode("utf-8"))
                except Exception as exc:
                    msg = traceback.format_exc()
                    pickled = pickle.dumps(Traceback(exc, msg), 0).decode("utf-8")
                    array[index].write(("pickle" + "%.6d" % (len(pickled) + 12) + pickled).encode("utf-8"))
        time.sleep(0.01)


def initialize():
    """Start the queue workers if needed. Called by app.ready and possibly unit
    tests."""

    global NUMBER_OF_WORKERS
    global _queue
    global _workers

    # If we already have a queue do nothing
    if _queue is not None:
        return

    _queue = []
    for i in range(NUMBER_OF_WORKERS):
        _queue.append(mmap.mmap(-1, 10000))


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
