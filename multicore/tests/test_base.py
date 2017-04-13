from django.conf import settings
from django.test import TestCase

from multicore import NUMBER_OF_WORKERS, Task


class TaskTestCase(TestCase):

    def test_parallel(self):
        print "yay"
