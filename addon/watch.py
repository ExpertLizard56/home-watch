"""Home watch: the Home Assistant add-on that keeps an eye on every machine on the home network.

Each device posts a report to port 8790 (/report) every 5 minutes; the machine the add-on runs on reports on itself.
Home watch keeps the history, runs the checks, leaves every notice in Home Assistant's notifications and sends it to the
phones (at most once a day per problem), and serves the page (the Home Assistant sidebar, or the add-on's port).

The parts: watch.py (this: reports, checks, alerts, the web door), history.py (the CSV history), pi.py (the hub's own
report), services.py (the service checks), trail.py (each machine's busiest programs and the logs machines send),
page.html (the page).

GET /health answers the Supervisor's watchdog (config.yaml's `watchdog`): 200 "ok" while the service-check loop is
going round, 503 with a short reason when it has stopped, so Home Assistant restarts an add-on that hangs, not only one
that exits. It carries no data and no secret.

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
from urllib.parse import parse_qs, urlparse

import history
import pi
import services
import trail

DATA = history.DATA
STATE_FILE = f'{DATA}/state.json'
LOG_FILE = f'{DATA}/log.txt'
SETTINGS_FILE = f'{DATA}/settings.json'
# Home Assistant's API, through the Supervisor: every notice is left in its notifications and then sent to the notify
# services (needs homeassistant_api: true in config.yaml).
SUPERVISOR = 'http://supervisor'
# The shared rule of the three Pi add-ons: an ISO time with a zone in this file silences the phones until then.
QUIET_FILE = '/share/phone-quiet-until.txt'

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
    'drive_temp': 70,        # C, the drive's own sensor, all the time for 15 minutes
    'drive_wear': 70,        # % of the drive's rated life used (the PC reporter sends it)
}
WRITES_GB = 20      # a Windows PC's day of writes over this and 3x its usual day = something is writing a lot
WRITES_EVERY = 3600  # seconds between working that out (it reads 8 days of history)
DRIVE_HOT_FOR = 900  # seconds over drive_temp before it's a problem (a big copy warms a drive for a minute or two)
# Address prefixes allowed to post reports and to open the page: the private ranges and Home Assistant's ingress.
DEFAULT_NETWORKS = ('10.', '127.', '192.168.', *(f'172.{n}.' for n in range(16, 32)), '172.30.32.')

lock = threading.Lock()
writes_seen = {}         # machine id -> (when worked out, history.writes_jump's answer)
state = {'machines': {}, 'sent': {}, 'raw': {}}
options = {}
order = []               # machine ids in the order settings.json lists them, which is the order the page shows
labels = {}              # machine id -> the name the page shows for it, from settings.json
limits = {}              # machine id -> {temp, mem, disk_used_pct, cpu_busy} of its own, from settings.json
networks = DEFAULT_NETWORKS  # address prefixes allowed to post reports and to open the page, from settings.json (push calls skip it)
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

    Every key is optional and a missing file is the normal first run; nothing in it is secret. Read at start and again
    after a change from the page's Machines screen, which writes the file: machines (the page's order, and labels),
    limits (one machine's own numbers), log_senders (which address is which machine, for the syslog trail),
    allowed_networks (who may post reports and open the page), heal_script (the Home Assistant script self-heal
    calls, {app} filled in per check) and heal_containers ({app: container}: outside Home Assistant, self-heal restarts
    that container through Docker instead)."""
    global order, labels, limits, networks, heal_script
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
    found_senders = {}
    if isinstance(asked, dict):
        found_senders = {str(ip).strip(): machine_id(m) for ip, m in asked.items() if machine_id(m)}
    senders.clear()  # in place: trail.listen holds this dict, so a change shows without a restart
    senders.update(found_senders)
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
    asked = got.get('heal_containers')  # outside Home Assistant: {app: container} restarted through Docker
    services.heal_containers = {str(k).strip(): str(v).strip() for k, v in asked.items()
                                if str(k).strip() and str(v).strip()} if isinstance(asked, dict) else {}
    if note and read:
        note(f"settings.json: {len(order)} machines in order, {len(labels)} labels, {len(limits)} with their own limits, "
             f"{len(senders)} log senders, {len(networks)} address prefixes allowed")


