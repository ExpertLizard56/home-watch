"""Home watch: the Home Assistant add-on that keeps an eye on every machine on the home network.

Each device posts a report to port 8790 (/report) every 5 minutes; the machine the add-on runs on reports on itself.
Home watch keeps the history, runs the checks, tells your phone through Home Assistant's alert webhook (at most once
a day per problem) and serves the page (the Home Assistant sidebar, or the add-on's port).

The parts: watch.py (this: reports, checks, alerts, the web door), history.py (the CSV history), pi.py (the hub's own
report), services.py (the service checks), trail.py (each machine's busiest programs and the logs machines send),
page.html (the page).

Borrowed, with credit:
- Beszel (github.com/henrygd/beszel, MIT License, Copyright (c) 2024 henrygd): reporters send raw counters and the
  hub works out amounts from the change between two reports (Beszel's agent/deltatracker); CPU % from busy/idle
  ticks; eMMC wear (pi.py); the one-card-per-machine page.
- Uptime Kuma (MIT): the monitor types, their rules and the page's strip, uptime and outages.
- Netdata and Glances (GPL): ideas only, no code. Scrutiny (MIT): ideas only.
- Check drives.ps1: the drive checks it had, and its once-a-day-per-problem alert rule.
"""

import json
import os
import re
import signal
import sys
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import history
import pi
import services
import trail

DATA = history.DATA
STATE_FILE = f'{DATA}/state.json'
LOG_FILE = f'{DATA}/log.txt'
SETTINGS_FILE = f'{DATA}/settings.json'
OPTIONS_FILE = os.environ.get('HOME_WATCH_OPTIONS', '/data/options.json')
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'page.html')
PORT = int(os.environ.get('HOME_WATCH_PORT', '8790'))
EVERY = history.EVERY
COUNTERS = {'disk_read_bytes': 'disk_read_mb', 'disk_write_bytes': 'disk_write_mb',
            'net_rx_bytes': 'net_rx_mb', 'net_tx_bytes': 'net_tx_mb'}

LIMITS = {
    'temp': 85,              # C
    'mem': 95,               # % used, all the time for an hour
    'disk_used_pct': 90,     # the PC reporter and the hub check their own space instead
    'cpu_busy': None,        # % for an hour; off unless a machine asks for it in settings.json
}
# Address prefixes allowed to post reports and to open the page: the private ranges and Home Assistant's ingress.
DEFAULT_NETWORKS = ('10.', '127.', '192.168.', *(f'172.{n}.' for n in range(16, 32)), '172.30.32.')

lock = threading.Lock()
state = {'machines': {}, 'sent': {}, 'raw': {}}
options = {}
order = []               # machine ids in the order settings.json lists them, which is the order the page shows
labels = {}              # machine id -> the name the page shows for it, from settings.json
limits = {}              # machine id -> {temp, mem, disk_used_pct, cpu_busy} of its own, from settings.json
networks = DEFAULT_NETWORKS  # address prefixes allowed to post reports and to open the page, from settings.json
senders = {}             # syslog sender IP -> machine id, from settings.json (trail.py)
heal_script = services.HEAL_SCRIPT  # the Home Assistant script self-heal calls, {app} filled in per check


def note(text):
    line = f"{datetime.now():%Y-%m-%d %H:%M} {text}"
    print(line, flush=True)
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def save_state():
    with lock:
        text = json.dumps(state)
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, STATE_FILE)


def machine_id(text):
    return re.sub(r'[^a-z0-9-]', '', str(text or '').lower())


# ---- the settings ------------------------------------------------------------------------------------------------

