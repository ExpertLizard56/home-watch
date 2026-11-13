# Home Watch

Home Watch is a Home Assistant add-on that watches the machines on a home network and tells a phone when something is
wrong. Each machine runs a small reporter every 5 minutes and posts raw counters; the add-on works out the amounts,
keeps the history, runs the checks, sends an alert through a webhook, and serves a page in Home Assistant. No cloud
account and nothing extra to install. Its files are plain files in `/share/home-watch`, and it is MIT licensed.

`README.md` is for whoever installs it. This file is for whoever changes it.

## Layout

`addon/` is the add-on: one container on `python:3.13-alpine`, which `addon/Dockerfile` starts as `python -u /watch.py`.
That base image is published for aarch64, amd64 and armv7, which is why `config.yaml` can list all three without a
`build.yaml`.

- `watch.py` — the hub: takes the reports, keeps each machine's state, runs the checks that need history (hot, disk
  full, memory or CPU high for an hour, silent), sends the alerts, serves the page and the JSON API the page calls.
- `history.py` — the per-machine CSV history, and the averaged points a graph is drawn from.
- `pi.py` — the hub's own report, since nothing else reports on the machine the add-on runs on: CPU, memory, swap,
  load, temperature, the data drive's space, kernel card errors through the Supervisor, and how much the card writes.
  It reports itself as machine `pi`, kind `pi`.
- `services.py` — the service checks from `monitors.json`: the checks themselves, their status and retry rules, the
  event and per-check CSVs, uptime/strips/outages, the push codes and self-heal.
- `trail.py` — the busiest programs every Linux report carries, and the syslog lines machines send in.
- `page.html` — the page: one file, plain SVG, no libraries. Three views (everything, one machine, one check), routed
  by the URL fragment (`#<machine>`, `#check:<name>`).
- `config.yaml` — the add-on's metadata and its few options (see version bumps). `settings.example.json` and
  `monitors.json` are generic examples; `services.py` copies `monitors.json` into a data folder that has no check list
  yet, and never over one that is already there.
- `reporters/` — what the other machines run: `pc.ps1` (Windows) and `homewatch.sh` (Linux and OpenWrt/busybox).
- Root files: `README.md`, `LICENSE`, `repository.yaml` (makes this repo addable as an add-on repository), `ruff.toml`
  (the Python lint rules) and `lefthook.yml` (the checks that run on commit).

## How it works

### Reports

A reporter posts JSON to `POST /report` on port 8790 (host networking), every 5 minutes:

```
{machine, name, kind, metrics:{...}, counters:{...}, cpu_ticks:[total, idle], procs:[[name, %], ...], problems:{key: msg}}
```

Reporters send **raw counters** (bytes read and written, bytes received and sent, CPU ticks since boot). The hub works
out the amounts from the change between two reports — Beszel's idea — so a reporter never does arithmetic of its own,
and a counter that went backwards (a reboot) is simply skipped. A report more than 15 minutes after the last one gets
no amounts either, for the same reason. `problems` is how a reporter raises something the hub could not see from the
numbers (a drive reporting bad health, a drive refusing writes, disk errors in a log).

The metrics land in the history columns `history.COLUMNS`; a machine with none of them leaves blanks.

### The data folder

Everything lives in `/share/home-watch` (`HOME_WATCH_DATA` moves it), as plain files:

- `history/<machine> <year-month>.csv` — one row per report, 12 months kept per machine.
- `services/<year-month>.csv` — one row per check state change (time, name, status, detail). Uptime, the hour strips
  and the outage list are all worked out from these.
- `checks/<year-month>.csv` — one row per check every 5 minutes (checks passed, average response ms): the
  response-time graph. Both service folders keep 12 months.
- `programs/<machine> <year-month>.csv` — programs at 1% of the CPU or more, per report, 12 months.
- `logs/<machine> <year-month>.log` — the syslog lines a machine sent in, 3 months.
- `state.json` — the live state; written at every check and on SIGTERM, so an update or restart loses no report.
- `log.txt` — the add-on's own log (it also goes to stdout).
- `monitors.json` — the check list (below); `settings.json` — the house's own names and numbers (below).
- `push-codes.json` — the push checks' codes, `{"<check name>": "<code>"}`, written whole at 0600.

Once a day the add-on drops the months each folder does not keep.

### Settings

`/share/home-watch/settings.json` is read when the add-on starts (restart after editing). Every key is optional, so a
missing file is normal and the defaults below hold; `settings.example.json` shows the shape with made-up values.

- `machines` — `{"<machine>": "<label>"}` or a plain `["<machine>", ...]`: the order the page shows machines in, and
  the names it shows them by. A machine that reports without being listed still shows, after the listed ones.
