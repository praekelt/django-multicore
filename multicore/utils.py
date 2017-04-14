import math

from multicore import NUMBER_OF_WORKERS


def ranges(iterable):
    """Return a set of ranges (start, end) points so an iterable can be passed
    in optimal chunks to a task."""

    count = len(iterable)
    delta = int(math.ceil(count * 1.0 / NUMBER_OF_WORKERS))
    start = 0
    while start < count:
        yield start, start+delta
        start += delta
