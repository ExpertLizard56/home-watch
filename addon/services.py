"""Home watch's service checks: ping, port, http, dns, dyndns, push, note and machine checks, on the page's Checks screen.

The list is /share/home-watch/monitors.json (plain, so it can still be edited by hand and restarted). A data folder with
no list in it yet gets the example list shipped in addon/monitors.json; a list that is already there is never
overwritten. The page's Checks screen validates a check, writes the list whole (a temp file, then a rename) and puts it
in front of the running checks at once, so nothing needs a restart. Each check runs every `every` seconds:
- ping: a device answers; port: a TCP port takes a connection; http: a page answers 200-299 (or the exact status in
  `expect`); dns: a DNS server answers a name;
- push: something else reports in at /api/push/<code>?status=up&msg=..., and silence longer than `every` = down;
- machine (Home watch's own): one of its machines reported within `quiet` seconds (15 min if not set) with no problem.
  A machine check may also name a `host`: each round it then pings that address the way a ping check does, and is down
  when the address does not answer ("Not on the network") or answers but the report is stale ("On the network, but its
  reporter stopped sending"); its response time is then the ping's.
- note (Home watch's own): one of its machines carries a note with the check's `label`, and the note's text says what
  the check expects: it must hold `good_text` and must not hold `bad_text`, both plain case-insensitive substrings.
  bad_text is looked for first, so "not answering" can be bad while "answering" is good. A report older than `quiet`
  seconds (900 by default) is down, since the machine has stopped saying anything.

Every check has an "id": a whole number, unique, never reused. Everything kept about a check is under that id (its
state, history, outages, push code, pushes), so its "name" is only the label shown: rename it as often as you like and
nothing is lost. A check with no id gets the next free number on load, and monitors.json is written back with it (in
place, order and every other field kept) - once; the next start changes nothing. A name-keyed code, history or outage
left from before ids is moved to that check's id on the same load. A duplicate id is an error logged by name and the
later check is skipped, never merged.

The push checks' codes are NOT in the list: they live in /share/home-watch/push-codes.json ({"<check id>": "<code>"},
made 0600, written whole). The list holds no secrets, so it can be copied or committed; copying a new check list over
it never loses the codes. A list that still keeps a code in a "token" field is migrated on load: the code moves into
push-codes.json, and the list is rewritten without it. A push check with no code at all gets one made
(secrets.token_hex(16)) and a log line naming the check (never the code). /api/push/<code> finds its check by
comparing the code with hmac.compare_digest, so a wrong code learns nothing from how long the answer took.
A check that fails is "pending" until it has failed `retries` more times in a row, then "down", or "off" for a monitor
with "off_ok": true (a TV switched off at the wall: normal, grey on the page, not counted against its uptime).
Changes go in /share/home-watch/services/<year-month>.csv (time, id, status, detail); from them come the uptime
(24 h, 30 days, a year), the hour-by-hour strip and the outage list. Every 5 minutes each check's results go in
/share/home-watch/checks/<year-month>.csv (time, id, ok, checks, ms: its response times). Both kept 12 months.
Monitors with "heal": true post a webhook message to Home Assistant's self-heal automation when they go down or come
back, but only when the add-on's `self_heal` option is on. The message names the script that restarts the app
(monitor.script), built from the `heal_script` setting in settings.json (default script.home_watch_heal_{app}) and
the check's `app` field, or its name made into a slug ("Media Hub" -> media_hub) when it has no `app`.

Borrowed from Uptime Kuma (github.com/louislam/uptime-kuma, MIT License, Copyright (c) 2021 Louis Lam): the monitor
types and their up/down rules, retries with a pending state, the push URL shape (/api/push/<code>?status=&msg=) and
the webhook message shape ({"heartbeat": {"status", "msg"}, "monitor": {"name"}, "msg"}) that the self-heal
automation reads.

The Supervisor's watchdog (config.yaml's `watchdog`) asks the hub's GET /health whether the add-on is well, and that
answer is this module's `last_round`: the time the check loop last began a round. A loop that has stopped going round
is an add-on that needs restarting, so /health says so (services.health).
"""

import csv
import hmac
import http.client
import io
import json
import os
import random
import re
import secrets
import shutil
import socket
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

DATA = os.environ.get('HOME_WATCH_DATA', '/share/home-watch')
LIST = f'{DATA}/monitors.json'
CODES = f'{DATA}/push-codes.json'  # {"<check id>": "<code>"} (JSON keys are strings); never in git
EVENTS = f'{DATA}/services'
CHECKS_DIR = f'{DATA}/checks'
KEEP_MONTHS = 12
FLUSH = 300  # seconds between rows in the checks file
HEALTH_STALE = 300  # seconds; older than this without a loop round and GET /health answers 503 (the watchdog restarts)
DEFAULT_LIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'monitors.json')
HEAL_SCRIPT = 'script.home_watch_heal_{app}'  # the Home Assistant script self-heal calls; {app} = the check's app
REMOVED = f'{DATA}/removed-ids.json'  # ids of checks removed on the page, so a new check never takes one
TYPES = ('ping', 'port', 'http', 'dns', 'dyndns', 'push', 'note', 'machine')
FORM_FIELDS = ('name', 'group', 'type', 'host', 'port', 'url', 'expect', 'name_to_look_up', 'server', 'hostname',
               'machine', 'label', 'good_text', 'bad_text', 'quiet', 'every', 'retries', 'off_ok', 'heal', 'app')

