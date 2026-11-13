"""Home watch's service checks: ping, port, http, dns, dyndns, push and machine checks, on a list you edit by hand.

The list is /share/home-watch/monitors.json (plain, editable). A data folder with no list in it yet gets the example
list shipped in addon/monitors.json; a list that is already there is never overwritten. Each check runs every
`every` seconds:
- ping: a device answers; port: a TCP port takes a connection; http: a page answers 200-299; dns: a DNS server
  answers a name;
- push: something else reports in at /api/push/<code>?status=up&msg=..., and silence longer than `every` = down;
- machine (Home watch's own): one of its machines reported within `quiet` seconds (15 min if not set) with no problem.

The push checks' codes are NOT in the list: they live in /share/home-watch/push-codes.json ({"<check name>": "<code>"},
made 0600, written whole). The list holds no secrets, so it can be copied or committed; copying a new check list over
it never loses the codes. A list that still keeps a code in a "token" field is migrated on load: the code moves into
push-codes.json, and the list is rewritten without it. A push check with no code at all gets one made
(secrets.token_hex(16)) and a log line naming the check (never the code). /api/push/<code> finds its check by
comparing the code with hmac.compare_digest, so a wrong code learns nothing from how long the answer took.
A check that fails is "pending" until it has failed `retries` more times in a row, then "down", or "off" for a monitor
with "off_ok": true (a TV switched off at the wall: normal, grey on the page, not counted against its uptime).
Changes go in /share/home-watch/services/<year-month>.csv (time, name, status, detail); from them come the uptime
(24 h, 30 days, a year), the hour-by-hour strip and the outage list. Every 5 minutes each check's results go in
/share/home-watch/checks/<year-month>.csv (time, name, ok, checks, ms: its response times). Both kept 12 months.
Monitors with "heal": true post a webhook message to Home Assistant's self-heal automation when they go down or come
back, but only when the add-on's `self_heal` option is on. The message names the script that restarts the app
(monitor.script), built from the `heal_script` setting in settings.json (default script.home_watch_heal_{app}) and
the check's `app` field, or its name made into an id ("Media Hub" -> media_hub) when it has no `app`.

Borrowed from Uptime Kuma (github.com/louislam/uptime-kuma, MIT License, Copyright (c) 2021 Louis Lam): the monitor
types and their up/down rules, retries with a pending state, the push URL shape (/api/push/<code>?status=&msg=) and
the webhook message shape ({"heartbeat": {"status", "msg"}, "monitor": {"name"}, "msg"}) that the self-heal
automation reads.
"""

import csv
import hmac
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
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

DATA = os.environ.get('HOME_WATCH_DATA', '/share/home-watch')
LIST = f'{DATA}/monitors.json'
CODES = f'{DATA}/push-codes.json'  # {"<check name>": "<code>"}; never in git, so replacing the list keeps them
EVENTS = f'{DATA}/services'
CHECKS_DIR = f'{DATA}/checks'
KEEP_MONTHS = 12
FLUSH = 300  # seconds between rows in the checks file
DEFAULT_LIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'monitors.json')
HEAL_SCRIPT = 'script.home_watch_heal_{app}'  # the Home Assistant script self-heal calls; {app} = the check's app

lock = threading.Lock()
monitors = []
status = {}  # name -> {state: up|pending|down|waiting, since, detail, fails, last, ms}
pushes = {}  # check name -> (time, ok, msg)
codes = {}  # check name -> its push code; a new dict on every load()
machine = None  # watch.py sets this: machine id -> its latest report (or None)
heal_script = HEAL_SCRIPT  # watch.py sets this from settings.json
samples = {}  # name -> [ok, checks, ms total, ms count] since the last row was written
cache = {}  # what summary() last worked out, and when


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
    """name -> code. A missing file is normal (the first run); an unreadable one is said out loud (new codes then)."""
    try:
        with open(CODES, encoding='utf-8') as f:
            got = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        note(f"push-codes.json unreadable ({e}); making new codes for the push checks")
        return {}
    return {str(k): str(v) for k, v in got.items() if v}


