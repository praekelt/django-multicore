import math

from django.db.models import QuerySet

from multicore import NUMBER_OF_WORKERS


def ranges(iterable):
    """Return a set of ranges (start, end) points so an iterable can be passed
    in optimal chunks to a task."""

    # Use faster method for queryset
    if isinstance(iterable, QuerySet):
        count = iterable.count()
    else:
        count = len(iterable)

    delta = int(math.ceil(count * 1.0 / NUMBER_OF_WORKERS))
    start = 0
    while start < count:
        yield start, start+delta
        start += delta
