"""Tests for the service checks: the ids the upgrade gives every check (and what it moves with it), and a machine
check with and without a host.

Run from addon/: `python -m unittest test_services`. Everything happens in a temporary data folder, made before
services is imported (it reads HOME_WATCH_DATA then); each test gets its own folder under it and points the module's
paths at it, so nothing here needs Home Assistant, a network or a real install. A machine check's ping is stubbed out,
so the host states are tested without a device on the network.
"""

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from unittest import mock

DATA = tempfile.mkdtemp(prefix='home-watch-services-test-')
os.environ['HOME_WATCH_DATA'] = DATA

import services  # noqa: E402  (after the temporary data folder is set)
import watch  # noqa: E402  (the page's API: the Checks screen's save, remove and move doors)

NOW = time.time()
HOUR_AGO = datetime.fromtimestamp(NOW - 3600)
PING = {'type': 'ping', 'host': '192.0.2.1', 'every': 60, 'retries': 0}


class Helpers(unittest.TestCase):
    """A fresh data folder and the small writers the tests below share; no tests of its own, so the API tests can use
    the same setup without running them twice."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix='checks-', dir=DATA)
        services.DATA = self.folder
        services.LIST = os.path.join(self.folder, 'monitors.json')
        services.CODES = os.path.join(self.folder, 'push-codes.json')
        services.REMOVED = os.path.join(self.folder, 'removed-ids.json')
        services.EVENTS = os.path.join(self.folder, 'services')
        services.CHECKS_DIR = os.path.join(self.folder, 'checks')
        services.monitors[:] = []
        services.status.clear()
        services.pushes.clear()
        services.samples.clear()
        services.cache.clear()
        services.codes = {}
        services.machine = None
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

class Checks(Helpers):
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

    # ---- the Checks screen: validate, save, reorder, remove, and the self-heal app that survives a rename ----

    def test_validate_says_what_is_wrong_next_to_the_field(self):
        _, errors = services.validate({'type': 'ping', 'name': 'Router'})
        self.assertIn('host', errors)
        _, errors = services.validate({'type': 'port', 'name': 'NAS', 'host': '192.0.2.1', 'port': '0'})
        self.assertIn('port', errors)
        _, errors = services.validate({'type': 'http', 'name': 'Web', 'url': 'ftp://example.com/'})
        self.assertIn('url', errors)
        _, errors = services.validate({'type': 'dns', 'name': 'DNS', 'name_to_look_up': 'example.com'})
        self.assertIn('server', errors)
        _, errors = services.validate({'type': 'dyndns', 'name': 'DDNS'})
        self.assertIn('hostname', errors)
        _, errors = services.validate({'type': 'machine', 'name': 'PC'})
        self.assertIn('machine', errors)
        _, errors = services.validate({'type': 'nope', 'name': ''})
        self.assertIn('type', errors)
        self.assertIn('name', errors)

    def test_validate_keeps_only_the_fields_the_type_reads(self):
        got, errors = services.validate({'type': 'http', 'name': 'Web', 'url': 'https://example.com/', 'expect': '204',
                                         'host': '192.0.2.1', 'port': '445', 'name_to_look_up': 'example.com',
                                         'server': '192.0.2.53', 'hostname': 'home.example.net', 'machine': 'PC',
                                         'every': '120', 'retries': '1', 'off_ok': True, 'heal': True, 'app': 'Media Hub'})
        self.assertEqual(errors, {})
        self.assertEqual(got['url'], 'https://example.com/')
        self.assertEqual(got['expect'], 204)
        self.assertEqual(got['machine'], 'pc')       # a typed machine id is made the way watch.py makes it
        self.assertEqual(got['every'], 120)
        self.assertEqual(got['retries'], 1)
        self.assertTrue(got['off_ok'])
        self.assertTrue(got['heal'])
        self.assertEqual(got['app'], 'media_hub')    # the script name's app is a slug
        for key in ('host', 'port', 'name_to_look_up', 'server', 'hostname'):
            self.assertNotIn(key, got)                # an http check reads none of these

    def test_validate_keeps_an_expected_http_status(self):
        got, errors = services.validate({'type': 'http', 'name': 'API', 'url': 'https://example.com/health', 'expect': '204'})
        self.assertEqual(errors, {})
        self.assertEqual(got['expect'], 204)
        _, errors = services.validate({'type': 'http', 'name': 'API', 'url': 'https://example.com/', 'expect': '999'})
        self.assertIn('expect', errors)

    def test_save_adds_a_check_with_a_new_id_and_writes_the_list(self):
        self.write_list([{'id': 3, 'name': 'Router', **PING}])
        services.load(self.note)
        row, errors = services.save_check({'name': 'NAS web', 'group': 'Services', 'type': 'http',
                                           'url': 'http://192.0.2.10/', 'every': '60', 'retries': '2'}, self.note)
        self.assertEqual(errors, {})
        self.assertEqual(row['id'], 4)
        got = self.read_list()
        self.assertEqual([m['name'] for m in got], ['Router', 'NAS web'])
        self.assertEqual(got[1]['id'], 4)
        self.assertEqual(got[1]['url'], 'http://192.0.2.10/')
        self.assertEqual(services.snapshot()[-1]['name'], 'NAS web')  # the running checks picked it up

    def test_save_edits_a_check_and_keeps_its_id_and_place(self):
        self.write_list([{'id': 1, 'name': 'First', **PING}, {'id': 2, 'name': 'Second', **PING}])
        services.load(self.note)
        row, errors = services.save_check({'id': 2, 'name': 'Renamed', 'group': 'Devices', 'type': 'ping',
                                           'host': '192.0.2.9', 'every': 30, 'retries': 1}, self.note)
        self.assertEqual(errors, {})
        self.assertEqual(row['id'], 2)
        got = self.read_list()
        self.assertEqual([m['name'] for m in got], ['First', 'Renamed'])
        self.assertEqual(got[1]['every'], 30)
        self.assertEqual(got[1]['retries'], 1)

    def test_save_refuses_a_check_that_is_not_there(self):
        self.write_list([{'id': 1, 'name': 'First', **PING}])
        services.load(self.note)
        row, errors = services.save_check({'id': 99, 'name': 'X', 'type': 'ping', 'host': '192.0.2.1'}, self.note)
        self.assertIsNone(row)
        self.assertIn('id', errors)
        self.assertEqual([m['id'] for m in self.read_list()], [1])

    def test_save_makes_a_push_code_and_returns_the_url_and_curl(self):
        self.write_list([{'id': 1, 'name': 'Router', **PING}])
        services.load(self.note)
        row, errors = services.save_check({'name': 'Backup', 'type': 'push', 'every': '7200', 'retries': '0'}, self.note)
        self.assertEqual(errors, {})
        code = services.read_codes(self.note)[str(row['id'])]
        self.assertEqual(row['push'], f'/api/push/{code}')
        self.assertEqual(row['url'], f'http://homeassistant.local:8790/api/push/{code}')
        self.assertIn(f'curl "http://homeassistant.local:8790/api/push/{code}', row['curl'])
        self.assertEqual(row['every'], 7200)

    def test_a_new_code_changes_the_push_url(self):
        self.write_list([{'id': 1, 'name': 'Backup', 'type': 'push', 'every': 7200}])
        services.load(self.note)
        old = services.codes['1']
        row = services.new_code(1, self.note)
        self.assertNotEqual(row['push'], f'/api/push/{old}')
        self.assertEqual(row['push'], f"/api/push/{services.read_codes(self.note)['1']}")
        self.assertIn(row['url'], row['curl'])
        self.assertIsNone(services.new_code(99, self.note))   # not a check any more

    def test_changing_a_push_check_away_from_push_drops_its_code(self):
        self.write_list([{'id': 1, 'name': 'Backup', 'type': 'push', 'every': 7200}])
        services.load(self.note)
        self.assertIn('1', services.codes)
        row, errors = services.save_check({'id': 1, 'name': 'Backup', 'type': 'ping', 'host': '192.0.2.1',
                                           'every': 60, 'retries': 2}, self.note)
        self.assertEqual(errors, {})
        self.assertNotIn('1', services.read_codes(self.note))
        self.assertNotIn('push', row)

    def test_move_reorders_the_list_which_is_the_page_order(self):
        self.write_list([{'id': 1, 'name': 'One', **PING}, {'id': 2, 'name': 'Two', **PING},
                         {'id': 3, 'name': 'Three', **PING}])
        services.load(self.note)
        self.assertTrue(services.move_check(3, -1, self.note))
        self.assertEqual([m['id'] for m in self.read_list()], [1, 3, 2])
        self.assertTrue(services.move_check(1, 1, self.note))
        self.assertEqual([m['id'] for m in self.read_list()], [3, 1, 2])
        self.assertTrue(services.move_check(3, -1, self.note))   # already first: nothing to do
        self.assertEqual([m['id'] for m in self.read_list()], [3, 1, 2])
        self.assertFalse(services.move_check(99, 1, self.note))

    def test_remove_drops_the_check_its_history_code_and_number(self):
        self.write_list([{'id': 1, 'name': 'Keep', **PING}, {'id': 2, 'name': 'Backup', 'type': 'push', 'every': 7200}])
        services.load(self.note)
        services.log_event(NOW, 2, 'down', 'backup failed')
        os.makedirs(services.CHECKS_DIR, exist_ok=True)
        with open(os.path.join(services.CHECKS_DIR, f'{HOUR_AGO:%Y-%m}.csv'), 'w', encoding='utf-8') as f:
            f.write('time,id,ok,checks,ms\n')
            f.write(f'{HOUR_AGO:%Y-%m-%d %H:%M},1,5,5,10\n')
            f.write(f'{HOUR_AGO:%Y-%m-%d %H:%M},2,4,5,12\n')
        self.assertTrue(services.remove_check(2, self.note))
        self.assertEqual([m['id'] for m in self.read_list()], [1])
        self.assertNotIn('2', services.codes)
        self.assertNotIn('2', services.read_codes(self.note))
        events = self.read(os.path.join(services.EVENTS, f'{HOUR_AGO:%Y-%m}.csv'))
        self.assertNotIn(',2,', events)
        checks = self.read(os.path.join(services.CHECKS_DIR, f'{HOUR_AGO:%Y-%m}.csv'))
        self.assertEqual(len(checks.splitlines()), 2)          # the header and check 1
        self.assertEqual(services.removed_ids(), {2})
        row, errors = services.save_check({'name': 'New', 'type': 'ping', 'host': '192.0.2.1'}, self.note)
        self.assertEqual(errors, {})
        self.assertEqual(row['id'], 3)                         # the removed number is never handed out again
        self.assertFalse(services.remove_check(2, self.note))

    def test_a_self_heal_check_without_an_app_gets_its_slugged_name_written_back(self):
        self.write_list([{'id': 1, 'name': 'Media Hub', 'type': 'ping', 'host': '192.0.2.1', 'heal': True}])
        services.load(self.note)
        self.assertEqual(self.read_list()[0]['app'], 'media_hub')
        self.assertIn('keeps its app', ' '.join(self.notes))

    def test_renaming_a_self_heal_check_keeps_its_app_and_script(self):
        self.write_list([{'id': 1, 'name': 'Media Hub', 'type': 'ping', 'host': '192.0.2.1', 'heal': True}])
        services.load(self.note)
        self.write_list([{'id': 1, 'name': 'Living room hub', 'type': 'ping', 'host': '192.0.2.1', 'heal': True,
                          'app': 'media_hub'}])
        services.load(self.note)
        self.assertEqual(self.read_list()[0]['app'], 'media_hub')   # the app, and so the script, follows the id
        self.assertEqual(services.app_name(services.monitors[0]), 'media_hub')
        row, errors = services.save_check({'id': 1, 'name': 'Living room hub', 'type': 'ping', 'host': '192.0.2.1',
                                           'heal': True, 'app': 'media_hub'}, self.note)
        self.assertEqual(errors, {})
        self.assertEqual(row['app'], 'media_hub')

    def test_http_expect_requires_that_status(self):
        class Reply:
            status = 204
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
        with mock.patch.object(services.urllib.request, 'urlopen', return_value=Reply()):
            self.assertEqual(services.check_http({'url': 'https://example.com/', 'expect': 204})[:2], (True, ''))
            ok, detail, _ = services.check_http({'url': 'https://example.com/', 'expect': 200})
        self.assertFalse(ok)
        self.assertIn('expected 200', detail)

    # ---- a machine check: with no host it only watches the reports; with one it also pings the address ----

    def reported(self, ago, problems=None):
        """What services.machine hands back for a machine's report, `ago` seconds old."""
        services.machine = lambda mid: {'last': time.time() - ago, 'problems': problems or {}}

    def machine_check(self, **extra):
        return {'id': 1, 'type': 'machine', 'machine': 'pc', 'quiet': 900, **extra}

    def test_a_machine_check_without_a_host_only_watches_its_reports(self):
        self.reported(60)
        self.assertEqual(services.check_machine(self.machine_check()), (True, '', 0))
        self.reported(3600)
        ok, detail, ms = services.check_machine(self.machine_check())
        self.assertFalse(ok)
        self.assertEqual(detail, 'no report for 60 min')
        self.assertEqual(ms, 0)                       # nothing was pinged, so there is no response time

    def test_a_machine_check_with_a_host_is_down_when_the_host_does_not_answer(self):
        self.reported(60)                             # the reporter is fine: the address is what fails
        with mock.patch.object(services, 'check_ping', return_value=(False, 'no answer', 3010.5)) as ping:
            got = services.check_machine(self.machine_check(host='192.0.2.1'))
        self.assertEqual(got, (False, 'Not on the network', 3010.5))
        self.assertEqual(ping.call_args[0][0]['host'], '192.0.2.1')  # pinged the check's own address

    def test_a_machine_check_with_a_host_is_down_when_only_its_reporter_stopped(self):
        self.reported(3600)                           # the address answers, the report is stale
        with mock.patch.object(services, 'check_ping', return_value=(True, '', 4.5)):
            got = services.check_machine(self.machine_check(host='192.0.2.1'))
        self.assertEqual(got, (False, 'On the network, but its reporter stopped sending', 4.5))

    def test_a_machine_check_with_a_host_is_up_when_both_are_fine(self):
        self.reported(60)
        with mock.patch.object(services, 'check_ping', return_value=(True, '', 4.5)):
            got = services.check_machine(self.machine_check(host='192.0.2.1'))
        self.assertEqual(got, (True, '', 4.5))         # the response time is the ping's
        self.reported(60, {'drive': 'Drive health is bad'})  # a problem in the report still counts
        with mock.patch.object(services, 'check_ping', return_value=(True, '', 4.5)):
            got = services.check_machine(self.machine_check(host='192.0.2.1'))
        self.assertEqual(got, (False, 'Drive health is bad', 4.5))

    def test_a_machine_check_with_a_host_waits_out_its_retries_before_down(self):
        self.write_list([{'name': 'Workstation', 'type': 'machine', 'machine': 'pc', 'host': '192.0.2.1',
                          'quiet': 900, 'every': 60, 'retries': 2}])
        services.load(self.note)
        self.reported(60)
        self.assertEqual(services.snapshot()[0]['host'], '192.0.2.1')  # so the check's page can show the address
        with mock.patch.object(services, 'check_ping', return_value=(False, 'no answer', 3000.0)):
            for expected in ('pending', 'pending', 'down'):
                services.run_one(services.monitors[0], {}, self.note)
                self.assertEqual(services.status[1]['state'], expected)
                self.assertEqual(services.status[1]['detail'], 'Not on the network')
        self.assertEqual(services.status[1]['ms'], 3000)  # and the check's page shows the ping time


    # ---- a note check: a note in one of the machine reports, read as plain substrings ----

    def reported_note(self, ago, found):
        services.machine = lambda mid: {'last': time.time() - ago, 'notes': found}

    def note_check(self, **extra):
        return {'id': 1, 'type': 'note', 'machine': 'pc', 'label': 'Backup', 'every': 60, 'retries': 0, **extra}

    def test_a_note_check_is_up_when_the_note_says_what_it_should(self):
        self.reported_note(60, {'Backup': 'last run 2 hours ago, BACKUP OK'})
        self.assertEqual(services.check_note(self.note_check(good_text='backup ok')), (True, '', 0))
        self.assertEqual(services.check_note(self.note_check()), (True, '', 0))

    def test_a_note_check_is_down_when_the_bad_text_is_in_it(self):
        self.reported_note(60, {'Backup': 'last run failed'})
        ok, detail, ms = services.check_note(self.note_check(good_text='backup', bad_text='FAILED'))
        self.assertFalse(ok)
        self.assertEqual(detail, 'last run failed')
        self.assertEqual(ms, 0)

    def test_a_note_check_is_down_when_the_good_text_is_missing(self):
        self.reported_note(60, {'Backup': 'last run 2 hours ago'})
        ok, detail, _ = services.check_note(self.note_check(good_text='ok'))
        self.assertFalse(ok)
        self.assertEqual(detail, 'last run 2 hours ago')

    def test_bad_text_wins_over_good_text(self):
        self.reported_note(60, {'Backup': 'backup ok, but upload failed'})
        self.assertFalse(services.check_note(self.note_check(good_text='ok', bad_text='failed'))[0])
        self.reported_note(60, {'Backup': 'backup ok, upload fine'})
        self.assertTrue(services.check_note(self.note_check(good_text='ok', bad_text='failed'))[0])

    def test_a_missing_note_is_down(self):
        self.reported_note(60, {'Other': 'fine'})
        self.assertEqual(services.check_note(self.note_check(good_text='ok')), (False, 'No note called Backup', 0))

    def test_a_stale_report_makes_a_note_check_down(self):
        self.reported_note(3600, {'Backup': 'backup ok'})
        ok, detail, _ = services.check_note(self.note_check(good_text='ok'))
        self.assertFalse(ok)
        self.assertEqual(detail, "pc hasn't reported for 60 minutes")

    def test_a_note_check_walks_its_retries_and_can_be_off(self):
        self.write_list([{'name': 'Backup result', 'type': 'note', 'machine': 'pc', 'label': 'Backup',
                          'bad_text': 'failed', 'every': 60, 'retries': 2, 'off_ok': True}])
        services.load(self.note)
        self.reported_note(60, {'Backup': 'backup failed'})
        self.assertEqual(services.snapshot()[0]['machine'], 'pc')
        for expected in ('pending', 'pending', 'off'):
            services.run_one(services.monitors[0], {}, self.note)
            self.assertEqual(services.status[1]['state'], expected)
            self.assertEqual(services.status[1]['detail'], 'backup failed')

    def test_validate_a_note_check(self):
        got, errors = services.validate({'type': 'note', 'name': 'Backup result', 'machine': 'PC',
                                         'label': 'Backup', 'good_text': 'ok', 'bad_text': 'failed'})
        self.assertEqual(errors, {})
        self.assertEqual(got['machine'], 'pc')
        self.assertEqual(got['label'], 'Backup')
        self.assertEqual(got['good_text'], 'ok')
        self.assertEqual(got['bad_text'], 'failed')
        _, errors = services.validate({'type': 'note', 'name': 'Backup result'})
        self.assertIn('machine', errors)
        self.assertIn('label', errors)
        self.assertIn('good_text', errors)
        _, errors = services.validate({'type': 'note', 'name': 'Backup result', 'machine': 'pc',
                                       'label': 'Backup', 'bad_text': 'failed'})
        self.assertEqual(errors, {})


