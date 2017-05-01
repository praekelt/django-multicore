Django Multicore
================
**An app that makes it easy to parallelize Django code.**

.. figure:: https://travis-ci.org/praekelt/django-multicore.svg?branch=develop
   :align: center
   :alt: Travis

.. contents:: Contents
    :depth: 5

Installation
------------

#. Install or add ``dill`` and ``django-multicore`` to your Python path.

#. Add ``multicore`` to your ``INSTALLED_APPS`` setting.

Overview
--------

Django itself is a single threaded application but many pieces of code are
trivially easy to run in parallel. Multi-threading is typically used to achieve
this but it is still subject to Python's Global Interpreter lock. Python's
multiprocessing module bypasses the GIL but it is harder to use within Django.

This app presents a simple interface for writing parallel code.

Features
--------

#. Persistent pool of workers enabling persistent database connections.
#. Can take system load average into account to decide whether parallelization
   is worth it at any given time.

Usage
-----

Let's render 100 users. Always break a large task into smaller tasks, but not
too small! If the ranges are too small then tasks aren't worth the effort
because the overhead becomes too much.::

    import time
    from multicore import Task
    from multicore.utils import ranges


    def expensive_render(user):
        time.sleep(0.01)
        return user.username


    def multi_expensive_render(start, end):
        s = ""
        for user in User.objects.all()[start:end]:
            s += expensive_render(user)
        return s


    task = Task()
    users = User.objects.all()[:100]
    for start, end in ranges(users):
        # Note we don't pass "users" to run because it can't be pickled
        task.run(multi_expensive_render, start, end)

    print ", ".join(task.get())

If we control the code it's easy but sometimes we need to monkey patch code in
another app. Django Rest Framework's list fetch is a prime candidate for parallelization.
Here's the original code (pagination block omitted for brevity)::

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

Rewriting it requires a *lot* of knowledge of how DRF works.::

    import importlib
    from django.core.urlresolvers import resolve
    from rest_framework.mixins import ListModelMixin
    from multicore import Task
    from multicore.utils import ranges, PicklableWSGIRequest


    def helper(request, start, end):
        view_func, args, kwargs = resolve(request.get_full_path())
        module = importlib.import_module(view_func.__module__)
        view = getattr(module, view_func.__name__)()
        setattr(view, "request", request)
        view.format_kwarg = view.get_format_suffix()
        queryset = view.filter_queryset(view.get_queryset())
        serializer = view.get_serializer(queryset[start:end], many=True)
        return serializer.data


    def mylist(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())

        task = Task()
        if task is not None:
            for start, end in ranges(queryset):
                task.run(
                    helper, PicklableWSGIRequest(request._request),
                    start, end
                )

            # Get results and combine the lists
            results = [item for sublist in task.get() for item in sublist]
            return Response(results)

        else:
            serializer = self.get_serializer(queryset, many=True)
            results = serializer.data

        return Response(results)

    ListModelMixin.list = mylist

The ``run`` method takes an optional parameter ``serialization_format`` with value
``pickle`` (the default), ``json`` or ``string``. Pickle is slow and safe. If you
know what type of data you have (you should!) set this as appropriate.

The ``run`` method also takes an optional parameter ``use_dill`` with default
value ``False``. Dill is a library that can often pickle things that can't be
pickled by the standard pickler but it is slightly slower.

Settings
--------

If the system load average exceeds this value then a multicore task won't be
created and your code must fall back to a synchronous code path. Note that this
value is for a single core machine and is automatically converted to reflect
the actual number of cores on the machine. A value of None (the default) always
creates a multicore task::

    MULTICORE = {"max-load-average": 85}

FAQ's
-----

My webserver is already under load. How does this app help?
***********************************************************

Webservers typically run number-of-cores x 8 Django processes at 70% load
because it gives you enough overhead while at the same time not wasting money
by sitting idly.

If you have 4 cores and 4 cold requests arrive (requests that won't hit the
Django cache and thus take longer to complete) then multicore won't help you.
However, if less than 4 cold requests arrive then you have a core available to
reduce the response time of each individual request.

Will it try to execute hundreds of pieces of code in parallel?
**************************************************************

No. The pool has a fixed size and can only execute number-of-cores tasks in
parallel. You may also set `max_load_average` as a further guard.

Why didn't you use multiprocessing.Pool?
****************************************

It just has too many issues with Django when it comes to scoping. Even pipes
and sockets introduce too much overhead, so memory mapping is used.

Do you have any benchmarks?
***************************

No, because this is just an interface, not a collection of parallel code.

Okay... the unit test is 3 times as fast on a quad core machine. And the Django
Rest Framework code in this doc is 2 times as fast on the same quad core
machine. Note that it is very dependent on the type of serializer and data.

In general the code scales nearly linearly if you don't access the database.
Multicore itself adds about 5 milliseconds overhead on my machine.

