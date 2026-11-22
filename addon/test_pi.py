import unittest

import pi


class CrashLoops(unittest.TestCase):
    def setUp(self):
        pi.restarts.clear()
        self.counts = {'otbr': 0, 'matter': 0}

    def get(self, path):
        if path.startswith('/containers/json'):
            return [{'Id': n, 'Names': [f'/{n}']} for n in self.counts]
        return {'RestartCount': self.counts[path.split('/')[2]]}

    def test_three_restarts_in_30_min_is_a_crash_loop(self):
        self.assertEqual(pi.crash_loops(0, self.get), {})
        for minute in (1, 2, 3):
            self.counts['otbr'] += 1
            found = pi.crash_loops(minute * 60, self.get)
        self.assertEqual(found, {'otbr': 3})

    def test_restarts_spread_out_are_not(self):
        pi.crash_loops(0, self.get)
        for minute in (20, 40, 60):
            self.counts['otbr'] += 1
            found = pi.crash_loops(minute * 60, self.get)
        self.assertEqual(found, {})

    def test_it_clears_once_the_restarts_stop(self):
        pi.crash_loops(0, self.get)
        self.counts['otbr'] = 5
        self.assertEqual(pi.crash_loops(60, self.get), {'otbr': 5})
        self.assertEqual(pi.crash_loops(60 + pi.CRASH_WINDOW + 1, self.get), {})

    def test_a_container_made_again_starts_over(self):
        self.counts['otbr'] = 7
        pi.crash_loops(0, self.get)
        self.counts['otbr'] = 0
        self.assertEqual(pi.crash_loops(60, self.get), {})
        self.counts['otbr'] = 1
        self.assertEqual(pi.crash_loops(120, self.get), {})


if __name__ == '__main__':
    unittest.main()
