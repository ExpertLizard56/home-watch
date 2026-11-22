"""Tests for the page's Machines screen: saving a name and limits to settings.json, the validation, forgetting a
machine and its stored data, and the ingress-only rule for changes.

Run from addon/: `python -m unittest test_machines`. Everything happens in a temporary data folder, made before watch is
imported (its modules read HOME_WATCH_DATA then); each test gets its own folder under it and points the paths at it, so
nothing here needs Home Assistant, a network or a real install.
"""

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

DATA = tempfile.mkdtemp(prefix='home-watch-machines-test-')
os.environ['HOME_WATCH_DATA'] = DATA

import history  # noqa: E402  (after the temporary data folder is set)
import trail  # noqa: E402
import watch  # noqa: E402


class Helpers(unittest.TestCase):
    """A fresh data folder and the small writers the tests below share; no tests of its own, so the API tests can use
    the same setup without running them twice."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix='machines-', dir=DATA)
        watch.SETTINGS_FILE = os.path.join(self.folder, 'settings.json')
        watch.STATE_FILE = os.path.join(self.folder, 'state.json')
        watch.LOG_FILE = os.path.join(self.folder, 'log.txt')
        history.HISTORY = os.path.join(self.folder, 'history')
        trail.PROGRAMS = os.path.join(self.folder, 'programs')
        trail.LOGS = os.path.join(self.folder, 'logs')
        watch.state.clear()
        watch.state.update({'machines': {}, 'sent': {}, 'raw': {}})
        watch.order = []
        watch.labels = {}
        watch.limits = {}
        watch.networks = watch.DEFAULT_NETWORKS
        watch.senders.clear()
        watch.options.clear()
        self.notes = []

    def note(self, text):
        self.notes.append(text)

    def write_settings(self, got):
        with open(watch.SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(got, f, indent=2)

    def read_settings(self):
        with open(watch.SETTINGS_FILE, encoding='utf-8') as f:
            return json.load(f)

    def read(self):
        with open(watch.SETTINGS_FILE, encoding='utf-8') as f:
            return f.read()

    def report(self, mid, name='', ago=60):
        """A machine as a report leaves it in the hub's state, `ago` seconds old."""
        watch.state['machines'][mid] = {'name': name or mid, 'last': time.time() - ago, 'metrics': {},
                                        'problems': {}, 'kind': 'linux'}

    def put(self, folder, name):
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, name), 'w', encoding='utf-8') as f:
            f.write('kept\n')

    def names(self, folder):
        return sorted(os.listdir(folder)) if os.path.isdir(folder) else []


