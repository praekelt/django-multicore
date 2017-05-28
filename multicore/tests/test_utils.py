from django.test import TestCase

from multicore.utils import ranges


class TaskUtilsCase(TestCase):

    def test_ranges(self):
        li = list(ranges(range(80), number_of_workers=4))
        self.assertEqual(li, [(0, 20), (20, 40), (40, 60), (60, 80)])

        li = list(ranges(range(80), min_range_size=30, number_of_workers=4))
        self.assertEqual(li, [(0, 30), (30, 60), (60, 80)])

        li = list(ranges(range(80), min_range_size=100, number_of_workers=4))
        self.assertEqual(li, [(0, 80)])
