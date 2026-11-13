"""Home watch's service checks: ping, port, http, dns, dyndns, push and machine checks, on a list you edit by hand.

The list is /share/home-watch/monitors.json (plain, editable). A data folder with no list in it yet gets the example
list shipped in addon/monitors.json; a list that is already there is never overwritten. Each check runs every
`every` seconds:
- ping: a device answers; port: a TCP port takes a connection; http: a page answers 200-299; dns: a DNS server
  answers a name;
- push: something else reports in at /api/push/<code>?status=up&msg=..., and silence longer than `every` = down;
- machine (Home watch's own): one of its machines reported within `quiet` seconds (15 min if not set) with no problem.
  A machine check may also name a `host`: each round it then pings that address the way a ping check does, and is down
  when the address does not answer ("Not on the network") or answers but the report is stale ("On the network, but its
  reporter stopped sending"); its response time is then the ping's.

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
"""

import csv
import hmac
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
DEFAULT_LIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'monitors.json')
HEAL_SCRIPT = 'script.home_watch_heal_{app}'  # the Home Assistant script self-heal calls; {app} = the check's app

lock = threading.Lock()
monitors = []
status = {}  # check id -> {state: up|pending|down|waiting, since, detail, fails, last, ms}
pushes = {}  # check id -> (time, ok, msg)
codes = {}  # check id (as a string) -> its push code; a new dict on every load()
machine = None  # watch.py sets this: machine id -> its latest report (or None)
heal_script = HEAL_SCRIPT  # watch.py sets this from settings.json
samples = {}  # check id -> [ok, checks, ms total, ms count] since the last row was written
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


def stored_ids():
    """The ids any stored row already carries: a check's number is never handed out twice, so a check removed long
    ago cannot pass its history to a new one. Read only when a number has to be made."""
    found = set()
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
    if changed:
        save(LIST, show_list(kept))
        note(f"monitors.json: every check now has its id ({len(kept)} checks)")
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


CHECKS = {'ping': check_ping, 'port': check_port, 'http': check_http, 'dns': check_dns, 'dyndns': check_dyndns,
          'push': check_push, 'machine': check_machine}


def run_one(m, options, note):
    cid = m['id']
    try:
        ok, detail, ms = CHECKS[m['type']](m)
    except Exception as e:  # a refused connection, a timeout...
        ok, detail, ms = False, str(e)[:120] or type(e).__name__, 0
    now = time.time()
    with lock:
        if ok is not None:
            got = samples.setdefault(cid, [0, 0, 0.0, 0])
            got[1] += 1
            if ok:
                got[0] += 1
                if ms:
                    got[2] += ms
                    got[3] += 1
        s = status[cid]
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