class Api(Helpers):
    """The Checks screen's doors: anyone allowed may look, but only Home Assistant's ingress (a signed-in panel
    request, which the Supervisor marks with X-Remote-User-Id) may change a check."""

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

    def test_the_checks_list_is_readable_from_the_lan_but_not_editable(self):
        self.write_list([{'id': 1, 'name': 'Router', **PING}])
        services.load(self.note)
        status, got = self.call('/api/checks')
        self.assertEqual(status, 200)
        self.assertFalse(got['can_edit'])
        self.assertEqual([c['name'] for c in got['checks']], ['Router'])
        self.assertEqual(got['groups'], [])
        status, got = self.call('/api/checks', ingress=True)
        self.assertEqual(status, 200)
        self.assertTrue(got['can_edit'])

    def test_a_change_without_the_ingress_header_is_refused(self):
        self.write_list([{'id': 1, 'name': 'Router', **PING}])
        services.load(self.note)
        before = self.everything()
        status, got = self.call('/api/checks/save', {'name': 'New', 'type': 'ping', 'host': '192.0.2.1'})
        self.assertEqual(status, 403)
        self.assertIn('panel', got['error'])
        self.assertEqual(self.everything(), before)             # nothing was written
        self.assertEqual([m['name'] for m in self.read_list()], ['Router'])

    def test_save_from_ingress_adds_a_check_and_returns_its_push_url(self):
        self.write_list([{'id': 1, 'name': 'Router', **PING}])
        services.load(self.note)
        status, got = self.call('/api/checks/save', {'name': 'Backup', 'group': 'Housekeeping', 'type': 'push',
                                                    'every': '7200', 'retries': '0'}, ingress=True)
        self.assertEqual(status, 200)
        self.assertTrue(got['ok'])
        self.assertTrue(got['push'].startswith('/api/push/'))
        self.assertIn('curl', got)
        self.assertEqual(got['check']['name'], 'Backup')
        self.assertEqual([m['name'] for m in self.read_list()], ['Router', 'Backup'])

    def test_bad_fields_come_back_next_to_the_field(self):
        self.write_list([{'id': 1, 'name': 'Router', **PING}])
        services.load(self.note)
        status, got = self.call('/api/checks/save', {'name': 'Broken', 'type': 'ping'}, ingress=True)
        self.assertEqual(status, 400)
        self.assertIn('host', got['fields'])
        self.assertEqual([m['name'] for m in self.read_list()], ['Router'])

    def test_remove_from_ingress_drops_the_check_and_its_history(self):
        self.write_list([{'id': 1, 'name': 'Router', **PING}])
        services.load(self.note)
        services.log_event(NOW, 1, 'down', 'no answer')
        status, got = self.call('/api/checks/remove', {'id': 1}, ingress=True)
        self.assertEqual(status, 200)
        self.assertTrue(got['ok'])
        self.assertEqual(self.read_list(), [])
        self.assertNotIn(1, services.changes(30))
        self.assertEqual(services.removed_ids(), {1})

    def test_move_from_ingress_reorders_the_checks(self):
        self.write_list([{'id': 1, 'name': 'One', **PING}, {'id': 2, 'name': 'Two', **PING}])
        services.load(self.note)
        status, _ = self.call('/api/checks/move', {'id': 2, 'by': -1}, ingress=True)
        self.assertEqual(status, 200)
        self.assertEqual([m['id'] for m in self.read_list()], [2, 1])
        status, _ = self.call('/api/checks/move', {'id': 2, 'by': -1})   # the LAN may not move it
        self.assertEqual(status, 403)
        self.assertEqual([m['id'] for m in self.read_list()], [2, 1])


if __name__ == '__main__':
    unittest.main()