def load(note=None):
    """Read the check list and the push codes. A code still kept in a check's "token" field moves into
    push-codes.json and out of the list; a push check with no code gets one, and one log line names it."""
    global codes
    note = note or (lambda text: None)
    if not os.path.exists(LIST):
        shutil.copy(DEFAULT_LIST, LIST)
    with open(LIST, encoding='utf-8') as f:
        found = json.load(f)
    known = read_codes(note)
    moved = made = False
    for m in found:
        if 'token' in m:  # the old place for a push check's code, or where one copy of the list still keeps it
            if not known.get(m['name']):
                known[m['name']] = m.pop('token')
            else:
                m.pop('token')
            moved = True
    for m in found:
        if m.get('type') == 'push' and not known.get(m['name']):
            known[m['name']] = secrets.token_hex(16)
            note(f"push check {m['name']}: no code yet, so one was made (see push-codes.json)")
            made = True
    if moved:
        save(LIST, show_list(found))
        note("monitors.json: its push codes moved to push-codes.json and out of the list")
    if moved or made:
        save(CODES, json.dumps(known, indent=2) + '\n')
    with lock:
        codes = known
        monitors[:] = found
        for m in found:
            status.setdefault(m['name'], {'state': 'waiting', 'since': time.time(), 'detail': '', 'fails': 0, 'last': 0})


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
    started = time.time()
    req = urllib.request.Request(m['url'], headers={'User-Agent': 'Home watch'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    ok = 200 <= code < 300
    return ok, '' if ok else f'answered {code}', (time.time() - started) * 1000


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
    got = pushes.get(m['name'])  # take_push files it under the check's name, not its code
    if not got:
        return None, 'no report yet', 0
    when, ok, msg = got
    if time.time() - when > int(m.get('every', 60)) + 30:
        return False, f"no report for {int((time.time() - when) / 60)} min", 0
    return ok, '' if ok else (msg or 'reported down'), 0


def check_machine(m):
    got = machine(m['machine']) if machine else None
    if not got:
        return None, 'no report yet', 0
    quiet = time.time() - got.get('last', 0)
    if quiet > int(m.get('quiet', 900)):
        return False, f"no report for {int(quiet / 60)} min", 0
    problems = got.get('problems') or {}
    return not problems, '; '.join(problems.values())[:200], 0


CHECKS = {'ping': check_ping, 'port': check_port, 'http': check_http, 'dns': check_dns, 'dyndns': check_dyndns,
          'push': check_push, 'machine': check_machine}


def run_one(m, options, note):
    name = m['name']
    try:
        ok, detail, ms = CHECKS[m['type']](m)
    except Exception as e:  # a refused connection, a timeout...
        ok, detail, ms = False, str(e)[:120] or type(e).__name__, 0
    now = time.time()
    with lock:
        if ok is not None:
            got = samples.setdefault(name, [0, 0, 0.0, 0])
            got[1] += 1
            if ok:
                got[0] += 1
                if ms:
                    got[2] += ms
                    got[3] += 1
        s = status[name]
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
        log_event(now, name, new, detail)
    if m.get('heal') and (new == 'down' or (new == 'up' and old == 'down')):
        heal(m, new, detail, options, note)


def log_event(now, name, state, detail):
    os.makedirs(EVENTS, exist_ok=True)
    path = f"{EVENTS}/{datetime.fromtimestamp(now):%Y-%m}.csv"
    new = not os.path.exists(path)
    with open(path, 'a', encoding='utf-8') as f:
        if new:
            f.write('time,name,status,detail\n')
        clean = detail.replace('"', "'")
        f.write(f'{datetime.fromtimestamp(now):%Y-%m-%d %H:%M:%S},"{name}",{state},"{clean}"\n')


def app_name(m):
    """The app a check's self-heal script restarts: its `app` field, else its name made into an id, so
    "Media Hub" and "media_hub" both name the same app."""
    got = str(m.get('app') or '').strip()
    if not got:
        got = str(m.get('name', ''))
    return re.sub(r'[^a-z0-9]+', '_', got.lower()).strip('_')


def heal(m, state, detail, options, note):
    """Post the message Home Assistant's self-heal automation reads, naming the script it should run
    (settings.json's heal_script with {app} filled in). Only when the add-on's self_heal option is on."""
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
    """A push check's report in. The code names the check (push-codes.json); every code is compared with
    hmac.compare_digest, so neither a wrong code nor the time an answer took says anything about the right one."""
    given = str(code).encode()
    with lock:
        name = None
        for known, saved in codes.items():
            if hmac.compare_digest(saved.encode(), given):
                name = known
        if name is None:
            return False
        st = (query.get('status', ['up'])[0] or 'up').lower()
        pushes[name] = (time.time(), st == 'up', query.get('msg', [''])[0][:200])
    return True


def loop(options, note):
    load(note)
    note(f"service checks: {len(monitors)} monitors")
    pool = ThreadPoolExecutor(max_workers=16)
    flushed = tidied = time.time()
    while True:
        now = time.time()
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
            due = [m for m in monitors if now - status[m['name']]['last'] >= int(m.get('every', 60)) - 1]
            for m in due:
                status[m['name']]['last'] = now  # not picked again while it runs
        for m in due:
            pool.submit(run_one, m, options, note)
        time.sleep(5)


def snapshot():
    """Each check as the page shows it. A push check also carries its push path, so it can be copied from the
    check's page into whatever reports in (the page is home-network only, like the rest of the add-on)."""
    out = []
    with lock:
        for m in monitors:
            row = {'name': m['name'], 'group': m.get('group', ''), 'type': m['type'], 'machine': m.get('machine', ''),
                   **status[m['name']]}
            if m['type'] == 'push' and codes.get(m['name']):
                row['push'] = f"/api/push/{codes[m['name']]}"
            out.append(row)
    return out


def flush(now):
    """One row per check for the last 5 minutes: how many of its checks passed and its average response time."""
    with lock:
        got = dict(samples)
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
            w.writerow(['time', 'name', 'ok', 'checks', 'ms'])
        for name, (ok, checks, ms, count) in got.items():
            w.writerow([stamp, name, ok, checks, round(ms / count) if count else ''])


def months(folder):
    return sorted(f for f in os.listdir(folder) if re.fullmatch(r'\d{4}-\d{2}\.csv', f)) if os.path.isdir(folder) else []


def tidy(note):
    for folder in (EVENTS, CHECKS_DIR):
        for f in months(folder)[:-KEEP_MONTHS]:
            os.remove(f'{folder}/{f}')
            note(f"removed old service history {f}")


def changes(days):
    """name -> [(time, state, detail)], oldest first, reaching back at least `days` (plus the state before)."""
    out = {}
    for f in months(EVENTS)[-(days // 28 + 2):]:
        with open(f'{EVENTS}/{f}', encoding='utf-8') as fh:
            for r in csv.DictReader(fh):
                try:
                    t = datetime.strptime(r['time'], '%Y-%m-%d %H:%M:%S').timestamp()
                except (KeyError, ValueError, TypeError):
                    continue
                out.setdefault(r['name'], []).append((t, r['status'], r.get('detail') or ''))
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
    for name, timeline in changes(366).items():
        u = {}
        for key, days in (('d1', 1), ('d30', 30), ('d365', 365)):
            got = spent(timeline, now - days * 86400, now)
            total = got['up'] + got['down']
            u[key] = round(100 * got['up'] / total, 2) if total else None
        uptime[name] = u
        ticks = []
        for i in range(24):
            got = spent(timeline, now - (24 - i) * 3600, now - (23 - i) * 3600)
            ticks.append('part' if got['down'] and got['up'] else 'down' if got['down'] else 'up' if got['up']
                         else 'off' if got['off'] else '')
        strips[name] = ticks
    cache.update(at=now, uptime=uptime, strips=strips)
    return uptime, strips


def outages(days=30):
    """Each time a check was down: name, start, end (None = still down), what it said. Newest first."""
    since = time.time() - days * 86400
    out = []
    for name, timeline in changes(days).items():
        start = None
        for t, st, detail in timeline:
            if st == 'down' and start is None:
                start = (t, detail)
            elif st in ('up', 'off') and start is not None:
                if t >= since:
                    out.append({'name': name, 'start': start[0], 'end': t, 'detail': start[1]})
                start = None
        if start is not None:
            out.append({'name': name, 'start': start[0], 'end': None, 'detail': start[1]})
    return sorted(out, key=lambda o: -o['start'])


def points(name, days, count=300):
    """A check's response time (ms) and share of checks passed (%), averaged into about 300 points."""
    step = max(FLUSH, days * 86400 / count)
    since = time.time() - days * 86400
    buckets = {}
    for f in months(CHECKS_DIR)[-(days // 28 + 2):]:
        with open(f'{CHECKS_DIR}/{f}', encoding='utf-8') as fh:
            for r in csv.DictReader(fh):
                if r.get('name') != name:
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