lock = threading.Lock()
monitors = []
status = {}  # check id -> {state: up|pending|down|waiting, since, detail, fails, last, ms}
pushes = {}  # check id -> (time, ok, msg)
codes = {}  # check id (as a string) -> its push code; a new dict on every load()
machine = None  # watch.py sets this: machine id -> its latest report (or None)
heal_script = HEAL_SCRIPT  # watch.py sets this from settings.json
heal_containers = {}  # {app: container name}: watch.py sets this from settings.json; restarted through Docker
healed = {}  # {app: when its container was last restarted}
HEAL_COOLDOWN = 900  # seconds between two restarts of one app, as the Home Assistant script's 15 min wait
DOCKER_SOCKET = '/var/run/docker.sock'
samples = {}  # check id -> [ok, checks, ms total, ms count] since the last row was written
cache = {}  # what summary() last worked out, and when
last_round = time.time()  # when the check loop last began a round; health() reads it for the Supervisor's watchdog


def save(path, text):
    """Write a file whole (a temp file, then a rename) so a reader never sees half of it, 0600 where that means
    something (the push codes)."""
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def show_list(found):
    """The check list as it sits on the hub: one check per line, so it stays easy to read and edit by hand."""
    return '[\n' + ',\n'.join('  ' + json.dumps(m) for m in found) + '\n]\n'


def read_codes(note):
    """key -> code: a check's id once the ids are in (a name left from before them, until load() moves it). A missing
    file is normal (the first run); an unreadable one is said out loud (new codes then)."""
    try:
        with open(CODES, encoding='utf-8') as f:
            got = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        note(f"push-codes.json unreadable ({e}); making new codes for the push checks")
        return {}
    return {str(k): str(v) for k, v in got.items() if v}


def whole(value):
    """A check's id as a whole number above zero, or None when it is not usable (missing, a string, a float, a bool,
    zero or less)."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def removed_ids():
    """The ids of checks taken off the page: kept in removed-ids.json so a new check never inherits a removed one's
    number, even though its history rows are gone. A missing or unreadable file is an empty set."""
    try:
        with open(REMOVED, encoding='utf-8') as f:
            got = json.load(f)
    except (OSError, ValueError):
        return set()
    if not isinstance(got, list):
        return set()
    return {int(i) for i in got if str(i).strip().isdigit()}


def remember_removed(cid):
    """Keep a removed check's number for good, so nothing is ever handed it again."""
    save(REMOVED, json.dumps(sorted(removed_ids() | {cid})) + '\n')


def stored_ids():
    """The ids any stored row already carries, plus every id a check was ever removed with: a check's number is never
    handed out twice, so a check removed long ago cannot pass its history to a new one. Read only when a number has to
    be made."""
    found = removed_ids()
    for folder in (EVENTS, CHECKS_DIR):
        for f in months(folder):
            with open(f'{folder}/{f}', newline='', encoding='utf-8') as fh:
                rows = csv.reader(fh)
                next(rows, None)  # the header
                for row in rows:
                    try:
                        found.add(int(row[1]))
                    except (IndexError, TypeError, ValueError):
                        continue  # a name row from before ids, or a broken line
    return found


def next_number(used, known):
    """The next free number: one past every id the list, the codes and the stored rows already hold."""
    numbers = set(used)
    for key in known:
        try:
            numbers.add(int(key))
        except (TypeError, ValueError):
            continue  # a name key from before ids
    return max(numbers, default=0) + 1


def migrate_history(by_name, note):
    """Rows written before ids kept the check's name in the second column: rewrite each month's file once, putting
    that check's id there and leaving a name no check has any more alone. Files already carrying ids are untouched."""
    for folder in (EVENTS, CHECKS_DIR):
        for f in months(folder):
            path = f'{folder}/{f}'
            with open(path, encoding='utf-8') as fh:
                first = fh.readline()  # only the header: a file already carrying ids is not read whole again
            head = next(csv.reader([first]), []) if first else []
            if len(head) < 2 or head[1] != 'name':
                continue
            with open(path, newline='', encoding='utf-8') as fh:
                rows = list(csv.reader(fh))
            rows[0][1] = 'id'
            for row in rows[1:]:
                if len(row) > 1 and row[1] in by_name:
                    row[1] = str(by_name[row[1]])
            buf = io.StringIO()
            csv.writer(buf, lineterminator='\n').writerows(rows)
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as fh:
                fh.write(buf.getvalue())
            os.replace(tmp, path)
            note(f"service history {os.path.basename(folder)}/{f}: check names replaced by their ids")


