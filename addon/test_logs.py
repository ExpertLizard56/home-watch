"""Tests for the page's Logs section: the range cut by each line's own time, and the .log / .zip it downloads.

Run from addon/: `python -m unittest test_logs`. Nothing here needs Home Assistant or a real data folder: the folder is
a temporary one, made before trail is imported (it reads HOME_WATCH_DATA then).
"""

import io
import os
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta

DATA = tempfile.mkdtemp(prefix='home-watch-test-')
os.environ['HOME_WATCH_DATA'] = DATA

import trail  # noqa: E402  (after the temporary data folder is set)

MIDNIGHT = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
TODAY = MIDNIGHT + timedelta(hours=1)
YESTERDAY = MIDNIGHT - timedelta(hours=1)
TWO_DAYS = MIDNIGHT - timedelta(days=2) + timedelta(hours=1)
TEN_DAYS = MIDNIGHT - timedelta(days=10)
NOW = datetime(2026, 11, 13, 12, 0).timestamp()


def line(when, text='something happened', seconds=True):
    """A line as trail.listen writes it (with seconds), or as watch.note does (whole minutes)."""
    return f"{when:%Y-%m-%d %H:%M:%S} {text}" if seconds else f"{when:%Y-%m-%d %H:%M} {text}"


def put(machine, when, count=1, text='something happened'):
    """Appends `count` stamped lines to a machine's log for `when`'s month."""
    os.makedirs(trail.LOGS, exist_ok=True)
    with open(f"{trail.LOGS}/{machine} {when:%Y-%m}.log", 'a', encoding='utf-8') as f:
        f.writelines(f"{when:%Y-%m-%d %H:%M:%S} {text} {n}\n" for n in range(count))


class Logs(unittest.TestCase):
    def setUp(self):
        os.makedirs(trail.LOGS, exist_ok=True)
        for name in os.listdir(trail.LOGS):
            os.remove(os.path.join(trail.LOGS, name))
        if os.path.exists(trail.OWN_LOG):
            os.remove(trail.OWN_LOG)
        for when in (TEN_DAYS, TWO_DAYS, YESTERDAY, TODAY):  # oldest first, as the hub writes them
            put('nas', when)

    def test_since_is_local_midnight(self):
        self.assertEqual(trail.since(1, NOW), datetime(2026, 11, 13))
        self.assertEqual(trail.since(3, NOW), datetime(2026, 11, 11))
        self.assertIsNone(trail.since(0, NOW))

    def test_lines_cut_by_their_own_time(self):
        self.assertEqual(len(trail.lines('nas', 1)), 1)   # today
        self.assertEqual(len(trail.lines('nas', 3)), 3)   # today and the two days before
        self.assertEqual(len(trail.lines('nas', 14)), 4)  # the ten-day-old line comes back
        self.assertEqual(len(trail.lines('nas', 0)), 4)   # everything kept

    def test_a_line_without_a_date_keeps_the_one_before_it(self):
        with open(f"{trail.LOGS}/nas {TODAY:%Y-%m}.log", 'a', encoding='utf-8') as f:
            f.write('    continued\n')
        self.assertIn('    continued', trail.lines('nas', 1))
        put('old', TEN_DAYS)
        with open(f"{trail.LOGS}/old {TEN_DAYS:%Y-%m}.log", 'a', encoding='utf-8') as f:
            f.write('    old continued\n')
        self.assertNotIn('    old continued', trail.lines('old', 1))
        self.assertIn('    old continued', trail.lines('old', 0))

    def test_a_month_before_the_range_is_not_read(self):
        put('router', datetime(2020, 1, 2, 3, 4, 5))
        self.assertNotIn('router', [name for name, _ in trail.saved() if trail.lines(name, 7)])
        self.assertEqual(len(trail.lines('router', 7)), 0)
        self.assertEqual(len(trail.lines('router', 0)), 1)

    def test_saved_lists_machines_then_the_hubs_own_log(self):
        with open(trail.OWN_LOG, 'w', encoding='utf-8') as f:
            f.write(line(TODAY, 'started, watching 1 machines', seconds=False) + '\n')
        got = trail.saved()
        self.assertEqual([name for name, _ in got], ['nas', trail.OWN_ID])
        self.assertEqual(got[0][1], TODAY)   # the machine's newest line
        self.assertEqual(got[1][1], TODAY)   # the hub's, whole minutes only
        self.assertEqual(trail.last_entry(trail.OWN_ID), TODAY)

    def test_the_own_log_is_cut_too(self):
        with open(trail.OWN_LOG, 'w', encoding='utf-8') as f:
            f.write(line(TODAY, 'started', seconds=False) + '\n')
            f.write(line(TEN_DAYS, 'old news', seconds=False) + '\n')
        self.assertEqual(len(trail.lines(trail.OWN_ID, 1)), 1)
        self.assertEqual(len(trail.lines(trail.OWN_ID, 0)), 2)

    def test_one_machine_downloads_a_plain_log(self):
        filename, data, kind = trail.download(['nas'], 1, now=NOW)
        self.assertEqual(filename, 'nas-2026-11-13.log')
        self.assertEqual(kind, 'text/plain; charset=utf-8')
        text = data.decode()
        self.assertTrue(text.endswith('\n'))
        self.assertEqual(len(text.splitlines()), 1)  # only today's line
        self.assertIn('something happened 0', text)

    def test_nothing_is_cut_to_a_line_count(self):
        put('nas', TODAY, count=500)
        _, data, _ = trail.download(['nas'], 0, now=NOW)
        self.assertEqual(len(data.decode().splitlines()), 504)  # yesterday, two days ago, ten days ago, plus 500

    def test_several_machines_download_a_zip_of_one_log_each(self):
        put('pc', TODAY, text='pc went fine')
        filename, data, kind = trail.download(['pc', 'nas', trail.OWN_ID], 1, now=NOW)
        self.assertEqual(filename, 'home-watch-logs-2026-11-13.zip')
        self.assertEqual(kind, 'application/zip')
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertEqual(archive.namelist(), ['pc.log', 'nas.log', 'home-watch.log'])
            for name in archive.namelist():
                self.assertNotIn('old', archive.read(name).decode())

    def test_names_it_does_not_know_are_ignored(self):
        self.assertIsNone(trail.download(['not-a-machine'], 0, now=NOW))
        got = trail.download(['../history/nas', 'nas'], 0, now=NOW)
        self.assertEqual(got[0], 'nas-2026-11-13.log')


if __name__ == '__main__':
    unittest.main()