def load_settings(note=None):
    """The home's own names and numbers, from /share/home-watch/settings.json (an example is addon/settings.example.json).

    Every key is optional and a missing file is the normal first run; nothing in it is secret. Read once at start:
    machines (the page's order, and labels), limits (one machine's own numbers), log_senders (which address is which
    machine, for the syslog trail), allowed_networks (who may post reports and open the page) and heal_script (the
    Home Assistant script self-heal calls, {app} filled in per check)."""
    global order, labels, limits, networks, senders, heal_script
    got, read = {}, False
    try:
        with open(SETTINGS_FILE, encoding='utf-8') as f:
            got = json.load(f)
        read = True
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        if note:
            note(f"settings.json unreadable ({e}); using the defaults")
    if not isinstance(got, dict):
        got = {}
    listed = got.get('machines')
    found = []
    if isinstance(listed, dict):  # {"<machine>": "<label>"}, in the order the page should show them
        found = [(machine_id(m), str(label or '')) for m, label in listed.items()]
    elif isinstance(listed, list):  # ["<machine>", ...], in the order the page should show them
        found = [(machine_id(m), '') for m in listed]
    found = [(m, label) for m, label in found if m]
    order = [m for m, _ in found]
    labels = {m: label for m, label in found if label}
    own = got.get('limits')
    limits = {}
    if isinstance(own, dict):
        for machine, numbers in own.items():
            if not machine_id(machine) or not isinstance(numbers, dict):
                continue
            clean = {}
            for key in LIMITS:
                try:
                    clean[key] = float(numbers[key])
                except (KeyError, TypeError, ValueError):
                    continue
            if clean:
                limits[machine_id(machine)] = clean
    asked = got.get('log_senders')
    senders = {}
    if isinstance(asked, dict):
        senders = {str(ip).strip(): machine_id(m) for ip, m in asked.items() if machine_id(m)}
    asked = got.get('allowed_networks')
    networks = tuple(str(n).strip() for n in asked if str(n).strip()) if isinstance(asked, list) else ()
    if not networks:  # nothing said: the private ranges, so a typo can't lock the page out
        networks = DEFAULT_NETWORKS
    asked = got.get('heal_script')
    heal_script = services.HEAL_SCRIPT
    if isinstance(asked, str) and '{app}' in asked:
        heal_script = asked.strip()
    elif asked and note:  # a pattern with no {app} would call one script for every app: say so
        note(f"settings.json: heal_script has no {{app}} in it, so it is not used ({services.HEAL_SCRIPT})")
    if note and read:
        note(f"settings.json: {len(order)} machines in order, {len(labels)} labels, {len(limits)} with their own limits, "
             f"{len(senders)} log senders, {len(networks)} address prefixes allowed")


def limit_for(machine, key):
    """One machine's own number from settings.json, else the generic one."""
    got = limits.get(machine, {}).get(key)
    return LIMITS[key] if got is None else got


# ---- reports ----------------------------------------------------------------------------------------------------

def take_report(rep):
    """A report: {machine, name, kind, metrics:{...}, counters:{...}, cpu_ticks:[total, idle], problems:{key: message}}."""
    machine = machine_id(rep.get('machine'))
    if not machine:
        raise ValueError('no machine')
    now = time.time()
    metrics = {k: history.num(v) for k, v in (rep.get('metrics') or {}).items()}
    with lock:
        prev = state['raw'].get(machine)
        raw = {k: history.num(v) for k, v in (rep.get('counters') or {}).items()}
        ticks = rep.get('cpu_ticks')
        if ticks:
            raw['cpu_total'], raw['cpu_idle'] = history.num(ticks[0]), history.num(ticks[1])
        raw['t'] = now
        # Beszel's delta idea: amount = this counter minus last report's. A counter that went down = a restart: skip.
        if prev and now - prev.get('t', 0) < 3 * EVERY:
            for key, col in COUNTERS.items():
                a, b = prev.get(key), raw.get(key)
                if a is not None and b is not None and b >= a:
                    metrics[col] = round((b - a) / 1e6, 2)
            if 'cpu' not in metrics and prev.get('cpu_total') is not None and raw.get('cpu_total') is not None:
                dt, di = raw['cpu_total'] - prev['cpu_total'], raw['cpu_idle'] - prev['cpu_idle']
                if dt > 0:
                    metrics['cpu'] = round(100 * (dt - di) / dt, 1)
        state['raw'][machine] = raw
        m = state['machines'].setdefault(machine, {})
        m.update(name=rep.get('name') or machine, last=now, metrics=metrics,
                 problems=dict(rep.get('problems') or {}), kind=rep.get('kind', ''))
        procs = trail.clean_procs(rep.get('procs'))
        if procs:
            m['procs'] = procs
        busy = limit_for(machine, 'cpu_busy')
        if busy and (metrics.get('cpu') or 0) >= busy:
            m.setdefault('cpu_high_since', now)
        elif 'cpu' in metrics:
            m.pop('cpu_high_since', None)
        if (metrics.get('mem') or 0) >= limit_for(machine, 'mem'):
            m.setdefault('mem_high_since', now)
        else:
            m.pop('mem_high_since', None)
    history.write(machine, now, metrics)
    if procs:
        trail.write_procs(machine, now, procs)