class Machines(Helpers):
    def test_the_screen_lists_listed_and_reported_machines_in_order(self):
        self.write_settings({'machines': {'nas': 'NAS'}, 'limits': {'nas': {'temp': 90}}})
        watch.load_settings(self.note)
        self.report('pc', 'This PC')
        body = watch.machines_payload(False)
        self.assertEqual([m['id'] for m in body['machines']], ['nas', 'pc'])
        self.assertEqual(body['machines'][0]['name'], 'NAS')
        self.assertEqual(body['machines'][0]['limits'], {'temp': 90.0})
        self.assertFalse(body['machines'][0]['reported'])
        self.assertEqual(body['machines'][1]['name'], 'This PC')   # the name it reports, no label set
        self.assertTrue(body['machines'][1]['reported'])
        self.assertEqual(body['defaults'], watch.LIMITS)           # the form's hints
        self.assertFalse(body['can_edit'])
        self.assertTrue(watch.machines_payload(True)['can_edit'])

    def test_save_renames_and_sets_limits_keeping_every_other_key(self):
        self.write_settings({'machines': {'nas': 'NAS', 'pc': ''},
                             'limits': {'nas': {'disk_used_pct': 85}},
                             'log_senders': {'192.0.2.10': 'nas'},
                             'allowed_networks': ['192.0.2.'],
                             'heal_script': 'script.heal_{app}'})
        watch.load_settings(self.note)
        self.report('pc', 'This PC')
        row, errors = watch.save_machine({'id': 'pc', 'name': 'Workstation',
                                          'limits': {'temp': '95', 'cpu_busy': '40'}}, self.note)
        self.assertEqual(errors, {})
        self.assertEqual(row['name'], 'Workstation')
        self.assertEqual(row['limits'], {'temp': 95, 'cpu_busy': 40})
        got = self.read_settings()
        self.assertEqual(list(got['machines']), ['nas', 'pc'])     # the order is kept
        self.assertEqual(got['machines']['pc'], 'Workstation')
        self.assertEqual(got['limits'], {'nas': {'disk_used_pct': 85}, 'pc': {'temp': 95, 'cpu_busy': 40}})
        self.assertEqual(got['log_senders'], {'192.0.2.10': 'nas'})
        self.assertEqual(got['allowed_networks'], ['192.0.2.'])
        self.assertEqual(got['heal_script'], 'script.heal_{app}')
        # in use at once, without a restart
        self.assertEqual(watch.order, ['nas', 'pc'])
        self.assertEqual(watch.labels['pc'], 'Workstation')
        self.assertEqual(watch.limit_for('pc', 'temp'), 95)
        self.assertEqual(watch.limit_for('pc', 'mem'), watch.LIMITS['mem'])

    def test_a_blank_limit_falls_back_to_the_default(self):
        self.write_settings({'machines': {'pc': 'PC'}, 'limits': {'pc': {'temp': 95, 'mem': 90}}})
        watch.load_settings(self.note)
        self.report('pc', 'PC')
        row, errors = watch.save_machine({'id': 'pc', 'name': 'PC', 'limits': {'temp': '', 'mem': None}}, self.note)
        self.assertEqual(errors, {})
        self.assertEqual(row['limits'], {})
        self.assertNotIn('pc', self.read_settings().get('limits', {}))
        self.assertEqual(watch.limit_for('pc', 'temp'), watch.LIMITS['temp'])
        self.assertEqual(watch.limit_for('pc', 'mem'), watch.LIMITS['mem'])

    def test_a_machine_that_reported_but_is_not_listed_is_added_last(self):
        self.write_settings({'machines': {'nas': 'NAS'}})
        watch.load_settings(self.note)
        self.report('pc', 'This PC')
        row, errors = watch.save_machine({'id': 'pc', 'name': '', 'limits': {}}, self.note)
        self.assertEqual(errors, {})
        self.assertEqual(self.read_settings()['machines'], {'nas': 'NAS', 'pc': ''})
        self.assertEqual(row['name'], 'This PC')                  # no label: the name it reports
        self.assertEqual(watch.order, ['nas', 'pc'])

    def test_validate_says_what_is_wrong_next_to_the_field(self):
        known = ['pc']
        _, errors = watch.validate_machine({'id': 'pc', 'name': 'x' * 61, 'limits': {'temp': '200'}}, known)
        self.assertIn('name', errors)
        self.assertIn('temp', errors)
        _, errors = watch.validate_machine({'id': 'pc', 'limits': {'mem': 'lots', 'disk_used_pct': '0',
                                                                   'cpu_busy': '101'}}, known)
        for key in ('mem', 'disk_used_pct', 'cpu_busy'):
            self.assertIn(key, errors)
        self.assertIn('id', watch.validate_machine({'id': 'nope'}, known)[1])
        self.assertIn('id', watch.validate_machine({}, known)[1])
        found, errors = watch.validate_machine({'id': 'PC', 'name': ' Workstation ',
                                                'limits': {'temp': '82.5'}}, known)
        self.assertEqual(errors, {})
        self.assertEqual(found, {'id': 'pc', 'label': 'Workstation', 'limits': {'temp': 82.5}})

    def test_move_reorders_the_page_order(self):
        self.write_settings({'machines': {'one': 'One', 'two': 'Two', 'three': 'Three'}})
        watch.load_settings(self.note)
        self.assertTrue(watch.move_machine('three', -1, self.note))
        self.assertEqual(list(self.read_settings()['machines']), ['one', 'three', 'two'])
        self.assertTrue(watch.move_machine('one', 1, self.note))
        self.assertEqual(list(self.read_settings()['machines']), ['three', 'one', 'two'])
        self.assertTrue(watch.move_machine('three', -1, self.note))   # already first: nothing to do
        self.assertEqual(list(self.read_settings()['machines']), ['three', 'one', 'two'])
        self.assertFalse(watch.move_machine('nope', 1, self.note))
        self.assertEqual(watch.order, ['three', 'one', 'two'])         # the page's order, at once

    def test_forget_drops_the_machine_its_data_and_its_settings_entry(self):
        self.write_settings({'machines': {'nas': 'NAS', 'pc': 'PC'},
                             'limits': {'nas': {'temp': 90}, 'pc': {'mem': 90}},
                             'log_senders': {'192.0.2.10': 'nas', '192.0.2.20': 'pc'},
                             'heal_script': 'script.heal_{app}'})
        watch.load_settings(self.note)
        self.report('nas', 'NAS')
        self.report('pc', 'PC')
        watch.state['sent'].update({'nas-hot': 1.0, 'pc-hot': 1.0})
        watch.state['problems'] = {'nas-hot': ['NAS', 'hot'], 'pc-hot': ['PC', 'hot']}
        for mid in ('nas', 'pc'):
            self.put(history.HISTORY, f'{mid} 2026-11.csv')
            self.put(trail.PROGRAMS, f'{mid} 2026-11.csv')
            self.put(trail.LOGS, f'{mid} 2026-11.log')
        self.assertTrue(watch.forget_machine('nas', self.note))
        got = self.read_settings()
        self.assertEqual(list(got['machines']), ['pc'])
        self.assertEqual(got['limits'], {'pc': {'mem': 90}})
        self.assertEqual(got['log_senders'], {'192.0.2.20': 'pc'})
        self.assertEqual(got['heal_script'], 'script.heal_{app}')
        self.assertEqual(self.names(history.HISTORY), ['pc 2026-11.csv'])
        self.assertEqual(self.names(trail.PROGRAMS), ['pc 2026-11.csv'])
        self.assertEqual(self.names(trail.LOGS), ['pc 2026-11.log'])
        self.assertNotIn('nas', watch.state['machines'])
        self.assertEqual(list(watch.state['sent']), ['pc-hot'])
        self.assertEqual(list(watch.state['problems']), ['pc-hot'])
        self.assertEqual(watch.order, ['pc'])
        self.assertFalse(watch.forget_machine('nas', self.note))       # it has gone

    def test_a_broken_settings_file_is_left_alone(self):
        with open(watch.SETTINGS_FILE, 'w', encoding='utf-8') as f:
            f.write('{not json')
        self.report('pc', 'PC')
        row, errors = watch.save_machine({'id': 'pc', 'name': 'PC', 'limits': {}}, self.note)
        self.assertIsNone(row)
        self.assertIn('hand', errors['id'])
        self.assertEqual(self.read(), '{not json')
        self.assertFalse(watch.forget_machine('pc', self.note))