def load(note=None):
    """Read the check list and the push codes, and give every check its id: one without one gets the next free
    number, and monitors.json is written back with it (once). A code still kept in a check's "token" field, or under
    its name from before ids, moves into push-codes.json under the check's id; a push check with no code gets one. A
    duplicate id is an error logged by the check's name, and the later check is skipped, never merged."""
    global codes
    note = note or (lambda text: None)
    if not os.path.exists(LIST):
        shutil.copy(DEFAULT_LIST, LIST)
    with open(LIST, encoding='utf-8') as f:
        found = json.load(f)
    known = read_codes(note)
    changed = False
    used = {m['id'] for m in found if whole(m.get('id'))}
    reserved = None
    for m in found:
        if whole(m.get('id')) is not None:
            continue
        if reserved is None:
            reserved = stored_ids()
        if 'id' in m:
            note(f"check {m.get('name', '?')}: its id is not a whole number above zero, so it got a new one")
        m['id'] = next_number(used | reserved, known)
        used.add(m['id'])
        changed = True
    kept, seen = [], {}
    for m in found:
        label = m.get('name', '?')
        if m['id'] in seen:
            note(f"monitors.json: {label} has id {m['id']}, already used by {seen[m['id']]}: skipped")
            changed = True
            continue
        seen[m['id']] = label
        kept.append(m)
    apps = False
    for m in kept:  # a self-heal check keeps its app, so a later rename still calls the same script
        if m.get('heal') and not str(m.get('app') or '').strip():
            got = app_name(m)
            if got:
                m['app'] = got
                apps = True
    moved = made = False
    for m in kept:
        if 'token' in m:  # the old place for a push check's code, or where one copy of the list still keeps it
            if not known.get(str(m['id'])):
                known[str(m['id'])] = m.pop('token')
            else:
                m.pop('token')
            moved = changed = True
    ids = {str(m['id']) for m in kept}
    for m in kept:
        key, label = str(m['id']), str(m.get('name', ''))
        if not label or label == key or label not in known:
            continue
        if key not in known:  # a code still under the check's old name: it moves to its id and keeps its value
            known[key] = known.pop(label)
            moved = True
        elif label not in ids:  # both places: the id's own code is in use, the name's copy is dropped
            known.pop(label)
            moved = True
    for m in kept:
        if m.get('type') == 'push' and not known.get(str(m['id'])):
            known[str(m['id'])] = secrets.token_hex(16)
            note(f"push check {m.get('name', m['id'])}: no code yet, so one was made (see push-codes.json)")
            made = True
    if changed or apps:
        save(LIST, show_list(kept))
        if changed:
            note(f"monitors.json: every check now has its id ({len(kept)} checks)")
        if apps:
            note("monitors.json: every self-heal check now keeps its app, so a rename keeps the same script")
    if moved:
        note("push-codes.json: each push code is kept under its check's id now")
    if moved or made:
        save(CODES, json.dumps(known, indent=2, sort_keys=True) + '\n')
    migrate_history({str(m.get('name', '')): m['id'] for m in kept if m.get('name')}, note)
    with lock:
        codes = known
        monitors[:] = kept
        for m in kept:
            status.setdefault(m['id'], {'state': 'waiting', 'since': time.time(), 'detail': '', 'fails': 0, 'last': 0})


# ---- the Checks screen: check the fields, save, reorder, remove ----

def slug(text):
    """An app name as the self-heal script needs it: lowercase, letters and digits, runs of anything else made one
    underscore ("Media Hub" -> media_hub)."""
    return re.sub(r'[^a-z0-9]+', '_', str(text or '').lower()).strip('_')


def machine_slug(text):
    """A machine id as watch.py makes it from a report: lowercase letters, digits and hyphens."""
    return re.sub(r'[^a-z0-9-]', '', str(text or '').lower())


def _text(raw, key, limit):
    return str(raw.get(key) or '').strip()[:limit]


def _whole(raw, key, low, high, default):
    """(number, error): a whole number in range, `default` when the field is empty."""
    value = raw.get(key)
    if value is None or str(value).strip() == '':
        return default, None
    try:
        got = float(str(value).strip())
    except (TypeError, ValueError):
        return None, f'Use a whole number from {low} to {high}.'
    if got != int(got) or not low <= int(got) <= high:
        return None, f'Use a whole number from {low} to {high}.'
    return int(got), None


def _flag(raw, key):
    value = raw.get(key)
    return value is True or str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def validate(raw, current=None):
    """The Checks screen's fields -> (check, errors). `errors` maps a field to the sentence shown next to it; an
    empty `errors` means the check is ready to save. `current` is the check being edited, None for a new one. Only the
    fields the chosen type reads are kept, so changing type leaves nothing stale behind."""
    errors = {}
    kind = str(raw.get('type') or '').strip()
    if kind not in TYPES:
        errors['type'] = 'Pick a check type.'
        kind = 'ping'  # keep checking the rest of the form
    name = _text(raw, 'name', 80)
    if not name:
        errors['name'] = 'Give the check a name.'
    out = {'name': name or 'Check', 'type': kind}
    group = _text(raw, 'group', 60)
    if group:
        out['group'] = group
    every, bad = _whole(raw, 'every', 5, 86400, 300 if kind == 'push' else 1800 if kind == 'dyndns' else 60)
    if bad:
        errors['every'] = bad
    else:
        out['every'] = every
    retries, bad = _whole(raw, 'retries', 0, 20, 0 if kind == 'push' else 2)
    if bad:
        errors['retries'] = bad
    else:
        out['retries'] = retries
    host = _text(raw, 'host', 200)
    if kind == 'ping':
        if not host:
            errors['host'] = 'The address or name to ping.'
        else:
            out['host'] = host
    elif kind == 'port':
        if not host:
            errors['host'] = 'The address or name of the device.'
        else:
            out['host'] = host
        port, bad = _whole(raw, 'port', 1, 65535, None)
        if bad or port is None:
            errors['port'] = bad or 'The TCP port to connect to (1-65535).'
        else:
            out['port'] = port
    elif kind == 'http':
        url = _text(raw, 'url', 500)
        if not url:
            errors['url'] = 'The page to fetch, starting with http:// or https://.'
        elif not url.lower().startswith(('http://', 'https://')):
            errors['url'] = 'Start the URL with http:// or https://.'
        else:
            out['url'] = url
        expect, bad = _whole(raw, 'expect', 100, 599, None)
        if bad:
            errors['expect'] = bad
        elif expect is not None:
            out['expect'] = expect
    elif kind == 'dns':
        look = _text(raw, 'name_to_look_up', 200)
        if not look:
            errors['name_to_look_up'] = 'The name the DNS server should answer.'
        else:
            out['name_to_look_up'] = look
        server = _text(raw, 'server', 200)
        if not server:
            errors['server'] = 'The DNS server to ask.'
        else:
            out['server'] = server
        port, bad = _whole(raw, 'port', 1, 65535, 53)
        if bad:
            errors['port'] = bad
        else:
            out['port'] = port
    elif kind == 'dyndns':
        hostname = _text(raw, 'hostname', 200)
        if not hostname:
            errors['hostname'] = 'The dynamic-DNS name to check.'
        else:
            out['hostname'] = hostname
    elif kind == 'machine':
        mid = machine_slug(_text(raw, 'machine', 60))
        if not mid:
            errors['machine'] = 'Pick a machine that has reported, or type its id.'
        else:
            out['machine'] = mid
        if host:
            out['host'] = host
        quiet, bad = _whole(raw, 'quiet', 60, 86400, 900)
        if bad:
            errors['quiet'] = bad
        else:
            out['quiet'] = quiet
    elif kind == 'note':
        mid = machine_slug(_text(raw, 'machine', 60))
        if not mid:
            errors['machine'] = 'Pick a machine that has reported, or type its id.'
        else:
            out['machine'] = mid
        label = _text(raw, 'label', 40)
        if not label:
            errors['label'] = "The note's label, exactly as the machine sends it."
        else:
            out['label'] = label
        good = _text(raw, 'good_text', 200)
        bad = _text(raw, 'bad_text', 200)
        if good:
            out['good_text'] = good
        if bad:
            out['bad_text'] = bad
        if not good and not bad:
            errors['good_text'] = 'Give the text the note should contain, or the text it must not contain.'
    # a push check needs nothing beyond the common fields: something else calls its path
    if kind not in ('machine', 'note'):
        asked = _text(raw, 'machine', 60)
        mid = machine_slug(asked)
        if asked and not mid:
            errors['machine'] = 'Use lowercase letters, digits and hyphens for a machine id.'
        elif mid:
            out['machine'] = mid
    if _flag(raw, 'off_ok'):
        out['off_ok'] = True
    if _flag(raw, 'heal'):
        out['heal'] = True
        app = slug(_text(raw, 'app', 60)) or app_name({'name': name})
        if not app:
            errors['app'] = 'Name the app the self-heal script restarts.'
        else:
            out['app'] = app
    return out, errors


