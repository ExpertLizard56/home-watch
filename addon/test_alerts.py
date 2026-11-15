"""Tests for the phone notices: the quiet file, the phone list, and the notice itself.

Run from addon/: `python -m unittest test_alerts`. The Supervisor's HTTP calls are mocked, so nothing here needs Home
Assistant, a network or a real install; the data folder is a temporary one, made before watch is imported (its modules
read HOME_WATCH_DATA then), and each test gets its own quiet file under it.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

DATA = tempfile.mkdtemp(prefix='home-watch-alerts-test-')
os.environ['HOME_WATCH_DATA'] = DATA

import watch  # noqa: E402  (after the temporary data folder is set)


class Reply:
    """What urlopen returns, as much of it as the add-on reads: read() and a context manager."""

    def __init__(self, body=None):
        self.body = json.dumps(body).encode() if body is not None else b''

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Helpers(unittest.TestCase):
    """A fresh data folder, a quiet file of the test's own and a log the test can read; no tests of its own."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix='alerts-', dir=DATA)
        watch.QUIET_FILE = os.path.join(self.folder, 'phone-quiet-until.txt')
        watch.options.clear()
        self.notes = []
        logged = mock.patch.object(watch, 'note', self.notes.append)
        logged.start()
        self.addCleanup(logged.stop)

    def quiet(self, text):
        with open(watch.QUIET_FILE, 'w', encoding='utf-8') as f:
            f.write(text)

    def time_with_zone(self, hours):
        """An ISO time with a zone, `hours` from now (negative for the past)."""
        return (datetime.now().astimezone() + timedelta(hours=hours)).isoformat(timespec='seconds')

    def sent(self):
        """Send one notice; return (what send() said, [(url, method, body as JSON), ...])."""
        with mock.patch.object(watch.urllib.request, 'urlopen', return_value=Reply({'ok': True})) as urlopen:
            got = watch.send('NAS', 'Drive is 91% full')
        calls = [(c[0][0].full_url, c[0][0].get_method(), json.loads(c[0][0].data or b'null'))
                 for c in urlopen.call_args_list]
        return got, calls


class Quiet(Helpers):
    def test_a_future_time_with_a_zone_is_quiet(self):
        text = self.time_with_zone(2)
        self.quiet(text)
        self.assertEqual(watch.quiet_until(), datetime.fromisoformat(text))

    def test_a_past_time_is_not_quiet(self):
        self.quiet(self.time_with_zone(-2))
        self.assertIsNone(watch.quiet_until())

    def test_a_missing_file_is_not_quiet(self):
        self.assertIsNone(watch.quiet_until())

    def test_bad_text_is_not_quiet(self):
        self.quiet('tomorrow lunchtime')
        self.assertIsNone(watch.quiet_until())

    def test_a_time_without_a_zone_is_not_quiet(self):
        self.quiet((datetime.now() + timedelta(hours=2)).isoformat(timespec='seconds'))
        self.assertIsNone(watch.quiet_until())


SERVICES = [
    {'domain': 'notify', 'services': {'mobile_app_one': {}, 'mobile_app_two': {}, 'persistent_notification': {}}},
    {'domain': 'light', 'services': {'mobile_app_not_notify': {}}},
]


class Phones(Helpers):
    def test_every_mobile_app_service_is_a_phone(self):
        with mock.patch.object(watch.urllib.request, 'urlopen', return_value=Reply(SERVICES)) as urlopen:
            self.assertEqual(watch.phone_names(), ['mobile_app_one', 'mobile_app_two'])
        self.assertEqual(urlopen.call_count, 1)
        req = urlopen.call_args[0][0]
        self.assertEqual(req.full_url, 'http://supervisor/core/api/services')
        self.assertEqual(req.get_method(), 'GET')

    def test_no_notify_domain_means_no_phone(self):
        found = [{'domain': 'light', 'services': {'mobile_app_x': {}}}]
        with mock.patch.object(watch.urllib.request, 'urlopen', return_value=Reply(found)):
            self.assertEqual(watch.phone_names(), [])