class Notes(Helpers):
    """A report's short information lines: kept with the machine for its page, and information only."""

    def test_a_report_keeps_its_notes_and_a_later_one_without_them_clears_them(self):
        watch.take_report({'machine': 'pc', 'notes': {'Backup': 'last run 2 hours ago'}})
        self.assertEqual(watch.machine_report('pc')['notes'], {'Backup': 'last run 2 hours ago'})
        watch.take_report({'machine': 'pc', 'notes': {'Backup': 'running now'}})
        self.assertEqual(watch.machine_report('pc')['notes'], {'Backup': 'running now'})   # replaced, not merged
        watch.take_report({'machine': 'pc'})
        self.assertNotIn('notes', watch.machine_report('pc'))

    def test_notes_are_cut_to_their_limits_and_non_strings_dropped(self):
        got = watch.clean_notes({'L' * 60: 'x' * 400, 'count': 3, 'flag': True, 7: 'seven', 'ok': 'Fine'})
        self.assertEqual(got, {'L' * 40: 'x' * 300, 'ok': 'Fine'})
        self.assertEqual(watch.clean_notes([('label', 'text')]), {})        # not an object: nothing to keep
        self.assertEqual(len(watch.clean_notes({f'n{n}': 'v' for n in range(20)})), 12)

    def test_notes_never_become_a_problem_or_an_alert(self):
        watch.take_report({'machine': 'pc', 'metrics': {'temp': 40, 'mem': 10},
                           'notes': {'hot': 'Warm in here', 'space': 'Room to spare'}})
        self.assertEqual(watch.machine_report('pc')['notes'], {'hot': 'Warm in here', 'space': 'Room to spare'})
        self.assertEqual(watch.current_problems(), {})


