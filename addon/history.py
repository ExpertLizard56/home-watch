"""Home watch's history: one plain CSV per machine per month in /share/home-watch/history, 12 months kept.

Every file has the same columns (empty where a machine doesn't have it); amounts are per report (5 minutes).
"""

import csv
import glob
import os
import statistics
import time
from datetime import datetime

DATA = os.environ.get('HOME_WATCH_DATA', '/share/home-watch')
HISTORY = f'{DATA}/history'
EVERY = 300  # seconds between reports
KEEP_MONTHS = 12

COLUMNS = ['time', 'cpu', 'mem', 'swap', 'load', 'temp', 'gpu_temp', 'gpu_use', 'gpu_power', 'drive_temp',
           'disk_used_pct', 'disk_free_gb', 'disk_read_mb', 'disk_write_mb', 'net_rx_mb', 'net_tx_mb',
           'wifi_clients', 'uptime_h']


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def write(machine, now, metrics):
    os.makedirs(HISTORY, exist_ok=True)
    path = f"{HISTORY}/{machine} {datetime.fromtimestamp(now):%Y-%m}.csv"
    new = not os.path.exists(path)
    row = [datetime.fromtimestamp(now).strftime('%Y-%m-%d %H:%M')]
    for col in COLUMNS[1:]:
        v = metrics.get(col)
        row.append('' if v is None else (int(v) if v == int(v) else round(v, 2)))
    with open(path, 'a', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        if new:
            w.writerow(COLUMNS)
        w.writerow(row)


def read(machine, days):
    """(time, row) for the last `days`, oldest first."""
    since = time.time() - days * 86400
    rows = []
    for path in sorted(glob.glob(f"{HISTORY}/{glob.escape(machine)} *.csv"))[-(days // 28 + 2):]:
        with open(path, encoding='utf-8') as f:
            for r in csv.DictReader(f):
                try:
                    t = datetime.strptime(r['time'], '%Y-%m-%d %H:%M').timestamp()
                except (KeyError, ValueError):
                    continue
                if t >= since:
                    rows.append((t, r))
    return rows


def points(machine, days, count=300):
    """Averages into about 300 points so a year draws as fast as a day."""
    rows = read(machine, days)
    if not rows:
        return {'t': [], 'series': {}}
    step = max(EVERY, days * 86400 / count)
    buckets = {}
    for t, r in rows:
        b = buckets.setdefault(int(t // step), {})
        for col in COLUMNS[1:]:
            v = num(r.get(col))
            if v is not None:
                b.setdefault(col, []).append(v)
    keys = sorted(buckets)
    series = {}
    for col in COLUMNS[1:]:
        vals = [round(statistics.fmean(buckets[k][col]), 2) if col in buckets[k] else None for k in keys]
        if any(v is not None for v in vals):
            series[col] = vals
    return {'t': [int(k * step) for k in keys], 'series': series}


def drop(machine):
    """Remove a machine's whole history: every month of its CSV (the Machines screen's Forget)."""
    for path in glob.glob(f"{HISTORY}/{glob.escape(machine)} *.csv"):
        os.remove(path)


def tidy(note):
    """Keeps the newest 12 months of each machine."""
    by_machine = {}
    for p in sorted(glob.glob(f"{HISTORY}/* *.csv")):
        by_machine.setdefault(os.path.basename(p).rsplit(' ', 1)[0], []).append(p)
    for paths in by_machine.values():
        for p in paths[:-KEEP_MONTHS]:
            os.remove(p)
            note(f"removed old history {os.path.basename(p)}")