def save_check(raw, note=None):
    """The Checks screen's save: check the fields, then write monitors.json whole (an edited check keeps its id, a new
    one gets the next free number, never shown) and put the new list in front of the running checks at once. Returns
    (the check as the page shows it, field -> error); the errors are empty when it was saved."""
    note = note or (lambda text: None)
    current = None
    if str(raw.get('id') or '').strip():
        try:
            cid = int(str(raw['id']).strip())
        except (TypeError, ValueError):
            return None, {'id': 'This check is not one the hub knows; reload the page.'}
        with lock:
            current = next((m for m in monitors if m['id'] == cid), None)
        if current is None:
            return None, {'id': 'This check has gone; reload the page.'}
    found, errors = validate(raw, current)
    if errors:
        return None, errors
    with lock:
        if current is not None:
            found['id'] = current['id']
            index = monitors.index(current)
            found_list = list(monitors)
            found_list[index] = found
        else:
            known = set(codes) | {str(i) for i in stored_ids()}
            found['id'] = next_number({m['id'] for m in monitors}, known)
            found_list = list(monitors) + [found]
        new_codes = dict(codes)
        made = ''
        if found['type'] == 'push' and not new_codes.get(str(found['id'])):
            new_codes[str(found['id'])] = secrets.token_hex(16)
            made = 'made'
        elif found['type'] != 'push' and current and current.get('type') == 'push' and str(found['id']) in new_codes:
            new_codes.pop(str(found['id']))
            made = 'dropped'
        codes_text = json.dumps(new_codes, indent=2, sort_keys=True) + '\n' if made else None
    save(LIST, show_list(found_list))
    if codes_text:
        save(CODES, codes_text)
    with lock:
        monitors[:] = found_list
        if made:
            codes.clear()
            codes.update(new_codes)
        status.setdefault(found['id'], {'state': 'waiting', 'since': time.time(), 'detail': '', 'fails': 0, 'last': 0})
        row = check_row(found)
    cache.clear()
    if made == 'made':
        note(f"push check {found['name']}: a code was made (see push-codes.json)")
    elif made == 'dropped':
        note(f"check {found['name']}: no longer a push check, so its push code was dropped")
    note(f"check {found['name']}: saved")
    return row, {}


def move_check(cid, by, note=None):
    """Move a check up (-1) or down (+1) in the list, which is the order the page shows."""
    note = note or (lambda text: None)
    with lock:
        ids = [m['id'] for m in monitors]
        if cid not in ids:
            return False
        i = ids.index(cid)
        j = max(0, min(len(ids) - 1, i + int(by)))
        if i == j:
            return True
        found = list(monitors)
        found.insert(j, found.pop(i))
        text = show_list(found)
    save(LIST, text)
    with lock:
        monitors[:] = found
    note(f"check {found[j].get('name')}: moved {'up' if j < i else 'down'}")
    return True


def drop_history(cid):
    """Remove a check's rows from every month of services/ and checks/: its outages, uptime and response times."""
    want = str(cid)
    for folder in (EVENTS, CHECKS_DIR):
        for name in months(folder):
            path = f'{folder}/{name}'
            with open(path, newline='', encoding='utf-8') as fh:
                rows = list(csv.reader(fh))
            if not rows:
                continue
            left = [rows[0]] + [r for r in rows[1:] if not (len(r) > 1 and r[1] == want)]
            if len(left) == len(rows):
                continue
            buf = io.StringIO()
            csv.writer(buf, lineterminator='\n').writerows(left)
            save(path, buf.getvalue())