def limit_for(machine, key):
    """One machine's own number from settings.json, else the generic one."""
    got = limits.get(machine, {}).get(key)
    return LIMITS[key] if got is None else got


# ---- the Machines screen: the page's own editor for settings.json -------------------------------------------------

MACHINE_LIMITS = tuple(LIMITS)  # the numbers a machine may have its own of (all of LIMITS)
LIMIT_RANGES = {'temp': (1, 150), 'mem': (1, 100), 'disk_used_pct': (1, 100), 'cpu_busy': (1, 100),
                'drive_temp': (1, 150), 'drive_wear': (1, 100)}


def read_settings():
    """settings.json as it is, every key kept, or (None, why it cannot be used). A missing file is an empty one; a
    broken one is never overwritten, since the rest of the house's settings would go with it."""
    try:
        with open(SETTINGS_FILE, encoding='utf-8') as f:
            got = json.load(f)
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as e:
        return None, f'settings.json cannot be read ({e}); fix it by hand first.'
    if not isinstance(got, dict):
        return None, 'settings.json is not a JSON object; fix it by hand first.'
    return got, None


def write_settings(got):
    """Write settings.json whole (a temp file, then a rename), so a reader never sees half of it."""
    tmp = SETTINGS_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(json.dumps(got, indent=2) + '\n')
    os.replace(tmp, SETTINGS_FILE)


def listed_ids(got):
    """The machine ids settings.json lists, in the order it lists them."""
    listed = got.get('machines')
    if isinstance(listed, dict):
        return [machine_id(m) for m in listed if machine_id(m)]
    if isinstance(listed, list):
        return [machine_id(m) for m in listed if machine_id(m)]
    return []


def file_labels(got):
    """machine id -> the label settings.json gives it (a plain list gives none)."""
    listed = got.get('machines')
    if not isinstance(listed, dict):
        return {}
    return {machine_id(m): str(label or '').strip() for m, label in listed.items() if machine_id(m)}


def set_machines(got, ids, labels):
    """Put the machines in the page's order, as {"<machine>": "<label>"}; a machine with no label keeps an empty one,
    which means the name it reports."""
    got['machines'] = {m: labels.get(m, '') for m in ids}


def known_machines():
    """Every machine the hub can show: the ones settings.json lists, then the ones that have reported."""
    with lock:
        return list(dict.fromkeys(list(order) + list(state['machines'])))


def validate_machine(raw, known):
    """The Machines screen's fields -> (machine, errors). `errors` maps a field to the sentence shown next to it; an
    empty `errors` means it is ready to save. A blank limit means the generic one, so it is left out."""
    errors = {}
    mid = machine_id(raw.get('id'))
    if not mid:
        errors['id'] = 'Pick a machine.'
    elif mid not in known:
        errors['id'] = 'That machine has gone; reload the page.'
    label = str(raw.get('name') or '').strip()
    if len(label) > 60:
        errors['name'] = 'Keep the name to 60 characters or fewer.'
    own = {}
    given = raw.get('limits')
    given = given if isinstance(given, dict) else {}
    for key in MACHINE_LIMITS:
        value = given.get(key)
        if value is None or str(value).strip() == '':
            continue  # blank: the generic number, shown as the hint
        low, high = LIMIT_RANGES[key]
        try:
            number = float(str(value).strip())
        except (TypeError, ValueError):
            number = None
        if number is None or not low <= number <= high:
            errors[key] = f'Use a number from {low} to {high}, or leave it blank for the default.'
            continue
        own[key] = int(number) if number == int(number) else round(number, 2)
    return {'id': mid, 'label': label, 'limits': own}, errors