- `limits` — `{"<machine>": {"temp": ..., "mem": ..., "disk_used_pct": ..., "cpu_busy": ...}}`: one machine's own
  numbers instead of the generic `LIMITS` in `watch.py`.
- `log_senders` — `{"<ip>": "<machine>"}`: which address belongs to which machine, for the syslog trail.
- `allowed_networks` — the address prefixes allowed to post reports and open the page. Default: the private ranges and
  Home Assistant's ingress. A missing or empty list falls back to that default, so a typo cannot lock the page out.
- `heal_script` — the Home Assistant script self-heal calls, with `{app}` filled in per check (default
  `script.home_watch_heal_{app}`). A value with no `{app}` is refused out loud, since it would call one script for every
  app.

Nothing in `settings.json` is a secret, but it is one house's own list, so it stays out of git — as does the real
`monitors.json`. The page asks `/api/status` for the order and the labels.

### Service checks

`monitors.json` is a plain list, one check per line, read at start. Each check has `name`, optional `group`, a `type`,
`every` (seconds between runs), `retries` (failures in a row before it counts as down) and whatever its type needs:

- `ping` (`host`), `port` (`host`, `port`), `http` (`url`, 200-299 counts), `dns` (`name_to_look_up`, `server`, `port`).
- `dyndns` (`hostname`): asks a public IP-echo service for the network's real address and compares it with what the
  name resolves to. It never touches whatever updates the name. If the echo service itself fails, the check says up —
  a hiccup there must not be reported as a DNS problem.
- `machine` (`machine`): one of Home watch's own machines reported within `quiet` seconds (900 by default) and has no
  problem in its report.
- `push`: something else calls `/api/push/<code>?status=up&msg=...`; silence longer than `every` (plus 30 s) is down.

Other fields: `off_ok: true` turns "not answering" into a grey `off` instead of `down`, and it is not counted against
uptime (a device switched off at the wall is normal). `machine: "<id>"` also puts the check on that machine's page.
`heal: true` (with `app` naming what to restart) makes the check a candidate for self-heal.

A failing check is `pending` until it has failed `retries` more times in a row, then `down` — the rules Uptime Kuma
uses, along with the push URL shape and the strip. A check only writes a row to `services/` when its state changes, so
uptime is cheap to work out and the file stays small.

### Push codes

The codes are **not** in `monitors.json`: that list is a plain file anyone may copy or commit, and a code in it would be
a secret in git. They live in `push-codes.json` in the data folder (0600, written whole), which is why replacing the
check list never loses them. A push check with no code gets one made (`secrets.token_hex(16)`) and a log line that
names the check, never the code. A list still carrying a code in a `"token"` field is migrated on load: the code moves
to `push-codes.json` and out of the list. Arriving codes are compared with `hmac.compare_digest`, so neither a wrong
code nor the time an answer took says anything about the right one. A check's own page shows its push path to copy.

### Alerts and self-heal

Alerts go to the add-on's `alert_webhook` option, a form post with `title` and `message` (any Home Assistant webhook
that forwards to a phone works). At most once a day per problem; a problem that clears is forgotten, so it alerts again
if it comes back. With no webhook set, every alert is only written to the log. `silent_hours` (6 by default) is how long
a machine may go without a report before that is a problem too — shown for a week, then only on the page.

Self-heal is off unless the `self_heal` option is on and `self_heal_webhook` is set; it is a separate webhook from the
alerts. On a check that goes down (or comes back up) it posts Uptime Kuma's message shape —
`{"heartbeat": {"status", "msg", "time"}, "monitor": {"name", "type", "script"}, "msg"}` — where `monitor.script` is
`settings.json`'s `heal_script` with the check's `app` filled in (its name made into an id when it has no `app`), so the
automation behind the webhook can start that script. With the option off, the add-on logs what it would have done.

### Trail

Two things survive a machine's own restart: the busiest programs in its reports (`procs`, which `homewatch.sh` works out
from `/proc`) — the newest full list shows on its page and the programs at 1%+ are kept in `programs/` — and the system
log it sends to UDP 5514. Log lines are kept per sender in `logs/`, `log_senders` turning an address into a machine id
(an unknown sender is filed under its own address); crond's per-job lines and access points' Wi-Fi steering chatter are
dropped, as they are pure noise and cost megabytes.

### The page and its API

`GET /` serves `page.html`; every other GET is one of `/api/status` (state, order, labels), `/api/services` (each
check, its uptime and its 24-hour strip), `/api/history?machine=&days=`, `/api/check?name=&days=`,
`/api/outages?days=`, `/api/log?machine=` and `/api/push/<code>`. Days are clamped to 1-366 and the graphs are averaged
into about 300 points so a year draws as fast as a day. Both doors — the reports and the page — refuse any address that
does not start with one of `allowed_networks`. The page also answers on Home Assistant's ingress, so it can sit in the
sidebar.