class Send(Helpers):
    def test_a_notice_is_left_in_notifications_and_goes_to_the_phones(self):
        self.enterContext(mock.patch.object(watch, 'phone_names', return_value=['mobile_app_kitchen']))
        got, calls = self.sent()
        self.assertTrue(got)
        self.assertEqual(calls, [
            ('http://supervisor/core/api/services/persistent_notification/create', 'POST',
             {'title': 'NAS', 'message': 'Drive is 91% full'}),
            ('http://supervisor/core/api/services/notify/mobile_app_kitchen', 'POST',
             {'title': 'NAS', 'message': 'Drive is 91% full', 'data': {}}),
        ])

    def test_a_quiet_house_still_gets_the_notification_but_no_phone(self):
        self.enterContext(mock.patch.object(watch, 'phone_names', return_value=['mobile_app_kitchen']))
        self.quiet(self.time_with_zone(3))
        got, calls = self.sent()
        self.assertTrue(got)
        self.assertEqual([call[0] for call in calls],
                         ['http://supervisor/core/api/services/persistent_notification/create'])
        self.assertTrue(any('quiet until' in text for text in self.notes))

    def test_a_failed_notification_is_logged_and_does_not_raise(self):
        with mock.patch.object(watch.urllib.request, 'urlopen', side_effect=OSError('no supervisor')):
            self.assertFalse(watch.send('NAS', 'Drive is full'))
        self.assertTrue(any('could not leave a notification' in text for text in self.notes))

    def test_one_phone_failing_does_not_stop_the_next_or_the_notice(self):
        self.enterContext(mock.patch.object(watch, 'phone_names', return_value=['mobile_app_kitchen', 'mobile_app_hall']))
        replies = [Reply({'ok': True}), OSError('phone off'), Reply({'ok': True})]
        with mock.patch.object(watch.urllib.request, 'urlopen', side_effect=replies):
            self.assertTrue(watch.send('NAS', 'Drive is full'))
        self.assertTrue(any('could not notify mobile_app_kitchen' in text for text in self.notes))


if __name__ == '__main__':
    unittest.main()



class Ntfy(Helpers):
    """Outside Home Assistant: NTFY_URL set, the notice is one POST to that topic and nothing goes to the Supervisor."""

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {'NTFY_URL': 'http://127.0.0.1:8095/home'})
        env.start()
        self.addCleanup(env.stop)

    def test_one_post_to_the_topic_with_the_title(self):
        with mock.patch.object(watch.urllib.request, 'urlopen', return_value=Reply()) as urlopen:
            self.assertTrue(watch.send('NAS', 'Drive is 91% full'))
        self.assertEqual(urlopen.call_count, 1)
        req = urlopen.call_args[0][0]
        self.assertEqual((req.full_url, req.get_method(), req.data), ('http://127.0.0.1:8095/home', 'POST',
                                                                      b'Drive is 91% full'))
        self.assertEqual(req.get_header('Title'), 'NAS')

    def test_quiet_sends_nothing(self):
        self.quiet(self.time_with_zone(2))
        with mock.patch.object(watch.urllib.request, 'urlopen') as urlopen:
            self.assertTrue(watch.send('NAS', 'Drive is full'))
        urlopen.assert_not_called()

    def test_a_failure_is_logged_not_raised(self):
        with mock.patch.object(watch.urllib.request, 'urlopen', side_effect=OSError('refused')):
            self.assertFalse(watch.send('NAS', 'Drive is full'))
        self.assertTrue(any('ntfy' in n for n in self.notes))


class TrustedHeader(unittest.TestCase):
    """X-Remote-User-Id only counts from a login proxy: ingress or this same machine."""

    def test_only_proxies_with_the_header_may_change(self):
        user = {'X-Remote-User-Id': 'abc'}
        self.assertTrue(watch.ingress_request(user, '172.30.32.2'))
        self.assertTrue(watch.ingress_request(user, '127.0.0.1'))
        self.assertTrue(watch.ingress_request(user, '::1'))
        self.assertFalse(watch.ingress_request(user, '192.168.1.50'))      # a device on the home network
        self.assertFalse(watch.ingress_request(user, '172.27.66.2'))       # WireGuard
        self.assertFalse(watch.ingress_request({}, '127.0.0.1'))
        self.assertFalse(watch.ingress_request({'X-Remote-User-Id': ' '}, '172.30.32.2'))