def machine_row(mid):
    """One machine as the Machines screen edits it: the id, the name it shows, its own limits and whether it reports."""
    with lock:
        rep = state['machines'].get(mid) or {}
        return {'id': mid, 'name': labels.get(mid) or rep.get('name') or mid, 'label': labels.get(mid, ''),
                'limits': dict(limits.get(mid, {})), 'reported': bool(rep), 'last': rep.get('last')}


def machines_payload(can_edit=False):
    """The Machines screen: every machine that has reported and every one settings.json lists, in the page's order,
    with its shown name, its own limits, the generic limits (the form's hints) and whether this request may change
    anything."""
    with lock:
        ids = list(dict.fromkeys(order))
        ids += sorted(m for m in state['machines'] if m not in ids)
    return {'machines': [machine_row(mid) for mid in ids], 'defaults': dict(LIMITS), 'can_edit': bool(can_edit)}


def save_machine(raw, note=None):
    """The Machines screen's save: a machine's shown name and its own limits. settings.json is written whole, keeping
    every other key in it, and read back at once, so the page and the checks use the new numbers without a restart.
    Returns (the machine as the page shows it, field -> error); the errors are empty when it was saved."""
    note = note or (lambda text: None)
    got, bad = read_settings()
    if bad:
        return None, {'id': bad}
    found, errors = validate_machine(raw, known_machines())
    if errors:
        return None, errors
    mid = found['id']
    ids = list(dict.fromkeys(listed_ids(got)))
    if mid not in ids:
        ids.append(mid)  # a machine that has reported but is not listed yet goes last, and is listed from now on
    machine_labels = file_labels(got)
    machine_labels[mid] = found['label']
    set_machines(got, ids, machine_labels)
    own = got.get('limits')
    own = dict(own) if isinstance(own, dict) else {}
    if found['limits']:
        own[mid] = found['limits']
    else:
        own.pop(mid, None)
    if own:
        got['limits'] = own
    else:
        got.pop('limits', None)
    write_settings(got)
    load_settings(note)
    note(f"machine {mid}: saved")
    return machine_row(mid), {}


def move_machine(mid, by, note=None):
    """Move a machine up (-1) or down (+1) in the page's order, which is settings.json's list. A machine that has
    reported but is not listed yet joins the list where it was moved to."""
    note = note or (lambda text: None)
    got, bad = read_settings()
    if bad:
        return False
    ids = known_machines()
    if mid not in ids:
        return False
    i = ids.index(mid)
    j = max(0, min(len(ids) - 1, i + int(by)))
    if i == j:
        return True
    ids.insert(j, ids.pop(i))
    set_machines(got, ids, file_labels(got))
    write_settings(got)
    load_settings(note)
    note(f"machine {mid}: moved {'up' if j < i else 'down'}")
    return True


def forget_machine(mid, note=None):
    """Forget a machine: take it out of settings.json (its place, its label, its limits, and any log sender that
    points at it) and drop what the hub kept for it - history, programs, logs and its live state. A machine that goes
    on reporting shows up again, with no history."""
    note = note or (lambda text: None)
    mid = machine_id(mid)
    if not mid or mid not in known_machines():
        return False
    got, bad = read_settings()
    if bad:
        return False
    changed = False
    listed = list(dict.fromkeys(listed_ids(got)))
    if mid in listed:
        machine_labels = file_labels(got)
        machine_labels.pop(mid, None)
        set_machines(got, [m for m in listed if m != mid], machine_labels)
        changed = True
    own = got.get('limits')
    if isinstance(own, dict) and mid in own:
        own.pop(mid)
        if not own:
            got.pop('limits', None)
        changed = True
    asked = got.get('log_senders')
    if isinstance(asked, dict):
        left = {ip: m for ip, m in asked.items() if machine_id(m) != mid}
        if len(left) != len(asked):
            got['log_senders'] = left
            changed = True
    if changed:
        write_settings(got)
    for drop, what in ((history.drop, 'history'), (trail.drop, 'programs and logs')):
        try:
            drop(mid)
        except OSError as e:  # a file open elsewhere: say so, and still forget the machine
            note(f"machine {mid}: some {what} could not be removed ({e})")
    with lock:
        state['machines'].pop(mid, None)
        state['raw'].pop(mid, None)
        for key in [k for k in state['sent'] if k.startswith(f'{mid}-')]:
            state['sent'].pop(key)
        for key in [k for k in (state.get('problems') or {}) if k.startswith(f'{mid}-')]:
            state['problems'].pop(key)
    save_state()
    load_settings(note)
    note(f"machine {mid}: forgotten, with its stored history, programs and logs")
    return True


