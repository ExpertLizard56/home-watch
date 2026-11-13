"""What a machine leaves behind for working out what went wrong after a restart wipes it (a machine can run with a core
stuck busy for days and then hang, and the restart that fixes it takes the log that would have said why with it).

- Programs: each report's list of programs and their share of the CPU. The newest full list shows on the device page;
  programs at 1% or more go in /share/home-watch/programs/<machine> <year-month>.csv (time, program, %).
- Logs: machines can send their system log here (UDP 5514, OpenWrt's log_ip). Lines go in
  /share/home-watch/logs/<machine> <year-month>.log; cron's line for every job it starts and access points' Wi-Fi
  steering chatter are left out.
Programs kept 12 months, logs 3.
"""

import csv
import glob
import os
import socket
from datetime import datetime

from history import DATA

PROGRAMS = f'{DATA}/programs'
LOGS = f'{DATA}/logs'
LOG_PORT = 5514
KEEP = {PROGRAMS: 12, LOGS: 3}
NOISE = ('cmd /usr/bin/', 'cmd /usr/local/bin/',  # crond's "USER root pid N cmd ..." for every job, every minute
         'BEACON-REQ-TX-STATUS', 'BEACON-RESP-RX', 'Unexpected Link measurement report')  # access points' chatter, ~1 MB a day


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


def tidy(note):
    for folder, months in KEEP.items():
        by_machine = {}
        for path in sorted(glob.glob(f"{folder}/* *.*")):
            by_machine.setdefault(os.path.basename(path).rsplit(' ', 1)[0], []).append(path)
        for paths in by_machine.values():
            for path in paths[:-months]:
                os.remove(path)
                note(f"removed old {os.path.basename(path)}")
