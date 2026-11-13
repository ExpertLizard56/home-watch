"""What a machine leaves behind for working out what went wrong after a restart wipes it (a machine can run with a core
stuck busy for days and then hang, and the restart that fixes it takes the log that would have said why with it).

- Programs: each report's list of programs and their share of the CPU. The newest full list shows on the device page;
  programs at 1% or more go in /share/home-watch/programs/<machine> <year-month>.csv (time, program, %).
- Logs: machines can send their system log here (UDP 5514, OpenWrt's log_ip). Lines go in
  /share/home-watch/logs/<machine> <year-month>.log; cron's line for every job it starts and access points' Wi-Fi
  steering chatter are left out. The page's Logs section downloads them again - one .log for one machine, a .zip with
  one .log per machine for several - cut only by each line's own time, never to a line count.
Programs kept 12 months, logs 3.
"""

import csv
import glob
import io
import os
import re
import socket
import time
import zipfile
from datetime import datetime, timedelta

from history import DATA

PROGRAMS = f'{DATA}/programs'
LOGS = f'{DATA}/logs'
LOG_PORT = 5514
KEEP = {PROGRAMS: 12, LOGS: 3}
NOISE = ('cmd /usr/bin/', 'cmd /usr/local/bin/',  # crond's "USER root pid N cmd ..." for every job, every minute
         'BEACON-REQ-TX-STATUS', 'BEACON-RESP-RX', 'Unexpected Link measurement report')  # access points' chatter, ~1 MB a day
# The hub's own log (watch.py's LOG_FILE) is offered next to the machines'. Its id has a dot, which machine_id() never
# leaves in a machine name, so it cannot clash with a real one.
OWN_ID = 'log.txt'
OWN_NAME = 'Home Watch'
OWN_LOG = f'{DATA}/log.txt'


def clean_procs(raw):
    """[[name, %], ...] from a report -> the same, checked, busiest first (at most 400)."""
    out = []
    for item in raw or []:
        try:
            name, pct = str(item[0])[:60], round(float(item[1]), 1)
        except (TypeError, ValueError, IndexError):
            continue
        out.append([name, pct])
    return sorted(out, key=lambda p: -p[1])[:400]


def write_procs(machine, now, procs):
    busy = [p for p in procs if p[1] >= 1]
    if not busy:
        return
    os.makedirs(PROGRAMS, exist_ok=True)
    path = f"{PROGRAMS}/{machine} {datetime.fromtimestamp(now):%Y-%m}.csv"
    new = not os.path.exists(path)
    stamp = f'{datetime.fromtimestamp(now):%Y-%m-%d %H:%M}'
    with open(path, 'a', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        if new:
            w.writerow(['time', 'program', 'pct'])
        for name, pct in busy:
            w.writerow([stamp, name, pct])


def listen(networks, senders, note):
    """Takes syslog lines over UDP from the allowed addresses and files them by machine (`senders`: ip -> machine)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('0.0.0.0', LOG_PORT))
    os.makedirs(LOGS, exist_ok=True)
    note(f"taking logs on UDP {LOG_PORT}")
    while True:
        data, (ip, _) = sock.recvfrom(8192)
        if not ip.startswith(networks):
            continue
        text = data.decode('utf-8', 'replace').strip()
        if text.startswith('<') and '>' in text[:5]:
            text = text[text.index('>') + 1:]  # the <priority> number
        if not text or any(n in text for n in NOISE):
            continue
        now = datetime.now()
        try:
            with open(f"{LOGS}/{senders.get(ip, ip)} {now:%Y-%m}.log", 'a', encoding='utf-8') as f:
                f.write(f"{now:%Y-%m-%d %H:%M:%S} {text}\n")
        except OSError as e:
            note(f"log line not kept: {e}")


def tail(machine, lines=200):
    """The last lines of a machine's log, newest last."""
    out = []
    for path in sorted(glob.glob(f"{LOGS}/{glob.escape(machine)} *.log"))[-2:]:
        with open(path, encoding='utf-8', errors='replace') as f:
            out.extend(f.readlines())
    return [line.rstrip('\n') for line in out[-lines:]]


# ---- the page's Logs section: what is saved, and downloading a range of it ---------------------------------------

def _paths(machine):
    """A machine's month files, oldest first."""
    return sorted(glob.glob(f"{LOGS}/{glob.escape(machine)} *.log"))


def stamp(line):
    """The time a line of a log was written, from the date at its start (None when it has none).

    Machines' lines start "YYYY-MM-DD HH:MM:SS" (trail.listen) and the hub's own "YYYY-MM-DD HH:MM" (watch.note)."""
    for fmt, size in (('%Y-%m-%d %H:%M:%S', 19), ('%Y-%m-%d %H:%M', 16)):
        try:
            return datetime.strptime(str(line)[:size], fmt)
        except ValueError:
            continue
    return None


def since(days, now=None):
    """Local midnight `days` calendar days back: 1 is today, 3 is today and the two days before it; 0 is everything."""
    days = int(days or 0)
    if days <= 0:
        return None
    midnight = datetime.fromtimestamp(time.time() if now is None else now).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=days - 1)