# ---- reports ----------------------------------------------------------------------------------------------------

def clean_notes(raw):
    """{label: text} from a report -> the same, checked: at most 12, labels cut to 40 characters and texts to 300, and
    anything that is not a string left out. Notes are information for the page only: they never become a problem."""
    out = {}
    if not isinstance(raw, dict):
        return out
    for label, text in raw.items():
        if not isinstance(label, str) or not isinstance(text, str):
            continue
        out[label[:40]] = text[:300]
        if len(out) >= 12:
            break
    return out


def take_report(rep):
    """A report: {machine, name, kind, metrics:{...}, counters:{...}, cpu_ticks:[total, idle], problems:{key: message},
    notes:{label: text}}."""
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
        # Each report replaces the notes; one without them clears them, the way problems are replaced.
        notes = clean_notes(rep.get('notes'))
        if notes:
            m['notes'] = notes
        else:
            m.pop('notes', None)
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
        if (metrics.get('drive_temp') or 0) >= limit_for(machine, 'drive_temp'):
            m.setdefault('drive_hot_since', now)
        else:
            m.pop('drive_hot_since', None)
    history.write(machine, now, metrics)
    if procs:
        trail.write_procs(machine, now, procs)


def machine_report(machine):
    with lock:
        m = state['machines'].get(machine)
        return dict(m) if m else None


# ---- checks and alerts --------------------------------------------------------------------------------------------

def writes_jumps(now):
    """A Windows machine's last day of writes against its usual day, worked out once an hour (history.writes_jump)."""
    with lock:
        pcs = [mid for mid, m in state['machines'].items() if m.get('kind') == 'windows']
    out = {}
    for mid in pcs:
        when, jump = writes_seen.get(mid, (0, None))
        if now - when >= WRITES_EVERY:
            try:
                jump = history.writes_jump(mid, WRITES_GB)
            except Exception as e:
                note(f"writes check for {mid} failed: {e!r}")
                jump = None
            writes_seen[mid] = (now, jump)
        if jump:
            out[mid] = jump
    return out


def current_problems():
    now = time.time()
    jumps = writes_jumps(now)
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
            since = m.get('drive_hot_since')
            if since and now - since >= DRIVE_HOT_FOR:
                out[f'{machine}-drive-hot'] = (name, (f"Drive is running hot: {mt.get('drive_temp', 0):.0f} C for "
                                                      f"{(now - since) / 60:.0f} minutes"))
            if machine in jumps:
                gb, times = jumps[machine]
                out[f'{machine}-writes'] = (name, (f"Wrote {gb:.0f} GB to its drives in the last day, about {times:.0f}x the "
                                                   "usual. Something is writing a lot (a new update or driver?)"))
            wear = mt.get('drive_wear')
            if wear is not None and wear >= limit_for(machine, 'drive_wear'):
                out[f'{machine}-drive-wear'] = (name, f"Drive says {wear:.0f}% of its life is used. Plan a replacement.")
            since = m.get('cpu_high_since')
            if since and now - since >= 3600:
                top = ', '.join(f"{p} {v:.0f}%" for p, v in (m.get('procs') or [])[:3])
                out[f'{machine}-cpu'] = (name, f"CPU busy ({mt.get('cpu', 0):.0f}%) for {(now - since) / 3600:.0f} hours"
                                         + (f". Busiest: {top}" if top else ''))
    return out


# ---- notices -------------------------------------------------------------------------------------------------------

