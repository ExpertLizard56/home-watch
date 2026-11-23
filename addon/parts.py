"""Part health: another app's own list of its parts and whether each one works, polled every round.

Some apps know more about their insides than a service check can see from outside: a search app with a dozen sources
answers its health page fine while one source has been dead for a week. Such an app serves a JSON list of its parts,
and settings.json's `part_health` names it:

    "part_health": [{"name": "Finder", "url": "http://127.0.0.1:8768/sources/health"}]

The app's answer is an object with a list under `parts` or `sources`, each item {"name", "broken", "reason"}; other
keys are ignored. A part that turns broken sends one notice ("<app>", "<part>: <reason>"), and one more when it works
again ("<part> is working again"). Nothing repeats while a part stays broken, and the state is kept in
DATA/parts.json, so a restart doesn't send them again. A notice that could not be sent is tried again next round.
An app that cannot be reached sends nothing here: whether the app itself is up is a service check's job.
"""

import json
import os
import urllib.request

import history

STATE_FILE = f'{history.DATA}/parts.json'
TIMEOUT = 20

watched = []  # [{"name", "url"}], from settings.json's part_health
unreachable = set()  # apps whose last poll failed, so the log says so once, not every round


def configure(asked, note=None):
    """Take settings.json's part_health: a list of {"name", "url"}; anything else is dropped (and said)."""
    watched.clear()
    if not asked:
        return
    if not isinstance(asked, list):
        if note:
            note("settings.json: part_health is not a list, so it is not used")
        return
    for item in asked:
        if isinstance(item, dict) and str(item.get('name') or '').strip() and str(item.get('url') or '').startswith('http'):
            watched.append({'name': str(item['name']).strip(), 'url': str(item['url']).strip()})
        elif note:
            note(f"settings.json: part_health entry {item!r} needs a name and an http url, so it is not used")


def load_state():
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            got = json.load(f)
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state):
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def fetch(url):
    """The app's parts as {name: (broken, reason)}."""
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
        body = json.loads(r.read().decode('utf-8', 'replace'))
    items = body.get('parts', body.get('sources')) if isinstance(body, dict) else None
    if not isinstance(items, list):
        raise TypeError('no parts or sources list in the answer')
    out = {}
    for item in items:
        if isinstance(item, dict) and str(item.get('name') or '').strip():
            out[str(item['name']).strip()] = (bool(item.get('broken')), str(item.get('reason') or '').strip())
    return out


def check(send, note):
    """One round: poll every watched app and send a notice for each part that changed between working and broken."""
    if not watched:
        return
    state = load_state()
    changed = False
    for app in watched:
        name = app['name']
        try:
            parts = fetch(app['url'])
        except Exception as e:
            if name not in unreachable:
                note(f"part health: could not read {name} ({e}); trying again each round")
                unreachable.add(name)
            continue
        if name in unreachable:
            note(f"part health: {name} answers again")
            unreachable.discard(name)
        known = state.get(name) if isinstance(state.get(name), dict) else {}
        now = {}
        for part, (broken, reason) in parts.items():
            was = bool(known.get(part, {}).get('broken'))
            if broken != was:
                msg = f"{part}: {reason or 'broken'}" if broken else f"{part} is working again"
                note(f"ALERT {name} - {msg}")
                if not send(name, msg):
                    broken = was  # not sent: keep the old state so it is tried again next round
            now[part] = {'broken': broken, 'reason': reason}
        if now != known:
            state[name] = now
            changed = True
    for name in [n for n in state if n not in {a['name'] for a in watched}]:
        del state[name]
        changed = True
    if changed:
        save_state(state)