def machine_report(machine):
    with lock:
        m = state['machines'].get(machine)
        return dict(m) if m else None


# ---- checks and alerts --------------------------------------------------------------------------------------------

def current_problems():
    now = time.time()
    silent_after = float(options.get('silent_hours', 6)) * 3600
    out = {}
    with lock:
        for machine, m in state['machines'].items():
            name = m.get('name', machine)
            quiet = now - m.get('last', 0)
            if quiet > silent_after:
                if quiet < 7 * 86400:  # after a week it only shows on the page
                    out[f'{machine}-silent'] = (name, f"Hasn't reported for {quiet / 3600:.0f} hours. Is it off or stuck?")
                continue
            for key, msg in (m.get('problems') or {}).items():
                out[f'{machine}-{key}'] = (name, msg)
            mt = m.get('metrics') or {}
            t = mt.get('temp')
            if t and t > limit_for(machine, 'temp'):
                out[f'{machine}-hot'] = (name, f"Running hot: {t:.0f} C")
            used = mt.get('disk_used_pct')
            if m.get('kind') not in ('pi', 'windows') and used and used > limit_for(machine, 'disk_used_pct'):
                out[f'{machine}-space'] = (name, f"Drive is {used:.0f}% full")
            since = m.get('mem_high_since')
            if since and now - since >= 3600:
                mem = limit_for(machine, 'mem')
                out[f'{machine}-memory'] = (name, f"Memory has been over {mem:g}% full for {(now - since) / 3600:.0f} hours")
            since = m.get('cpu_high_since')
            if since and now - since >= 3600:
                top = ', '.join(f"{p} {v:.0f}%" for p, v in (m.get('procs') or [])[:3])
                out[f'{machine}-cpu'] = (name, f"CPU busy ({mt.get('cpu', 0):.0f}%) for {(now - since) / 3600:.0f} hours"
                                         + (f". Busiest: {top}" if top else ''))
    return out


def send(title, message):
    hook = options.get('alert_webhook')
    if not hook:
        note(f"no alert_webhook set: {title} - {message}")
        return False
    body = urlencode({'title': title, 'message': message}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(hook, data=body, method='POST'), timeout=20).read()
        return True
    except Exception as e:
        note(f"could not send: {title} - {message} ({e})")
        return False


def check():
    """Alert once a day per problem. A problem that clears is forgotten, so it alerts again if it returns."""
    problems = current_problems()
    with lock:
        sent = dict(state['sent'])
    keep = {}
    for key, (title, msg) in problems.items():
        when = sent.get(key)
        if not when or time.time() - when >= 86400:
            note(f"ALERT {key} - {msg}")
            if send(title, msg):
                when = time.time()
        if when:
            keep[key] = when
    with lock:
        state['sent'] = keep
        state['problems'] = {k: list(v) for k, v in problems.items()}
    save_state()


def loop():
    last_tidy = 0
    while True:
        started = time.time()
        for what, job in (('Hub report', lambda: take_report(pi.report(note))), ('check', check)):
            try:
                job()
            except Exception as e:
                note(f"{what} failed: {e}")
        if started - last_tidy > 86400:
            history.tidy(note)
            trail.tidy(note)
            last_tidy = started
        time.sleep(max(10, EVERY - (time.time() - started)))


# ---- the page and the report door ---------------------------------------------------------------------------------