def supervisor_call(path, payload=None):
    """Call Home Assistant's API through the Supervisor, with the token it gives this add-on (homeassistant_api).

    A payload makes it a JSON POST; no payload is a GET."""
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f'{SUPERVISOR}{path}', data=body, method='POST' if body else 'GET', headers={
        'Authorization': f"Bearer {os.environ.get('SUPERVISOR_TOKEN', '')}", 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode('utf-8', 'replace') or 'null')


def quiet_until():
    """The time in QUIET_FILE while it is still in the future, else None (the phones are not quiet).

    The shared rule of the three Pi add-ons: an ISO time with a zone (2026-11-16T12:00:00-05:00) silences the phones
    until then. A missing file, text that is not a time, or a time with no zone all mean not quiet, because without a
    zone it cannot be compared with now safely."""
    try:
        with open(QUIET_FILE, encoding='utf-8') as f:
            until = datetime.fromisoformat(f.read().strip())
    except (OSError, ValueError):
        return None
    if until.tzinfo is None:
        return None
    return until if until > datetime.now().astimezone() else None


def phone_names():
    """The notify services a notice goes to: every one the companion app makes (they all start mobile_app_)."""
    for domain in supervisor_call('/core/api/services') or []:
        if isinstance(domain, dict) and domain.get('domain') == 'notify':
            return [name for name in (domain.get('services') or {}) if name.startswith('mobile_app_')]
    return []


def send(title, message):
    """One notice: into Home Assistant's notifications, then to the phones unless the house is quiet.

    The shared phone-notice rule of the three Pi add-ons (Media Hub, Home watch, Home upkeep): every notice is also
    left in Home Assistant's notifications, the durable record; then it goes to every phone (each mobile_app_ notify
    service); while QUIET_FILE holds a future time step 2 is skipped
    and that is logged; and a failure is logged, never raised. The persistent notification is what says the notice got
    through: a phone that cannot be reached does not make the add-on send the same alert again.

    Outside Home Assistant (env NTFY_URL, an ntfy topic) the notice goes there instead, as one POST; True when ntfy took
    it (or the phones are quiet, so it is not tried again)."""
    topic = os.environ.get('NTFY_URL', '').strip()
    if topic:
        until = quiet_until()
        if until:
            note(f"phones quiet until {until.isoformat()}: {title} - {message}")
            return True
        try:
            req = urllib.request.Request(topic, data=message.encode('utf-8'), method='POST',
                                         headers={'Title': title.encode('utf-8').decode('latin-1')})
            with urllib.request.urlopen(req, timeout=20) as r:
                r.read()
            return True
        except Exception as e:
            note(f"could not send to ntfy: {title} - {message} ({e})")
            return False
    posted = False
    try:
        supervisor_call('/core/api/services/persistent_notification/create', {'title': title, 'message': message})
        posted = True
    except Exception as e:
        note(f"could not leave a notification: {title} - {message} ({e})")
    until = quiet_until()
    if until:
        note(f"phones quiet until {until.isoformat()}: {title} - {message}")
        return posted
    try:
        names = phone_names()
    except Exception as e:
        note(f"could not list the phones: {title} - {message} ({e})")
        return posted
    if not names:
        note(f"no phone to notify: {title} - {message}")
        return posted
    for name in names:
        try:
            supervisor_call(f'/core/api/services/notify/{name}', {'title': title, 'message': message, 'data': {}})
        except Exception as e:
            note(f"could not notify {name}: {title} - {message} ({e})")
    return posted


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

TRUSTED_PROXIES = ('172.30.32.2', '127.0.0.1', '::1', '::ffff:127.0.0.1')


def ingress_request(headers, address):
    """Whether a login proxy sent this request for a signed-in user: Home Assistant's ingress (172.30.32.2) or a proxy
    on this same machine (127.0.0.1), which set X-Remote-User-Id and stripped any copy a client sent. Only such a
    request may change the checks or the machines. The header counts only from those addresses: with host networking
    any device could send it straight to the add-on's port, and those (and every other address) can view but not
    change."""
    return address in TRUSTED_PROXIES and bool(str(headers.get('X-Remote-User-Id') or '').strip())


