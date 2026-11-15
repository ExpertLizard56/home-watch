"""Tests for running outside Home Assistant (plain Docker): the kernel log read from /dev/kmsg when there is no
Supervisor, and self-heal restarting a container through the Docker Engine API (heal_containers in settings.json).

Run from addon/: `python -m unittest test_plain`. /dev/kmsg and the Docker socket are stubbed, so nothing here needs
Linux, Docker or Home Assistant.
"""

import errno
import json
import os
import tempfile
import unittest
from unittest import mock

DATA = tempfile.mkdtemp(prefix='home-watch-plain-test-')
os.environ['HOME_WATCH_DATA'] = DATA

import pi  # noqa: E402
import services  # noqa: E402
import watch  # noqa: E402

CHECK = {'id': 1, 'name': 'Media Hub', 'type': 'ping', 'host': '192.0.2.1', 'heal': True, 'app': 'media_hub'}


class Kmsg(unittest.TestCase):
    def reads(self, *records):
        """os.read handing out one kmsg record per call, then 'nothing more' (EAGAIN on a non-blocking read)."""
        out = list(records)

        def read(fd, size):
            if not out:
                raise BlockingIOError
            got = out.pop(0)
            if isinstance(got, Exception):
                raise got
            return got
        return read

    def test_kmsg_keeps_the_text_of_each_record(self):
        read = self.reads(b'6,1,10,-;Booting Linux\n', OSError(errno.EPIPE, 'overwritten'),
                          b'3,2,20,-;mmc0: Timeout waiting for hardware interrupt\n SUBSYSTEM=mmc\n')
        with mock.patch.object(pi.os, 'open', return_value=3), mock.patch.object(pi.os, 'read', read), \
                mock.patch.object(pi.os, 'close'):
            self.assertEqual(pi.kmsg(), 'Booting Linux\nmmc0: Timeout waiting for hardware interrupt')

    def test_without_a_supervisor_card_errors_count_from_kmsg(self):
        pi.seen['card_errors'] = None
        notes = []
        text = 'Booting Linux\nmmc0: Timeout waiting for hardware interrupt\nEXT4-fs error (device mmcblk0p2)'
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(pi, 'kmsg', return_value=text):
            os.environ.pop('SUPERVISOR_TOKEN', None)
            self.assertEqual(pi.card_errors(notes.append), 2)

    def test_no_kmsg_reports_unknown(self):
        notes = []
        with mock.patch.object(pi, 'kmsg', side_effect=FileNotFoundError('/dev/kmsg')):
            os.environ.pop('SUPERVISOR_TOKEN', None)
            self.assertIsNone(pi.card_errors(notes.append))
        self.assertIn("couldn't read the kernel log", notes[-1])

    def test_with_a_supervisor_token_the_supervisor_is_asked(self):
        with mock.patch.dict(os.environ, {'SUPERVISOR_TOKEN': 'x'}), \
                mock.patch.object(pi, 'supervisor', return_value='') as sup:
            self.assertEqual(pi.card_errors(lambda t: None), 0)
        sup.assert_called_once_with(pi.KERNEL_LOGS[0], 'text/plain')


class DockerHeal(unittest.TestCase):
    def setUp(self):
        services.heal_containers = {'media_hub': 'media-hub'}
        services.healed.clear()
        self.notes = []

    def tearDown(self):
        services.heal_containers = {}

    def test_down_restarts_the_container_once_then_waits(self):
        with mock.patch.object(services, 'restart_container') as restart, \
                mock.patch.object(services.urllib.request, 'urlopen') as hook:
            services.heal(CHECK, 'down', 'no answer', {'self_heal': True}, self.notes.append)
            services.heal(CHECK, 'down', 'no answer', {'self_heal': True}, self.notes.append)
            restart.assert_called_once_with('media-hub')
            hook.assert_not_called()                       # no Home Assistant webhook
        self.assertIn('self-heal waits', self.notes[-1])

    def test_after_the_cool_down_it_restarts_again(self):
        with mock.patch.object(services, 'restart_container') as restart:
            services.heal_container(CHECK, 'down', {'self_heal': True}, self.notes.append, now=1000)
            services.heal_container(CHECK, 'down', {'self_heal': True}, self.notes.append,
                                    now=1000 + services.HEAL_COOLDOWN)
        self.assertEqual(restart.call_count, 2)

    def test_nothing_when_self_heal_is_off_or_the_check_is_up(self):
        with mock.patch.object(services, 'restart_container') as restart:
            services.heal(CHECK, 'down', '', {'self_heal': False}, self.notes.append)
            services.heal(CHECK, 'up', '', {'self_heal': True}, self.notes.append)
        restart.assert_not_called()

    def test_a_failed_restart_is_noted_not_raised(self):
        with mock.patch.object(services, 'restart_container', side_effect=OSError('no socket')):
            services.heal(CHECK, 'down', '', {'self_heal': True}, self.notes.append)
        self.assertIn('could not restart media-hub', self.notes[-1])

    def test_restart_container_posts_to_the_engine(self):
        conn = mock.MagicMock()
        conn.getresponse.return_value.status = 204
        conn.getresponse.return_value.read.return_value = b''
        with mock.patch.object(services, 'DockerSocket', return_value=conn):
            services.restart_container('media-hub')
        conn.request.assert_called_once_with('POST', '/containers/media-hub/restart?t=20')
        conn.getresponse.return_value.status = 404
        with mock.patch.object(services, 'DockerSocket', return_value=conn), self.assertRaises(RuntimeError):
            services.restart_container('nope')


class Settings(unittest.TestCase):
    def test_heal_containers_is_read_from_settings(self):
        folder = tempfile.mkdtemp(dir=DATA)
        watch.SETTINGS_FILE = os.path.join(folder, 'settings.json')
        with open(watch.SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump({'heal_containers': {'media_hub': ' media-hub ', '': 'x', 'blank': ''}}, f)
        watch.load_settings(lambda t: None)
        self.assertEqual(services.heal_containers, {'media_hub': 'media-hub'})
        with open(watch.SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump({}, f)
        watch.load_settings(lambda t: None)
        self.assertEqual(services.heal_containers, {})


if __name__ == '__main__':
    unittest.main()
