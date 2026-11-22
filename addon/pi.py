"""The hub's own report, made inside the add-on (the same shape a device posts to /report).

The hub's card is usually an SD card: no SMART and no wear report, so what's watched is the trouble the drive
checks look for elsewhere: kernel card errors since the machine started, the card refusing a write, free space, and
how much it writes. eMMC wear is read the way Beszel does (agent/emmc_linux.go, MIT) in case it boots from eMMC.
"""

import errno
import glob
import json
import os
import re
import time
import urllib.request

import history
import services

LIMITS = {
    'free_gb': 5,     # under 5 GB free on the hub's card is a problem
    'writes_gb': 2,   # a day, and 3x the usual, before it's called a lot
}
CARD_ERRORS = re.compile(r'mmc\d.*(error|timeout|fail)|I/O error|EXT4-fs error|remount.*read-only', re.IGNORECASE)
KMSG = '/dev/kmsg'
KERNEL_LOGS = ('/host/logs/boots/0/identifiers/kernel', '/host/logs/identifiers/kernel')

seen = {'card_errors': None}
CRASH_WINDOW = 1800   # a container Docker restarted on its own this many times in 30 min is crash-looping
CRASH_TIMES = 3
restarts = {}         # container name -> [(time, Docker's RestartCount)], the last CRASH_WINDOW of them


def read(path, default=''):
    try:
        with open(path, encoding='utf-8') as f:
            return f.read()
    except OSError:
        return default


def supervisor(path, accept='application/json'):
    req = urllib.request.Request(f'http://supervisor{path}', headers={
        'Authorization': f"Bearer {os.environ.get('SUPERVISOR_TOKEN', '')}", 'Accept': accept})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode('utf-8', 'replace')


def kmsg(path=KMSG):
    """The kernel log since boot, read straight from /dev/kmsg (outside Home Assistant: the container gets it mounted
    read-only). One record per read; reading stops when nothing is left. Each record is "pri,seq,time,flags;text"."""
    lines = []
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
    try:
        while True:
            try:
                record = os.read(fd, 8192)
            except BlockingIOError:
                break
            except OSError as e:
                if e.errno == errno.EPIPE:  # a record was overwritten while reading: skip it
                    continue
                raise
            if not record:
                break
            lines.append(record.decode('utf-8', 'replace').split(';', 1)[-1].split('\n', 1)[0])
    finally:
        os.close(fd)
    return '\n'.join(lines)


def kernel_texts():
    """Where the kernel log can come from: the Supervisor (inside Home Assistant, role manager) or /dev/kmsg."""
    if os.environ.get('SUPERVISOR_TOKEN'):
        return [lambda path=path: supervisor(path, 'text/plain') for path in KERNEL_LOGS]
    return [kmsg]


def card_errors(note):
    """Kernel messages since the machine started that say the card is in trouble. Inside Home Assistant this needs
    the add-on's Supervisor role manager, the only way the Supervisor lets an add-on read the kernel log; outside it,
    /dev/kmsg mounted into the container."""
    last = None
    for get in kernel_texts():
        try:
            text = get()
        except Exception as e:
            last = e
            continue
        count = sum(1 for line in text.splitlines() if CARD_ERRORS.search(line))
        if count != seen['card_errors']:
            note(f"kernel log read: {count} SD card errors since it started")
            seen['card_errors'] = count
        return count
    note(f"couldn't read the kernel log: {last}")
    return None


def emmc_wear(dev='mmcblk0'):
    """Beszel's reading: pre_eol 1 normal, 2 warning, 3 urgent; life_time 0x01-0x0B = 0-100%+ used."""
    base = f'/sys/block/{dev}/device'
    pre, life = read(f'{base}/pre_eol_info').strip(), read(f'{base}/life_time').split()
    if not pre and not life:
        return None, None  # an SD card: no wear report
    used = max((int(x, 16) for x in life), default=0)
    return int(pre, 16) if pre else None, (min(used, 11) - 1) * 10 if used else None


def writes_a_lot():
    """SD cards wear out from writing. Last 24 h against the usual day (history.writes_jump)."""
    jump = history.writes_jump('pi', LIMITS['writes_gb'])
    if jump:
        return f"The SD card wrote {jump[0]:.1f} GB in the last day, about {jump[1]:.0f}x the usual. Something is writing a lot"
    return None


