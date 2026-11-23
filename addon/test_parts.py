"""Tests for part health (parts.py): one notice when a part turns broken, one when it recovers, no repeats.

Run from addon/: `python -m unittest test_parts`. A small local web server stands in for the app and is made to report
a broken part (a fake failure), so no real app has to break.
"""

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA = tempfile.mkdtemp(prefix='home-watch-parts-test-')
os.environ['HOME_WATCH_DATA'] = DATA

import parts  # noqa: E402  (after the temporary data folder is set)


class FakeApp:
    """Serves {"sources": [...]} at /sources/health; .answer is what it says, .status its HTTP status."""

    def __init__(self):
        self.answer = {'sources': []}
        self.status = 200
        app = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(app.answer).encode()
                self.send_response(app.status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/sources/health'

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class PartHealth(unittest.TestCase):
    def setUp(self):
        parts.STATE_FILE = os.path.join(tempfile.mkdtemp(dir=DATA), 'parts.json')
        parts.unreachable.clear()
        self.app = FakeApp()
        self.addCleanup(self.app.close)
        self.notes, self.sent, self.deliver = [], [], True
        parts.configure([{'name': 'Finder', 'url': self.app.url}], self.notes.append)

    def send(self, title, message):
        self.sent.append((title, message))
        return self.deliver

    def round(self):
        parts.check(self.send, self.notes.append)

    def say(self, **broken):
        self.app.answer = {'sources': [{'name': n, 'broken': bool(r), 'reason': r or 'ok'} for n, r in broken.items()]}

    def test_broken_once_then_recovered_once(self):
        self.say(dmm='', apibay='')
        self.round()
        self.assertEqual(self.sent, [])
        self.say(dmm='found nothing new in 48 h', apibay='')  # the fake failure
        self.round()
        self.round()
        self.round()
        self.assertEqual(self.sent, [('Finder', 'dmm: found nothing new in 48 h')])
        self.say(dmm='', apibay='')
        self.round()
        self.round()
        self.assertEqual(self.sent[1:], [('Finder', 'dmm is working again')])

    def test_restart_does_not_repeat(self):
        self.say(rutor='errors on every test title in the last two checks: HTTP 503')
        self.round()
        self.assertEqual(len(self.sent), 1)
        parts.unreachable.clear()  # a restart: only the state file carries over
        self.round()
        self.assertEqual(len(self.sent), 1)

    def test_unsent_notice_is_tried_again(self):
        self.say(knaben='found nothing for any test title in the last two checks')
        self.deliver = False
        self.round()
        self.deliver = True
        self.round()
        self.round()
        self.assertEqual([m for _, m in self.sent], ['knaben: found nothing for any test title in the last two checks'] * 2)

    def test_app_down_sends_nothing_and_logs_once(self):
        self.say(dmm='found nothing new in 48 h')
        self.round()
        self.app.status = 500
        self.round()
        self.round()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(sum('could not read Finder' in n for n in self.notes), 1)
        self.app.status = 200
        self.say(dmm='')
        self.round()
        self.assertEqual(self.sent[-1], ('Finder', 'dmm is working again'))

    def test_bad_settings_are_dropped(self):
        notes = []
        parts.configure([{'name': 'x'}, 'nope', {'name': 'Ok', 'url': 'http://h/x'}], notes.append)
        self.assertEqual(parts.watched, [{'name': 'Ok', 'url': 'http://h/x'}])
        self.assertEqual(len(notes), 2)
        parts.configure({'name': 'x'}, notes.append)
        self.assertEqual(parts.watched, [])


if __name__ == '__main__':
    unittest.main()
