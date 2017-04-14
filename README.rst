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

#. Install or add ``django-multicore`` to your Python path.

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

Render 100 users
****************

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
    users = User.objects.all()
    for start, end in ranges(users):
        # Note we don't pass "users" to run because it can't be pickled
        task.run(multi_expensive_render, start, end)

    print ", ".join(task.get())

Settings
--------

If the system load average exceeds this value then a multicore task won't be
created and your code must fall back to a synchronous code path. Note that this
value is for a single core machine and is automatically converted to reflect
the actual number of cores on the machine. A value of None (the default) always
creates a multicore task.

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

It just has too many issues with Django when it comes to scoping.

