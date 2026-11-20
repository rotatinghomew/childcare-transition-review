"""核对原始业务资料之间的引用与记录形式。"""

import unittest

from inspect_samples import read_samples


class SampleTests(unittest.TestCase):
    def setUp(self):
        self.data = read_samples()

    def test_transition_receipt_has_earlier_request(self):
        events = self.data['timeline']
        enrolled = [event for event in events if event['kind'] == '常规班实际接收']
        self.assertTrue(enrolled)
        cutoff = self.data['review']['cutoffs'][0]
        self.assertTrue(any(event['date'] <= cutoff < event['reported'] for event in enrolled))
        for event in enrolled:
            self.assertLess(event['date'], event['reported'])
            self.assertTrue(any(row['child'] == event['child'] and row['kind'] == '申请转班' and row['date'] < event['date'] for row in events))

    def test_teacher_may_cover_more_than_one_room(self):
        available = self.data['staff']
        self.assertTrue(any(len(rooms) > 1 for rooms in available.values()))
        for capacity in self.data['capacity']:
            self.assertIn(capacity['room'], available[capacity['teacher']])
