"""Tests for the service checks' ids: the upgrade that gives every check one, and what it moves with it.

Run from addon/: `python -m unittest test_services`. Everything happens in a temporary data folder, made before
services is imported (it reads HOME_WATCH_DATA then); each test gets its own folder under it and points the module's
paths at it, so nothing here needs Home Assistant, a network or a real install.
"""

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta

DATA = tempfile.mkdtemp(prefix='home-watch-services-test-')
os.environ['HOME_WATCH_DATA'] = DATA

import services  # noqa: E402  (after the temporary data folder is set)

NOW = time.time()
HOUR_AGO = datetime.fromtimestamp(NOW - 3600)
PING = {'type': 'ping', 'host': '192.0.2.1', 'every': 60, 'retries': 0}


class Checks(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix='checks-', dir=DATA)
        services.DATA = self.folder
        services.LIST = os.path.join(self.folder, 'monitors.json')
        services.CODES = os.path.join(self.folder, 'push-codes.json')
        services.EVENTS = os.path.join(self.folder, 'services')
        services.CHECKS_DIR = os.path.join(self.folder, 'checks')
        services.monitors[:] = []
        services.status.clear()
        services.pushes.clear()
        services.samples.clear()
        services.cache.clear()
        services.codes = {}
        self.notes = []

    def note(self, text):
        self.notes.append(text)

    def write_list(self, found):
        with open(services.LIST, 'w', encoding='utf-8') as f:
            json.dump(found, f, indent=2)

    def read_list(self):
        with open(services.LIST, encoding='utf-8') as f:
            return json.load(f)

    def write_codes(self, codes):
        with open(services.CODES, 'w', encoding='utf-8') as f:
            json.dump(codes, f)

    def write_events(self, where, state='up', detail=''):
        """A row as the hub wrote it before ids: the second column is the check's name."""
        os.makedirs(services.EVENTS, exist_ok=True)
        path = os.path.join(services.EVENTS, f'{HOUR_AGO:%Y-%m}.csv')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('time,name,status,detail\n')
            f.write(f'{HOUR_AGO:%Y-%m-%d %H:%M:%S},"{where}",{state},"{detail}"\n')

    def write_checks(self, where):
        """A response-time row as the hub wrote it before ids."""
        os.makedirs(services.CHECKS_DIR, exist_ok=True)
        path = os.path.join(services.CHECKS_DIR, f'{HOUR_AGO:%Y-%m}.csv')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('time,name,ok,checks,ms\n')
            f.write(f'{HOUR_AGO:%Y-%m-%d %H:%M},{where},4,5,12\n')

    def read(self, path):
        with open(path, encoding='utf-8') as f:
            return f.read()

    def everything(self):
        """Every file in the data folder, byte for byte: what a second start must leave alone."""
        got = {}
        for base, _, names in os.walk(self.folder):
            for name in names:
                path = os.path.join(base, name)
                with open(path, 'rb') as f:
                    got[os.path.relpath(path, self.folder)] = f.read()
        return got

    def test_checks_without_an_id_get_the_next_free_number(self):
        self.write_list([{'id': 4, 'name': 'Router', **PING},
                         {'name': 'NAS', 'group': 'Devices', **PING},
                         {'name': 'Nightly backup', 'type': 'push', 'every': 7200, 'retries': 0}])
        services.load(self.note)
        self.assertEqual([m['id'] for m in services.monitors], [4, 5, 6])
        self.assertEqual(sorted(services.status), [4, 5, 6])
        got = self.read_list()
        self.assertEqual([m['id'] for m in got], [4, 5, 6])
        self.assertEqual(got[1]['group'], 'Devices')      # the rest of the entry is kept
        self.assertEqual(got[2]['type'], 'push')          # and its order
        self.assertIn('monitors.json: every check now has its id', ' '.join(self.notes))

    def test_a_second_start_changes_nothing(self):
        self.write_list([{'name': 'Router', **PING}, {'name': 'Backup', 'type': 'push', 'every': 7200}])
        self.write_codes({'Backup': 'old-code'})
        self.write_events('Backup')
        self.write_checks('Router')
        services.load(self.note)
        before = self.everything()
        services.load(self.note)
        self.assertEqual(self.everything(), before)
        self.assertEqual(self.read_list()[0]['id'], 1)
        self.assertEqual(self.read_list()[1]['id'], 2)

    def test_name_keyed_codes_and_history_move_to_the_id(self):
        self.write_list([{'name': 'Router', **PING}, {'name': 'Backup', 'type': 'push', 'every': 7200}])
        self.write_codes({'Router': 'keep-me', 'Backup': 'and-me'})
        self.write_events('Router')
        self.write_checks('Router')
        services.load(self.note)
        self.assertEqual(services.monitors[0]['id'], 1)
        self.assertEqual(services.codes, {'1': 'keep-me', '2': 'and-me'})
        self.assertEqual(services.read_codes(self.note), {'1': 'keep-me', '2': 'and-me'})
        events = self.read(os.path.join(services.EVENTS, f'{HOUR_AGO:%Y-%m}.csv'))
        self.assertEqual(events.splitlines()[0], 'time,id,status,detail')
        self.assertEqual(events.splitlines()[1].split(',')[1], '1')
        checks = self.read(os.path.join(services.CHECKS_DIR, f'{HOUR_AGO:%Y-%m}.csv'))
        self.assertEqual(checks.splitlines()[0], 'time,id,ok,checks,ms')
        self.assertEqual(checks.splitlines()[1].split(',')[1], '1')
        self.assertEqual(list(services.changes(30)), [1])          # the outage list and uptime find it by id
        self.assertEqual(len(services.points(1, 30)['t']), 1)      # and so does the response-time graph

    def test_a_renamed_check_keeps_its_history_and_push_code(self):
        self.write_list([{'name': 'Nightly backup', 'type': 'push', 'every': 7200, 'retries': 0}])
        services.load(self.note)
        cid = services.monitors[0]['id']
        code = services.codes[str(cid)]
        services.log_event(NOW, cid, 'down', 'backup failed')
        self.write_list([{'id': cid, 'name': 'Backup, renamed', 'type': 'push', 'every': 7200, 'retries': 0}])
        services.load(self.note)
        self.assertEqual(services.monitors[0]['name'], 'Backup, renamed')
        self.assertEqual(services.codes[str(cid)], code)                  # the push URL still works
        self.assertEqual([state for _, state, _ in services.changes(30)[cid]], ['down'])
        self.assertEqual(len(services.outages(30)), 1)
        self.assertEqual(services.outages(30)[0]['name'], 'Backup, renamed')  # the label follows the rename
        self.assertEqual(services.snapshot()[0]['name'], 'Backup, renamed')
        self.assertEqual(services.snapshot()[0]['id'], cid)

    def test_a_duplicate_id_is_logged_by_name_and_the_later_check_is_skipped(self):
        self.write_list([{'id': 3, 'name': 'First', **PING}, {'id': 3, 'name': 'Second', **PING}])
        services.load(self.note)
        self.assertEqual([m['name'] for m in services.monitors], ['First'])
        self.assertEqual([m['name'] for m in self.read_list()], ['First'])
        self.assertTrue(any('Second' in text and 'id 3' in text for text in self.notes), self.notes)
        self.assertEqual(sorted(services.status), [3])

    def test_a_push_is_filed_under_the_id_and_its_url_is_unchanged(self):
        self.write_list([{'name': 'Backup', 'type': 'push', 'every': 7200, 'retries': 0}])
        services.load(self.note)
        cid = services.monitors[0]['id']
        code = services.codes[str(cid)]
        self.assertTrue(services.take_push(code, {'status': ['up'], 'msg': ['done']}))
        self.assertEqual(services.pushes[cid][1], True)
        self.assertFalse(services.take_push('not-the-code', {'status': ['up']}))
        row = services.snapshot()[0]
        self.assertEqual(row['id'], cid)
        self.assertEqual(row['push'], f'/api/push/{code}')   # the code travels in the URL, not the name

    def test_a_number_still_in_stored_rows_is_not_reused(self):
        os.makedirs(services.CHECKS_DIR, exist_ok=True)
        with open(os.path.join(services.CHECKS_DIR, f'{HOUR_AGO:%Y-%m}.csv'), 'w', encoding='utf-8') as f:
            f.write('time,id,ok,checks,ms\n')
            f.write(f'{(HOUR_AGO - timedelta(days=400)):%Y-%m-%d %H:%M},9,1,1,12\n')
        self.write_list([{'name': 'A new check', **PING}])
        services.load(self.note)
        self.assertEqual(services.monitors[0]['id'], 10)


if __name__ == '__main__':
    unittest.main()