def checks_payload(can_edit):
    """The Checks screen: the checks as the form edits them, the groups in use, the machines that have reported (to
    pick from) and whether this request may change anything."""
    body = services.checks_payload(can_edit)
    with lock:
        found = {}
        for machine, m in state['machines'].items():
            found[machine] = {'id': machine, 'name': labels.get(machine) or m.get('name') or machine,
                             'notes': sorted((m.get('notes') or {}).keys())}
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
        if len(parts) >= 3 and parts[-3] == 'api' and parts[-2] in ('checks', 'machines'):
            # The Checks and Machines screens change the add-on's own files. Only Home Assistant's ingress may: that is
            # the signed-in panel.
            if not ingress_request(self.headers, self.client_address[0]):
                return self.reply(403, {'error': f'{parts[-2].title()} can only be changed from the Home Assistant panel.'})
            body = self.json_body()
            if body is None:
                return
            if parts[-2] == 'checks':
                return self.check_change(last, body)
            return self.machine_change(last, body)
        return self.reply(404, {'error': 'no'})

    def json_body(self):
        """The POSTed JSON object, or None after answering why it cannot be used."""
        try:
            got = json.loads(self.body() or b'{}')
        except ValueError as e:
            self.reply(400, {'error': f'bad JSON: {e}'})
            return None
        if not isinstance(got, dict):
            self.reply(400, {'error': 'send a JSON object'})
            return None
        return got

    def machine_change(self, action, body):
        """One change from the Machines screen: save (rename and limits), move up/down, or forget."""
        mid = machine_id(body.get('id'))
        if action == 'save':
            row, errors = save_machine(body, note)
            if errors:
                return self.reply(400, {'error': 'Please fix the fields marked.', 'fields': errors})
            return self.reply(200, {'ok': True, 'machine': row})
        if action == 'move':
            try:
                by = int(body.get('by', 0))
            except (TypeError, ValueError):
                by = 0
            if not mid or by not in (-1, 1) or not move_machine(mid, by, note):
                return self.reply(404, {'error': 'No such machine.'})
            return self.reply(200, {'ok': True})
        if action == 'forget':
            if not mid or not forget_machine(mid, note):
                return self.reply(404, {'error': 'No such machine.'})
            return self.reply(200, {'ok': True})
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
        if urlparse(self.path).path.rstrip('/') == '/health':
            # The Supervisor's watchdog (config.yaml's `watchdog`) calls this every minute and restarts the add-on when
            # it stops answering or is not well; Home Assistant counts an answer under 300 as healthy and 503 as not.
            # It answers before the address gate: the answer holds nothing about the house, and a house that narrows
            # allowed_networks must not be able to turn a healthy add-on into a restart loop. (The Supervisor's own
            # address, 172.30.32.x, is in the default list in any case.) The answer is one word and a reason.
            ok, why = services.health()
            return self.reply(200 if ok else 503, 'ok' if ok else why, 'text/plain; charset=utf-8')
        url = urlparse(self.path)
        q = parse_qs(url.query)
        parts = url.path.rstrip('/').split('/')
        last = parts[-1]
        if len(parts) >= 2 and parts[-2] == 'push':  # Uptime Kuma's push URL shape, so a pusher only changes port and code
            # Also before the address gate: the code is the secret (128 bits, compared in constant time), as in Uptime
            # Kuma and Healthchecks, so a job can report from any address it has, such as another add-on on Home
            # Assistant's internal network (172.30.33.x). The answer says only whether the code was right.
            ok = services.take_push(last, q)
            return self.reply(200 if ok else 404, {'ok': ok})
        if not self.client_address[0].startswith(networks):
            return self.reply(403, {'error': 'no'})
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
            return self.reply(200, checks_payload(ingress_request(self.headers, self.client_address[0])))
        if last == 'machines':
            return self.reply(200, machines_payload(ingress_request(self.headers, self.client_address[0])))
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