def docker_get(path):
    conn = services.DockerSocket(timeout=10)
    try:
        conn.request('GET', path)
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            raise RuntimeError(f'docker {resp.status}')
        return json.loads(body)
    finally:
        conn.close()


def crash_loops(now=None, get=docker_get):
    """Containers that keep crashing: Docker's own restart policy brought them back CRASH_TIMES or more in the last
    CRASH_WINDOW (RestartCount only counts those, not a restart asked for, and starts over when a container is made
    again). {name: count}. Nothing without the Docker socket (as a Home Assistant add-on)."""
    if get is docker_get and not os.path.exists(services.DOCKER_SOCKET):
        return {}
    now = time.time() if now is None else now
    out, names = {}, set()
    for c in get('/containers/json?all=1'):
        name = (c.get('Names') or ['?'])[0].lstrip('/')
        names.add(name)
        count = int(get(f"/containers/{c['Id']}/json").get('RestartCount') or 0)
        seen_here = [(t, n) for t, n in restarts.get(name, []) if now - t <= CRASH_WINDOW and n <= count]
        seen_here.append((now, count))
        restarts[name] = seen_here
        rose = count - seen_here[0][1]
        if rose >= CRASH_TIMES:
            out[name] = rose
    for gone in set(restarts) - names:
        del restarts[gone]
    return out


def problems(note):
    out = {}
    try:
        for name, count in crash_loops().items():
            out[f'crash-{name}'] = f"{name} keeps crashing: Docker restarted it {count} times in the last 30 minutes"
    except Exception as e:  # the rest of the report still goes
        note(f'container restart check failed: {e}')
    st = os.statvfs(history.DATA)
    free_gb = st.f_bavail * st.f_frsize / 1e9
    if free_gb < LIMITS['free_gb']:
        out['space'] = f"Only {free_gb:.1f} GB free on the SD card"
    errors = card_errors(note)
    if errors:
        out['card-errors'] = f"{errors} SD card errors since its last restart. Check backups and consider replacing the card"
    try:
        probe = f'{history.DATA}/.writecheck'
        with open(probe, 'w') as f:
            f.write('ok')
        os.remove(probe)
    except OSError:
        out['card-write'] = 'The SD card is refusing writes. It may be failing'
    pre_eol, used = emmc_wear()
    if pre_eol and pre_eol >= 2:
        out['card-wear'] = f"The card says it's {'nearly worn out' if pre_eol == 2 else 'worn out'}"
    elif used is not None and used >= 90:
        out['card-wear'] = f"The card says {used}% of its life is used"
    lot = writes_a_lot()
    if lot:
        out['card-writes'] = lot
    return out


def report(note):
    ticks = [int(x) for x in read('/proc/stat').splitlines()[0].split()[1:]]
    mem = {ln.split(':')[0]: int(ln.split()[1]) for ln in read('/proc/meminfo').splitlines() if ':' in ln}
    temps = [int(read(p, '0')) / 1000 for p in glob.glob('/sys/class/thermal/thermal_zone*/temp')]
    card = read('/sys/block/mmcblk0/stat').split()
    net = [0, 0]
    for ln in read('/proc/net/dev').splitlines()[2:]:
        name, rest = ln.split(':', 1)
        if re.match(r'\s*(eth|end|wlan)\d', name):
            f = rest.split()
            net[0] += int(f[0])
            net[1] += int(f[8])
    st = os.statvfs(history.DATA)
    counters = {'net_rx_bytes': net[0], 'net_tx_bytes': net[1]}
    if card:
        counters.update(disk_read_bytes=int(card[2]) * 512, disk_write_bytes=int(card[6]) * 512)
    return {
        'machine': 'pi', 'name': 'Pi', 'kind': 'pi',
        'cpu_ticks': [sum(ticks), ticks[3] + ticks[4]],
        'metrics': {
            'mem': round(100 * (1 - mem['MemAvailable'] / mem['MemTotal']), 1),
            'swap': round(100 * (1 - mem.get('SwapFree', 0) / mem['SwapTotal']), 1) if mem.get('SwapTotal') else None,
            'load': float(read('/proc/loadavg').split()[0]),
            'temp': round(max(temps), 1) if temps else None,
            'disk_used_pct': round(100 * (1 - st.f_bavail / st.f_blocks), 1),
            'disk_free_gb': round(st.f_bavail * st.f_frsize / 1e9, 1),
            'uptime_h': round(float(read('/proc/uptime').split()[0]) / 3600, 1),
        },
        'counters': counters,
        'problems': problems(note),
    }
