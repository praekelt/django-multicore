import math


from django.contrib.auth.models import AnonymousUser
from django.db.models import QuerySet
from django.http import HttpRequest

from multicore import NUMBER_OF_WORKERS


TO_REDUCE = ("HTTP_HOST", "REMOTE_ADDR", "HTTPS", "SERVER_NAME", "SERVER_PORT")


# Some types have been deprecated. This helper class keeps the code in
# PicklableWSGIRequest short.
class Klass1(object): pass
try:
    klass1 = unicode
except NameError:
    klass1 = Klass1


class PicklableWSGIRequest(object):
    """Wrapper that enables requests to be pickled"""

    def __init__(self, context):
        self.context = context

    def __reduce__(self):
        di = {
            "META": dict([
                (k, self.context.META[k]) for k in TO_REDUCE
                if k in self.context.META and
                isinstance(self.context.META[k], (int, str, bool, klass1))
            ]),
            "POST": self.context.POST,
            "GET": self.context.GET,
            "user": self.context.user if hasattr(self.context, "user") else AnonymousUser(),
            "path": self.context.path
        }
        return HttpRequest, (), di


def ranges(iterable, min_range_size=0, number_of_workers=None):
    """Return a set of ranges (start, end) points so an iterable can be passed
    in optimal chunks to a task."""

    # Use faster method for queryset
    if isinstance(iterable, QuerySet):
        count = iterable.count()
    else:
        count = len(iterable)

    delta = max(
        int(math.ceil(count * 1.0 / (number_of_workers or NUMBER_OF_WORKERS))),
        min_range_size
    )
    start = 0
    while start < count:
        end = min(start + delta, count)
        yield start, end
        start = end

