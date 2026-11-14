"""Tests for the Supervisor's watchdog door: GET /health answers 200 "ok" while the service-check loop is going round,
and 503 with a short reason when it has stopped. The answer carries no data about the house and no secret.

Run from addon/: `python -m unittest test_health`. Everything happens in a temporary data folder, made before watch is
imported (its modules read HOME_WATCH_DATA then); the door is a real server on a free port, so it is tested the way the
Supervisor calls it (Home Assistant counts any answer under 300 as healthy and 503 as not). The check loop's last round
is set by hand in the door's tests, and one test runs the loop itself for a single round (its sleep stubbed out), so
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
from unittest import mock

DATA = tempfile.mkdtemp(prefix='home-watch-health-test-')
os.environ['HOME_WATCH_DATA'] = DATA

import services  # noqa: E402  (after the temporary data folder is set)
import watch  # noqa: E402  (the web door: GET /health)


class Helpers(unittest.TestCase):
    """A fresh data folder and the small writers the tests below share; no tests of its own, so the door's tests can
    use the same setup without running them twice."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix='health-', dir=DATA)
        services.DATA = self.folder
        services.LIST = os.path.join(self.folder, 'monitors.json')
        services.CODES = os.path.join(self.folder, 'push-codes.json')
        services.EVENTS = os.path.join(self.folder, 'services')
        services.CHECKS_DIR = os.path.join(self.folder, 'checks')
        services.monitors[:] = []
        services.status.clear()
        services.samples.clear()
        services.cache.clear()
        services.codes = {}
        watch.networks = watch.DEFAULT_NETWORKS
        watch.options.clear()
        self.notes = []

    def note(self, text):
        self.notes.append(text)

    def call(self, path='/health'):
        """(status, body, content type) as the Supervisor's request comes back; a 503 arrives as an HTTPError."""
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{self.port}{path}', timeout=5) as r:
                return r.status, r.read().decode(), r.headers.get('Content-Type')
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.read().decode(), e.headers.get('Content-Type')
            finally:
                e.close()


class Health(Helpers):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), watch.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_a_loop_that_just_ran_is_ok(self):
        services.last_round = time.time()
        status, body, kind = self.call()
        self.assertEqual(status, 200)
        self.assertEqual(body, 'ok')
        self.assertTrue(kind.startswith('text/plain'))

    def test_a_stale_loop_is_503_with_a_short_reason_and_no_data(self):
        services.last_round = time.time() - services.HEALTH_STALE - 60
        status, body, kind = self.call()
        self.assertEqual(status, 503)
        self.assertTrue(kind.startswith('text/plain'))
        self.assertIn('the check loop has not run for 6 min', body)
        self.assertNotIn('{', body)                   # no JSON, no machine, no address: nothing about the house

    def test_the_loop_has_five_minutes_before_it_counts_as_stuck(self):
        now = time.time()
        services.last_round = now - 60
        self.assertEqual(services.health(now), (True, 'ok'))
        services.last_round = now - services.HEALTH_STALE - 1
        ok, why = services.health(now)
        self.assertFalse(ok)
        self.assertIn('5 min', why)

    def test_the_door_answers_even_when_a_house_narrowed_allowed_networks(self):
        # The Supervisor's watchdog calls from its own address; a narrower allowed_networks must never turn a healthy
        # add-on into a restart loop. The answer holds nothing, so this door is not gated like the page's.
        watch.networks = ('192.0.2.',)
        services.last_round = time.time()
        self.assertEqual(self.call(), (200, 'ok', 'text/plain; charset=utf-8'))
        status, _, _ = self.call('/api/status')
        self.assertEqual(status, 403)                 # the rest of the page still is


class Loop(Helpers):
    """The check loop is what /health watches, so it must mark every round it begins."""

    def test_the_loop_marks_its_rounds(self):
        with open(services.LIST, 'w', encoding='utf-8') as f:
            json.dump([], f)
        services.last_round = 0.0
        started = time.time()
        sleep = mock.patch.object(services.time, 'sleep', side_effect=StopIteration)
        with self.assertRaises(StopIteration), sleep:
            services.loop({}, self.note)              # one round, then its sleep ends the loop for the test
        self.assertGreaterEqual(services.last_round, started)
        self.assertEqual(services.health(), (True, 'ok'))


if __name__ == '__main__':
    unittest.main()