def remove_check(cid, note=None):
    """Take a check off the list, drop its push code and every stored row it has, and remember its id so no new check
    ever takes it."""
    note = note or (lambda text: None)
    with lock:
        m = next((m for m in monitors if m['id'] == cid), None)
        if m is None:
            return False
        left = [x for x in monitors if x['id'] != cid]
        new_codes = {k: v for k, v in codes.items() if k != str(cid)}
        code_gone = str(cid) in codes
        text = show_list(left)
        codes_text = json.dumps(new_codes, indent=2, sort_keys=True) + '\n' if code_gone else None
    save(LIST, text)
    if codes_text:
        save(CODES, codes_text)
    with lock:
        monitors[:] = left
        if code_gone:
            codes.clear()
            codes.update(new_codes)
        status.pop(cid, None)
        pushes.pop(cid, None)
        samples.pop(cid, None)
    remember_removed(cid)
    drop_history(cid)
    cache.clear()
    note(f"check {m.get('name')}: removed, with its stored history")
    return True


def new_code(cid, note=None):
    """Make a push check a fresh code (its old URL stops working), save it, and return the check with its new path."""
    note = note or (lambda text: None)
    with lock:
        m = next((m for m in monitors if m['id'] == cid and m.get('type') == 'push'), None)
        if m is None:
            return None
        new_codes = dict(codes)
        new_codes[str(cid)] = secrets.token_hex(16)
        text = json.dumps(new_codes, indent=2, sort_keys=True) + '\n'
    save(CODES, text)
    with lock:
        codes.clear()
        codes.update(new_codes)
        row = check_row(m)
    note(f"push check {m.get('name')}: a new code was made (see push-codes.json)")
    return row


def push_info(m):
    """A push check's path and a one-line curl example: the code travels in the path, never in monitors.json."""
    code = codes.get(str(m['id']))
    if not code or m.get('type') != 'push':
        return {}
    path = f'/api/push/{code}'
    return {'push': path, 'url': f'http://homeassistant.local:8790{path}',
            'curl': f'curl "http://homeassistant.local:8790{path}?status=up&msg=done"'}


def check_row(m):
    """One check as the Checks screen edits it: every field the form shows, its live state, and a push check's path."""
    row = {'id': m['id'], 'name': m.get('name') or str(m['id']), 'type': m.get('type', '')}
    for key in FORM_FIELDS:
        if key in m and m[key] not in ('', None):
            row[key] = m[key]
    row.update(status.get(m['id'], {}))
    row.update(push_info(m))
    return row


def checks_payload(can_edit=False):
    """Everything the Checks screen needs: each check as the form edits it, the groups already in use, and whether
    this request may change anything (only Home Assistant's ingress may)."""
    with lock:
        rows = [check_row(m) for m in monitors]
        groups = sorted({m['group'] for m in monitors if m.get('group')})
    return {'checks': rows, 'groups': groups, 'can_edit': bool(can_edit)}


# ---- the checks: each returns (ok, detail, milliseconds) ----

def check_ping(m):
    started = time.time()
    r = subprocess.run(['ping', '-c', '1', '-W', '3', m['host']], capture_output=True, text=True, timeout=10, check=False)
    return r.returncode == 0, '' if r.returncode == 0 else 'no answer', (time.time() - started) * 1000


def check_port(m):
    started = time.time()
    with socket.create_connection((m['host'], int(m['port'])), timeout=10):
        pass
    return True, '', (time.time() - started) * 1000