def _newest(path, size=32768):
    """The time of the last stamped line in a file, read from its end so a big log costs little."""
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            start = max(0, f.tell() - size)
            f.seek(start)
            got = f.read().decode('utf-8', 'replace')
    except OSError:
        return None
    lines = got.splitlines()
    if start and lines:
        lines = lines[1:]  # the first is half a line
    for line in reversed(lines):
        when = stamp(line)
        if when:
            return when
    return None


def saved():
    """[(machine or OWN_ID, the newest entry's time or None)], machines alphabetically, the hub's own log last."""
    by_machine = {}
    for path in sorted(glob.glob(f'{LOGS}/* *.log')):  # sorted: the last path for a machine is its newest month
        by_machine[os.path.basename(path).rsplit(' ', 1)[0]] = path
    out = [(machine, _newest(path)) for machine, path in sorted(by_machine.items())]
    out.append((OWN_ID, _newest(OWN_LOG)))
    return out


def last_entry(machine):
    """When the newest entry in a machine's log (or the hub's own) was written, or None."""
    if machine == OWN_ID:
        return _newest(OWN_LOG)
    paths = _paths(machine)
    return _newest(paths[-1]) if paths else None


def _month(path):
    """The month a logs/ file holds, from its "<machine> <year-month>.log" name; None if it cannot be told."""
    try:
        return datetime.strptime(os.path.basename(path).rsplit(' ', 1)[-1][:7], '%Y-%m')
    except ValueError:
        return None


def _read(path, after):
    """A file's lines from `after` on, by each line's own time; a line without a date keeps the one before it."""
    out, seen = [], None
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            for raw in f:
                line = raw.rstrip('\r\n')
                when = stamp(line)
                if when:
                    seen = when
                if after and (seen is None or seen < after):
                    continue
                out.append(line)
    except OSError:
        return out
    return out


def lines(machine, days=0):
    """A machine's log lines for the range, oldest first. `days` counts calendar days back from local midnight
    (1 = today, 0 = all kept). Whole lines go, never a cut to some count; a file whose month is before the range is
    skipped unread."""
    after = since(days)
    if machine == OWN_ID:
        return _read(OWN_LOG, after)
    paths = _paths(machine)
    if after:
        first = after.replace(day=1)
        paths = [p for p in paths if (_month(p) or first) >= first]
    out = []
    for path in paths:
        out.extend(_read(path, after))
    return out


def _file_stem(name):
    """A name fit for a file: the hub's own log is home-watch, a machine's id is already safe."""
    return 'home-watch' if name == OWN_ID else (re.sub(r'[^A-Za-z0-9._-]', '-', name) or 'log')


def download(names, days=0, now=None):
    """(filename, bytes, content type) of the chosen logs for the range; None when none of the names has a log.

    One log is a plain .log; several are a .zip holding one .log each. Built in memory: a range is a few months at most."""
    picked, seen = [], set()
    for name in names:
        name = str(name or '').strip()
        if not name or name in seen or not (name == OWN_ID or _paths(name)):
            continue
        seen.add(name)
        picked.append((name, lines(name, days)))
    if not picked:
        return None
    day = datetime.fromtimestamp(time.time() if now is None else now).strftime('%Y-%m-%d')
    if len(picked) == 1:
        name, body = picked[0]
        text = '\n'.join(body) + ('\n' if body else '')
        return f'{_file_stem(name)}-{day}.log', text.encode(), 'text/plain; charset=utf-8'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, body in picked:
            archive.writestr(f'{_file_stem(name)}.log', '\n'.join(body) + ('\n' if body else ''))
    return f'home-watch-logs-{day}.zip', buf.getvalue(), 'application/zip'


def drop(machine):
    """Remove everything kept for a machine - its programs and its logs, every month (the Machines screen's Forget)."""
    for folder in (PROGRAMS, LOGS):
        for path in glob.glob(f"{folder}/{glob.escape(machine)} *.*"):
            os.remove(path)


def tidy(note):
    for folder, months in KEEP.items():
        by_machine = {}
        for path in sorted(glob.glob(f"{folder}/* *.*")):
            by_machine.setdefault(os.path.basename(path).rsplit(' ', 1)[0], []).append(path)
        for paths in by_machine.values():
            for path in paths[:-months]:
                os.remove(path)
                note(f"removed old {os.path.basename(path)}")