## The checks

`LIMITS` in `watch.py` holds the generic numbers: `temp` 85 C, `mem` 95% for an hour, `disk_used_pct` 90%, and
`cpu_busy` off unless a machine asks for one in `settings.json` (it is off for everyone else because a busy CPU is
normal for a PC). A machine's own numbers in `settings.json` replace those.

Who checks what, and why:

- **The reporter** checks what only it can see: on Windows, drive health, any drive under 10% free and disk errors in
  Windows' own log in the last 2 hours; on Linux, space and a drive that refuses a write. Those arrive as `problems`.
- **The hub's own report** (`pi.py`) checks the card it lives on: under 5 GB free, kernel card messages since boot
  (an SD card has no SMART, so this is the main sign of one wearing out), a card refusing writes, eMMC wear where the
  card reports it, and a day's writes against the median of the whole days before it (2 GB and 3x the usual before it
  is called a lot — only whole days count, or a short history makes an ordinary day look enormous).
- **The hub** checks what needs history: a machine with no report for `silent_hours`, memory over its limit for an
  hour, CPU over its limit for an hour (the alert names the busiest programs from that machine's last report), and the
  generic disk-space check — which it skips for `kind` `pi` and `windows`, since those two check their own drives
  properly.
- **The service checks** check everything outside: see above.

## Try a change locally

The add-on runs anywhere Python 3 runs, with the data folder pointed somewhere temporary and the options file at a path
that does not exist (the built-in defaults are then used):

```
cd addon
HOME_WATCH_DATA=<an empty folder> HOME_WATCH_PORT=18790 HOME_WATCH_OPTIONS=none python watch.py
```

Then post a report to `http://127.0.0.1:18790/report` and open `http://127.0.0.1:18790/` — the page, the machine's card
and its graphs should all show the report. `curl http://127.0.0.1:18790/api/status` is the quickest look.

Outside Home Assistant two things must fail, and that is expected: the hub's own report (no `/proc`, no card, no
Supervisor kernel log), and any check whose host does not answer (`ping` on Windows, or a made-up address). Both are
logged and skipped; the rest of the add-on must still work. A reporter can also be run by hand without a hub:
`HOMEWATCH_DRY=1 ./homewatch.sh test "Test"` and `powershell -File pc.ps1 -Dry` print what they would send.

## Checks before a commit

- `python -m ruff check .` — `ruff.toml` is ruff's defaults plus bugbear, line length 140, with `BLE001` ignored on
  purpose: catching every exception, noting it and carrying on is how the add-on stays up.
- `lefthook.yml` runs on every commit: gitleaks (refuses a commit whose new lines hold a password, token or key — this
  repo is public) and ruff on the staged Python. A fresh checkout needs `lefthook install` once. A refused commit means
  fix what it printed; never skip it (`--no-verify`, `LEFTHOOK=0`).
- There is no unit test suite. The cheapest proof of a change is ruff plus the local run above: start it on an empty
  data folder, post a report, fetch `/api/status`, `/api/services` and one graph. `python -m py_compile addon/*.py`
  catches a file that will not even import, and if `page.html`'s script changed, `node --check` the script on its own
  (it is one `<script>` block, and the page has no build step).

## Version bumps

Bump `version` in `addon/config.yaml` in the same commit as any behaviour change. Home Assistant compares that number to
decide whether an update exists, so a change without a bump reaches nobody who already has it installed. Nothing else in
the repo carries a version, and the README needs no edit for a release.

## Credit

Beszel (github.com/henrygd/beszel, MIT): raw counters and deltas, CPU from busy/idle ticks, eMMC wear fields, and one
card per machine. Uptime Kuma (github.com/louislam/uptime-kuma, MIT): the check types and their retry rules, the push
URL shape, the strip, uptime and outage figures, and the self-heal message shape. Netdata, Glances (GPL) and Scrutiny:
ideas only, no code. The detail is at the top of each module.

## Rules

- `python -m ruff check .` before committing. Commit with explicit paths.
- End every commit message with a line naming the AI and its model, so a later reader can tell who changed what, e.g.
  `Co-Authored-By: Cline (Claude Sonnet) <noreply@cline.bot>` (Claude adds its own).
- Nothing secret goes in git, and this repo is public: webhook addresses (`alert_webhook`, `self_heal_webhook` are
  add-on options, not files), push codes (they live in `push-codes.json` in the data folder), and any real address,
  hostname or machine name. The example files — `addon/monitors.json`, `addon/settings.example.json`, the README's
  blocks — stay generic: made-up machine names, a private LAN range, `example.com`.