def ingress_request(headers):
    """Whether Home Assistant's ingress proxied this request for a signed-in user. The Supervisor adds
    X-Remote-User-Id itself and strips any copy a client sent, so only a request carrying it may change the checks;
    the add-on's own port (and every other address) can view them but not change them."""
    return bool(str(headers.get('X-Remote-User-Id') or '').strip())


def checks_payload(can_edit):
    """The Checks screen: the checks as the form edits them, the groups in use, the machines that have reported (to
    pick from) and whether this request may change anything."""
    body = services.checks_payload(can_edit)
    with lock:
        found = {}
        for machine, m in state['machines'].items():
            found[machine] = {'id': machine, 'name': labels.get(machine) or m.get('name') or machine}
        for row in body['checks']:
            machine = row.get('machine')
            if machine and machine not in found:
                found[machine] = {'id': machine, 'name': machine}
        body['machines'] = [found[k] for k in sorted(found, key=lambda k: (order.index(k) if k in order else len(order), k))]
    return body


def log_rows():
    """What the page's Logs section lists: every machine with saved syslog files in the page's machine order, then the
    hub's own log (trail.OWN_ID) - each with the time of its newest line, or None where it has none yet."""
    out = []
    for machine, at in trail.saved():
        if machine == trail.OWN_ID:
            out.append((1, 0, {'id': machine, 'name': trail.OWN_NAME, 'last': at.timestamp() if at else None}))
            continue
        with lock:
            name = labels.get(machine) or (state['machines'].get(machine) or {}).get('name') or machine
        out.append((0, order.index(machine) if machine in order else len(order),
                    {'id': machine, 'name': name, 'last': at.timestamp() if at else None}))
    out.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in out]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, body, kind='application/json', extra=None):
        data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        if 'chunked' not in self.headers.get('Transfer-Encoding', '').lower():
            return self.rfile.read(int(self.headers.get('Content-Length', 0)))
        data = b''  # OpenWrt's wget (uclient-fetch) sends the report in chunks
        while True:
            size = int(self.rfile.readline().split(b';')[0].strip() or b'0', 16)
            if not size:
                self.rfile.readline()
                return data
            data += self.rfile.read(size)
            self.rfile.readline()

    def do_POST(self):
        if not self.client_address[0].startswith(networks):
            return self.reply(403, {'error': 'no'})
        parts = urlparse(self.path).path.rstrip('/').split('/')
        last = parts[-1]
        if last == 'report':
            try:
                take_report(json.loads(self.body() or b'{}'))
                self.reply(200, {'ok': True})
            except Exception as e:
                note(f"bad report from {self.client_address[0]}: {e}")
                self.reply(400, {'error': str(e)})
            return
        if len(parts) >= 3 and parts[-3] == 'api' and parts[-2] == 'checks':
            # The Checks screen changes the list. Only Home Assistant's ingress may: that is the signed-in panel.
            if not ingress_request(self.headers):
                return self.reply(403, {'error': 'Checks can only be changed from the Home Assistant panel.'})
            try:
                body = json.loads(self.body() or b'{}')
            except ValueError as e:
                return self.reply(400, {'error': f'bad JSON: {e}'})
            if not isinstance(body, dict):
                return self.reply(400, {'error': 'send a JSON object'})
            return self.check_change(last, body)
        return self.reply(404, {'error': 'no'})

    def check_change(self, action, body):
        """One change from the Checks screen: save (add or edit), remove, move up/down, or a new push code."""
        try:
            cid = int(str(body.get('id')).strip()) if str(body.get('id') or '').strip() else None
        except (TypeError, ValueError):
            cid = None
        if action == 'save':
            row, errors = services.save_check(body, note)
            if errors:
                return self.reply(400, {'error': 'Please fix the fields marked.', 'fields': errors})
            return self.reply(200, {'ok': True, 'check': row, **{k: row[k] for k in ('push', 'url', 'curl') if k in row}})
        if action == 'remove':
            if cid is None or not services.remove_check(cid, note):
                return self.reply(404, {'error': 'No such check.'})
            return self.reply(200, {'ok': True})
        if action == 'move':
            try:
                by = int(body.get('by', 0))
            except (TypeError, ValueError):
                by = 0
            if cid is None or by not in (-1, 1) or not services.move_check(cid, by, note):
                return self.reply(404, {'error': 'No such check.'})
            return self.reply(200, {'ok': True})
        if action == 'code':
            row = services.new_code(cid, note) if cid is not None else None
            if not row:
                return self.reply(404, {'error': 'No such push check.'})
            return self.reply(200, {'ok': True, 'check': row, **{k: row[k] for k in ('push', 'url', 'curl') if k in row}})
        return self.reply(404, {'error': 'no'})

    def do_GET(self):
        if not self.client_address[0].startswith(networks):
            return self.reply(403, {'error': 'no'})
        url = urlparse(self.path)
        q = parse_qs(url.query)
        parts = url.path.rstrip('/').split('/')
        last = parts[-1]
        if len(parts) >= 2 and parts[-2] == 'push':  # Uptime Kuma's push URL shape, so a pusher only changes port and code
            ok = services.take_push(last, q)
            return self.reply(200 if ok else 404, {'ok': ok})
        if len(parts) >= 2 and parts[-2] == 'logs' and last == 'download':
            # The page's Logs section: one machine's log is a .log, several are a .zip holding one .log each.
            try:
                days = max(0, min(366, int(q.get('days', ['0'])[0])))  # 0 = everything kept
            except ValueError:
                days = 0
            known = {machine for machine, _ in trail.saved()}  # only names the hub itself listed: no paths from the page
            names = [name for name in ','.join(q.get('machines', [])).split(',') if name in known]
            got = trail.download(names, days)
            if not got:
                return self.reply(400, {'error': 'pick at least one log'})
            filename, data, kind = got
            return self.reply(200, data, kind, {'Content-Disposition': f'attachment; filename="{filename}"'})
        if last == 'services':
            uptime, strips = services.summary()
            return self.reply(200, {'monitors': services.snapshot(), 'uptime': uptime, 'strips': strips})
        if last == 'checks':
            return self.reply(200, checks_payload(ingress_request(self.headers)))
        if last == 'logs':
            return self.reply(200, {'logs': log_rows()})
        if last in ('check', 'outages', 'history'):
            try:
                days = max(1, min(366, int(q.get('days', ['1'])[0])))
            except ValueError:
                days = 1
            if last == 'check':
                return self.reply(200, services.points(q.get('id', [''])[0], days))
            if last == 'outages':
                return self.reply(200, services.outages(days))
        if last == 'status':
            with lock:
                body = {'now': time.time(), 'machines': state['machines'], 'problems': state.get('problems', {}),
                        'order': order, 'labels': labels, 'silent_hours': float(options.get('silent_hours', 6))}
            return self.reply(200, body)
        if last == 'log':
            return self.reply(200, {'lines': trail.tail(machine_id(q.get('machine', [''])[0]))})
        if last == 'history':
            return self.reply(200, history.points(machine_id(q.get('machine', [''])[0]), days))
        with open(PAGE, 'rb') as f:
            self.reply(200, f.read(), 'text/html; charset=utf-8')


def stop(*_):
    """Home Assistant stops the add-on with SIGTERM (updates, restarts): keep the counters so no report is lost."""
    try:
        save_state()
        note("stopped, state saved")
    finally:
        sys.exit(0)


def main():
    os.makedirs(history.HISTORY, exist_ok=True)
    try:
        with open(OPTIONS_FILE, encoding='utf-8') as f:
            options.update(json.load(f))
    except OSError:
        pass
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            state.update(json.load(f))
    except (OSError, ValueError):
        pass
    load_settings(note)
    note(f"started, watching {len(state['machines'])} machines")
    signal.signal(signal.SIGTERM, stop)
    services.machine = machine_report
    services.heal_script = heal_script
    threading.Thread(target=loop, daemon=True).start()
    threading.Thread(target=services.loop, args=(options, note), daemon=True).start()
    threading.Thread(target=trail.listen, args=(networks, senders, note), daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()


if __name__ == '__main__':
    main()