class DriveChecks(Helpers):
    """The drive's own numbers: a temperature counts only after DRIVE_HOT_FOR over the limit, wear as soon as it's at
    the limit, and both follow a machine's own limits."""

    def test_a_hot_drive_is_a_problem_only_after_it_stays_hot(self):
        watch.take_report({'machine': 'pc', 'metrics': {'drive_temp': 75}})
        self.assertEqual(watch.current_problems(), {})                       # just warmed up: not yet
        watch.state['machines']['pc']['drive_hot_since'] -= watch.DRIVE_HOT_FOR
        self.assertIn('pc-drive-hot', watch.current_problems())
        watch.take_report({'machine': 'pc', 'metrics': {'drive_temp': 50}})  # cooled down: the clock starts over
        self.assertNotIn('drive_hot_since', watch.state['machines']['pc'])
        self.assertEqual(watch.current_problems(), {})

    def test_wear_at_the_limit_is_a_problem_and_a_machine_may_move_the_limit(self):
        watch.take_report({'machine': 'pc', 'metrics': {'drive_wear': 69}})
        self.assertEqual(watch.current_problems(), {})
        watch.take_report({'machine': 'pc', 'metrics': {'drive_wear': 70}})
        self.assertIn('70% of its life', watch.current_problems()['pc-drive-wear'][1])
        watch.limits = {'pc': {'drive_wear': 90, 'drive_temp': 80}}
        self.assertEqual(watch.current_problems(), {})
        watch.take_report({'machine': 'pc', 'metrics': {'drive_wear': 70, 'drive_temp': 75}})
        self.assertNotIn('drive_hot_since', watch.state['machines']['pc'])

    def test_the_drive_limits_are_on_the_machines_form(self):
        with open(watch.PAGE, encoding='utf-8') as f:
            page = f.read()
        for key in ('drive_temp', 'drive_wear'):
            self.assertIn(key, watch.MACHINE_LIMITS)
            self.assertIn(f'id="mf_{key}"', page)


class Api(Helpers):
    """The Machines screen's doors: anyone allowed may look, but only Home Assistant's ingress (a signed-in panel
    request, which the Supervisor marks with X-Remote-User-Id) may change anything."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), watch.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, path, body=None, ingress=False):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}', data=data,
                                     method='POST' if data is not None else 'GET')
        if data is not None:
            req.add_header('Content-Type', 'application/json')
        if ingress:
            req.add_header('X-Remote-User-Id', 'test-user')
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b'{}')
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b'{}')
            finally:
                e.close()

    def test_the_machines_list_is_readable_from_the_lan_but_not_editable(self):
        self.write_settings({'machines': {'nas': 'NAS'}})
        watch.load_settings(self.note)
        status, got = self.call('/api/machines')
        self.assertEqual(status, 200)
        self.assertFalse(got['can_edit'])
        self.assertEqual([m['id'] for m in got['machines']], ['nas'])
        status, got = self.call('/api/machines', ingress=True)
        self.assertEqual(status, 200)
        self.assertTrue(got['can_edit'])

    def test_a_change_without_the_ingress_header_is_refused(self):
        self.write_settings({'machines': {'nas': 'NAS'}})
        watch.load_settings(self.note)
        before = self.read()
        for path, body in (('/api/machines/save', {'id': 'nas', 'name': 'Changed', 'limits': {}}),
                           ('/api/machines/move', {'id': 'nas', 'by': 1}),
                           ('/api/machines/forget', {'id': 'nas'})):
            status, got = self.call(path, body)
            self.assertEqual(status, 403, path)
            self.assertIn('panel', got['error'])
        self.assertEqual(self.read(), before)                       # nothing was written
        self.assertEqual(watch.order, ['nas'])

    def test_save_from_ingress_writes_the_name_and_limits(self):
        self.write_settings({'machines': {'nas': 'NAS'}})
        watch.load_settings(self.note)
        self.report('nas', 'NAS')
        status, got = self.call('/api/machines/save', {'id': 'nas', 'name': 'Storage',
                                                       'limits': {'disk_used_pct': '85'}}, ingress=True)
        self.assertEqual(status, 200)
        self.assertTrue(got['ok'])
        self.assertEqual(got['machine']['name'], 'Storage')
        self.assertEqual(self.read_settings()['machines']['nas'], 'Storage')
        self.assertEqual(self.read_settings()['limits']['nas'], {'disk_used_pct': 85})

    def test_bad_fields_come_back_next_to_the_field(self):
        self.write_settings({'machines': {'nas': 'NAS'}})
        watch.load_settings(self.note)
        status, got = self.call('/api/machines/save', {'id': 'nas', 'name': 'NAS',
                                                       'limits': {'temp': '200'}}, ingress=True)
        self.assertEqual(status, 400)
        self.assertIn('temp', got['fields'])
        self.assertEqual(self.read_settings()['machines']['nas'], 'NAS')

    def test_forget_from_ingress_drops_the_machine_and_its_data(self):
        self.write_settings({'machines': {'nas': 'NAS'}})
        watch.load_settings(self.note)
        self.report('nas', 'NAS')
        self.put(history.HISTORY, 'nas 2026-11.csv')
        status, got = self.call('/api/machines/forget', {'id': 'nas'}, ingress=True)
        self.assertEqual(status, 200)
        self.assertTrue(got['ok'])
        self.assertEqual(self.read_settings()['machines'], {})
        self.assertEqual(self.names(history.HISTORY), [])
        self.assertNotIn('nas', watch.state['machines'])


if __name__ == '__main__':
    unittest.main()