def check_http(m):
    """The page answers: any 200-299, or the exact status in `expect` when the check names one."""
    started = time.time()
    req = urllib.request.Request(m['url'], headers={'User-Agent': 'Home watch'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    try:
        want = int(m['expect']) if m.get('expect') not in (None, '') else None
    except (TypeError, ValueError):
        want = None
    ok = code == want if want is not None else 200 <= code < 300
    return ok, '' if ok else (f'answered {code}, expected {want}' if want is not None else f'answered {code}'), \
        (time.time() - started) * 1000


def check_dns(m):
    """One A query over UDP, built by hand (no library): up if the server answers it with at least one record."""
    started = time.time()
    qid = random.randint(0, 0xFFFF)
    question = b''.join(bytes([len(p)]) + p.encode() for p in m['name_to_look_up'].split('.')) + b'\0'
    packet = struct.pack('>HHHHHH', qid, 0x0100, 1, 0, 0, 0) + question + struct.pack('>HH', 1, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(5)
        s.sendto(packet, (m['server'], int(m.get('port') or 53)))
        reply = s.recv(1500)
    rid, flags, _, answers = struct.unpack('>HHHH', reply[:8])
    rcode = flags & 0xF
    ok = rid == qid and rcode == 0 and answers > 0
    return ok, '' if ok else (f'error code {rcode}' if rcode else 'no answer records'), (time.time() - started) * 1000


def check_dyndns(m):
    """The dynamic-DNS name still points to the network's real, current public IP. Doesn't touch whatever
    updates the name (the router's own script, a client, its token): asks a public IP-echo service what address
    the network is actually on, and compares. Not run until it already learned that real address; an
    api.ipify.org hiccup never flags this as a DNS problem."""
    started = time.time()
    try:
        real = urllib.request.urlopen('https://api.ipify.org', timeout=10).read().decode().strip()
    except Exception:
        return True, '', (time.time() - started) * 1000
    try:
        resolved = socket.gethostbyname(m['hostname'])
    except OSError as e:
        return False, f"can't look up {m['hostname']}: {e}", (time.time() - started) * 1000
    ok = resolved == real
    detail = '' if ok else f"{m['hostname']} points to {resolved}, not the real address {real}"
    return ok, detail, (time.time() - started) * 1000


def check_push(m):
    got = pushes.get(m['id'])  # take_push files it under the check's id, not its code
    if not got:
        return None, 'no report yet', 0
    when, ok, msg = got
    if time.time() - when > int(m.get('every', 60)) + 30:
        return False, f"no report for {int((time.time() - when) / 60)} min", 0
    return ok, '' if ok else (msg or 'reported down'), 0


def check_machine(m):
    """One of Home watch's own machines reported within `quiet` seconds (900 if not set) and with no problem in its
    report. With a `host`, it also pings that address every round, the way a ping check does: down when the address
    does not answer, and down when it answers but the report is stale. Its response time is then the ping's."""
    got = machine(m['machine']) if machine else None
    ms = 0
    if m.get('host'):
        ok, _, ms = check_ping(m)
        if not ok:
            return False, 'Not on the network', ms
    if not got:
        return None, 'no report yet', ms
    quiet = time.time() - got.get('last', 0)
    if quiet > int(m.get('quiet', 900)):
        stopped = 'On the network, but its reporter stopped sending' if m.get('host') else f"no report for {int(quiet / 60)} min"
        return False, stopped, ms
    problems = got.get('problems') or {}
    return not problems, '; '.join(problems.values())[:200], ms


def check_note(m):
    """One of Home watch's own machines is still reporting, and the note its report carries says what this check
    expects. `label` names the note exactly; `good_text` and `bad_text` are plain, case-insensitive substrings of its
    text. bad_text is looked for first, so "not answering" can be bad while "answering" is good. A report older than
    `quiet` seconds (900 by default) is down, since the machine has stopped saying anything."""
    got = machine(m['machine']) if machine else None
    if not got:
        return None, 'no report yet', 0
    quiet = time.time() - got.get('last', 0)
    if quiet > int(m.get('quiet') or 900):
        return False, f"{m['machine']} hasn't reported for {int(quiet / 60)} minutes", 0
    label = str(m.get('label') or '')
    notes = got.get('notes') or {}
    if label not in notes:
        return False, f'No note called {label}', 0
    text = str(notes[label])
    low = text.lower()
    bad = str(m.get('bad_text') or '').strip().lower()
    if bad and bad in low:
        return False, text[:120], 0
    good = str(m.get('good_text') or '').strip().lower()
    if good and good not in low:
        return False, text[:120], 0
    return True, '', 0


CHECKS = {'ping': check_ping, 'port': check_port, 'http': check_http, 'dns': check_dns, 'dyndns': check_dyndns,
          'push': check_push, 'note': check_note, 'machine': check_machine}


def run_one(m, options, note):
    cid = m['id']
    try:
        ok, detail, ms = CHECKS[m['type']](m)
    except Exception as e:  # a refused connection, a timeout...
        ok, detail, ms = False, str(e)[:120] or type(e).__name__, 0
    now = time.time()
    with lock:
        s = status.get(cid)
        if s is None:  # taken off the list while this round was running
            return
        if ok is not None:
            got = samples.setdefault(cid, [0, 0, 0.0, 0])
            got[1] += 1
            if ok:
                got[0] += 1
                if ms:
                    got[2] += ms
                    got[3] += 1
        s['last'], s['ms'] = now, round(ms)
        old = s['state']
        if ok is None:  # a push monitor nothing has reported to yet, or a machine with no report yet
            new = 'waiting'
        elif ok:
            s['fails'] = 0
            new = 'up'
        else:
            s['fails'] += 1
            gone = 'off' if m.get('off_ok') else 'down'
            new = gone if s['fails'] > int(m.get('retries', 0)) else ('pending' if old != gone else gone)
        s['detail'] = detail if not ok else ''
        if new == old:
            return
        s['state'], s['since'] = new, now
    if new in ('up', 'down', 'off') or old == 'waiting':
        log_event(now, cid, new, detail)
    if m.get('heal') and (new == 'down' or (new == 'up' and old == 'down')):
        heal(m, new, detail, options, note)


def log_event(now, cid, state, detail):
    os.makedirs(EVENTS, exist_ok=True)
    path = f"{EVENTS}/{datetime.fromtimestamp(now):%Y-%m}.csv"
    new = not os.path.exists(path)
    with open(path, 'a', encoding='utf-8') as f:
        if new:
            f.write('time,id,status,detail\n')
        clean = detail.replace('"', "'")
        f.write(f'{datetime.fromtimestamp(now):%Y-%m-%d %H:%M:%S},{cid},{state},"{clean}"\n')


def app_name(m):
    """The app a check's self-heal script restarts: its `app` field, else its name made into a slug, so
    "Media Hub" and "media_hub" both name the same app."""
    got = str(m.get('app') or '').strip()
    if not got:
        got = str(m.get('name', ''))
    return re.sub(r'[^a-z0-9]+', '_', got.lower()).strip('_')


class DockerSocket(http.client.HTTPConnection):
    """HTTP to the Docker Engine over its unix socket (no extra library)."""

    def __init__(self, path=DOCKER_SOCKET, timeout=60):
        super().__init__('localhost', timeout=timeout)
        self.socket_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def restart_container(name):
    """Restart one container through the Docker Engine API; raises on anything but 204."""
    conn = DockerSocket()
    try:
        conn.request('POST', f'/containers/{urllib.parse.quote(name, safe="")}/restart?t=20')
        resp = conn.getresponse()
        body = resp.read().decode('utf-8', 'replace')[:200]
        if resp.status != 204:
            raise RuntimeError(f'docker {resp.status}: {body}')
    finally:
        conn.close()


def heal_container(m, state, options, note, now=None):
    """Outside Home Assistant: restart the check's container (settings.json's heal_containers, {app: container}) when it
    goes down. The same rule the Home Assistant script has: one restart, then HEAL_COOLDOWN before the next for that
    app. Only when the add-on's self_heal option is on."""
    app = app_name(m)
    name = heal_containers.get(app)
    if state != 'down':
        return
    if not options.get('self_heal'):
        note(f"would restart {name} for {m['name']} but self_heal is off")
        return
    now = time.time() if now is None else now
    if now - healed.get(app, 0) < HEAL_COOLDOWN:
        note(f"self-heal waits: {name} was restarted under {HEAL_COOLDOWN // 60} min ago")
        return
    healed[app] = now
    try:
        restart_container(name)
        note(f"self-heal: restarted {name} ({m['name']} down)")
    except Exception as e:
        note(f"self-heal could not restart {name}: {e}")


def heal(m, state, detail, options, note):
    """Post the message Home Assistant's self-heal automation reads, naming the script it should run
    (settings.json's heal_script with {app} filled in). Only when the add-on's self_heal option is on.
    An app listed in heal_containers is restarted through Docker instead (heal_container)."""
    if app_name(m) in heal_containers:
        return heal_container(m, state, options, note)
    hook = options.get('self_heal_webhook')
    script = heal_script.replace('{app}', app_name(m))
    if not options.get('self_heal') or not hook:
        note(f"would self-heal {m['name']} ({state}) with {script} but self_heal is off")
        return
    msg = f"[{m['name']}] [{'Up' if state == 'up' else 'Down'}] {detail}".strip()
    body = json.dumps({'heartbeat': {'status': 1 if state == 'up' else 0, 'msg': detail, 'time': datetime.now().isoformat()},
                       'monitor': {'name': m['name'], 'type': m['type'], 'script': script}, 'msg': msg}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(hook, data=body, headers={'Content-Type': 'application/json'}), timeout=15).read()
        note(f"self-heal sent: {m['name']} {state} ({script})")
    except Exception as e:
        note(f"self-heal not sent for {m['name']}: {e}")


def take_push(code, query):
    """A push check's report in. The code finds the check's id in push-codes.json; every code is compared with
    hmac.compare_digest, so neither a wrong code nor the time an answer took says anything about the right one."""
    given = str(code).encode()
    with lock:
        cid = None
        for key, saved in codes.items():
            if hmac.compare_digest(saved.encode(), given):
                try:
                    cid = int(key)
                except (TypeError, ValueError):
                    return False  # a code left under a name from before ids: no check of that name any more
                if cid not in {m['id'] for m in monitors}:
                    return False  # a code for a check that has since been removed
        if cid is None:
            return False
        st = (query.get('status', ['up'])[0] or 'up').lower()
        pushes[cid] = (time.time(), st == 'up', query.get('msg', [''])[0][:200])
    return True


def health(now=None):
    """(ok, reason) for the Supervisor's watchdog, which asks GET /health (watch.py) whether the add-on is well: the
    HTTP server answering is half of it (that is this call), and the other half is this module's check loop still going
    round. A loop that has not begun a round for HEALTH_STALE seconds is stuck (or has died), so the add-on says so and
    the Supervisor restarts it. The answer holds nothing about the house and no secret."""
    now = time.time() if now is None else now
    quiet = now - last_round
    if quiet > HEALTH_STALE:
        return False, f'the check loop has not run for {quiet / 60:.0f} min'
    return True, 'ok'


def loop(options, note):
    global last_round
    load(note)
    note(f"service checks: {len(monitors)} monitors")
    pool = ThreadPoolExecutor(max_workers=16)
    flushed = tidied = time.time()
    while True:
        now = last_round = time.time()  # /health reads this: a loop that stops marking its rounds is a stuck add-on
        if now - flushed >= FLUSH:
            flushed = now
            try:
                flush(now)
            except OSError as e:
                note(f"checks file not written: {e}")
        if now - tidied >= 86400:
            tidied = now
            tidy(note)
        with lock:
            due = [m for m in monitors if now - status[m['id']]['last'] >= int(m.get('every', 60)) - 1]
            for m in due:
                status[m['id']]['last'] = now  # not picked again while it runs
        for m in due:
            pool.submit(run_one, m, options, note)
        time.sleep(5)


def snapshot():
    """Each check as the page shows it. A push check also carries its push path, so it can be copied from the
    check's page into whatever reports in (the page is home-network only, like the rest of the add-on); a check with
    a host (a ping, a port, or a machine check that also pings its device) carries the address it pings."""
    out = []
    with lock:
        for m in monitors:
            row = {'id': m['id'], 'name': m.get('name', str(m['id'])), 'group': m.get('group', ''),
                   'type': m['type'], 'machine': m.get('machine', ''), **status[m['id']]}
            if m.get('host'):
                row['host'] = m['host']
            if m['type'] == 'push' and codes.get(str(m['id'])):
                row['push'] = f"/api/push/{codes[str(m['id'])]}"
            out.append(row)
    return out


def flush(now):
    """One row per check for the last 5 minutes: how many of its checks passed and its average response time."""
    with lock:
        got = {cid: sample for cid, sample in samples.items() if cid in status}  # a removed check writes nothing
        samples.clear()
    if not got:
        return
    os.makedirs(CHECKS_DIR, exist_ok=True)
    path = f"{CHECKS_DIR}/{datetime.fromtimestamp(now):%Y-%m}.csv"
    new = not os.path.exists(path)
    stamp = f'{datetime.fromtimestamp(now):%Y-%m-%d %H:%M}'
    with open(path, 'a', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        if new:
            w.writerow(['time', 'id', 'ok', 'checks', 'ms'])
        for cid, (ok, checks, ms, count) in got.items():
            w.writerow([stamp, cid, ok, checks, round(ms / count) if count else ''])


def months(folder):
    return sorted(f for f in os.listdir(folder) if re.fullmatch(r'\d{4}-\d{2}\.csv', f)) if os.path.isdir(folder) else []


def tidy(note):
    for folder in (EVENTS, CHECKS_DIR):
        for f in months(folder)[:-KEEP_MONTHS]:
            os.remove(f'{folder}/{f}')
            note(f"removed old service history {f}")


def changes(days):
    """check id -> [(time, state, detail)], oldest first, reaching back at least `days` (plus the state before)."""
    out = {}
    for f in months(EVENTS)[-(days // 28 + 2):]:
        with open(f'{EVENTS}/{f}', encoding='utf-8') as fh:
            for r in csv.DictReader(fh):
                try:
                    t = datetime.strptime(r['time'], '%Y-%m-%d %H:%M:%S').timestamp()
                    cid = int(r['id'])
                except (KeyError, ValueError, TypeError):
                    continue  # a name row from before ids, or a broken line
                out.setdefault(cid, []).append((t, r['status'], r.get('detail') or ''))
    return out


def spent(timeline, a, b):
    """Seconds a check spent up, down and off between a and b (pending and waiting don't count)."""
    out = {'up': 0.0, 'down': 0.0, 'off': 0.0}
    state, t0 = None, a
    for t, st, _ in timeline:
        if t <= a:
            state = st
            continue
        if t >= b:
            break
        if state in out:
            out[state] += t - t0
        state, t0 = st, t
    if state in out:
        out[state] += b - t0
    return out


def summary():
    """Uptime over 24 h, 30 days and a year, and a 24-tick strip (one per hour, oldest first) for every check.
    Worked out at most every 30 seconds (every open page asks each minute)."""
    now = time.time()
    if now - cache.get('at', 0) < 30:
        return cache['uptime'], cache['strips']
    uptime, strips = {}, {}
    for cid, timeline in changes(366).items():
        u = {}
        for key, days in (('d1', 1), ('d30', 30), ('d365', 365)):
            got = spent(timeline, now - days * 86400, now)
            total = got['up'] + got['down']
            u[key] = round(100 * got['up'] / total, 2) if total else None
        uptime[cid] = u
        ticks = []
        for i in range(24):
            got = spent(timeline, now - (24 - i) * 3600, now - (23 - i) * 3600)
            ticks.append('part' if got['down'] and got['up'] else 'down' if got['down'] else 'up' if got['up']
                         else 'off' if got['off'] else '')
        strips[cid] = ticks
    cache.update(at=now, uptime=uptime, strips=strips)
    return uptime, strips


def label(cid):
    """The name a check's id is shown by now, so a rename shows on its old outages too; the id if it is gone."""
    for m in monitors:
        if m['id'] == cid:
            return m.get('name', str(cid))
    return str(cid)


def outages(days=30):
    """Each time a check was down: its id, its name now, start, end (None = still down), what it said. Newest first."""
    since = time.time() - days * 86400
    out = []
    for cid, timeline in changes(days).items():
        start = None
        for t, st, detail in timeline:
            if st == 'down' and start is None:
                start = (t, detail)
            elif st in ('up', 'off') and start is not None:
                if t >= since:
                    out.append({'id': cid, 'name': label(cid), 'start': start[0], 'end': t, 'detail': start[1]})
                start = None
        if start is not None:
            out.append({'id': cid, 'name': label(cid), 'start': start[0], 'end': None, 'detail': start[1]})
    return sorted(out, key=lambda o: -o['start'])


def points(cid, days, count=300):
    """A check's response time (ms) and share of checks passed (%), averaged into about 300 points."""
    step = max(FLUSH, days * 86400 / count)
    since = time.time() - days * 86400
    buckets = {}
    want = str(cid)
    for f in months(CHECKS_DIR)[-(days // 28 + 2):]:
        with open(f'{CHECKS_DIR}/{f}', encoding='utf-8') as fh:
            for r in csv.DictReader(fh):
                if r.get('id') != want:
                    continue
                try:
                    t = datetime.strptime(r['time'], '%Y-%m-%d %H:%M').timestamp()
                    ok, checks = int(r['ok']), int(r['checks'])
                except (KeyError, ValueError, TypeError):
                    continue
                if t < since:
                    continue
                b = buckets.setdefault(int(t // step), [0, 0, 0.0, 0])
                b[0] += ok
                b[1] += checks
                if r.get('ms'):
                    b[2] += float(r['ms'])
                    b[3] += 1
    keys = sorted(buckets)
    return {'t': [int(k * step) for k in keys], 'series': {
        'ms': [round(buckets[k][2] / buckets[k][3]) if buckets[k][3] else None for k in keys],
        'up': [round(100 * buckets[k][0] / buckets[k][1], 1) if buckets[k][1] else None for k in keys]}}
