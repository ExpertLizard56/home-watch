# Home Watch

A Home Assistant add-on that watches every machine on your home network and tells your phone when something is wrong.

Each machine runs a small reporter every 5 minutes. Home Watch keeps a history of what they send, runs the checks (hot,
low on space, memory full for an hour, a drive or SD card in trouble, a service not answering), shows a page in Home
Assistant, and leaves every notice in Home Assistant's notifications and sends it to your phone. It needs no cloud
account and nothing extra installed.

## What you get

- A card per machine: CPU, memory, temperature, disk space and free space, how much it read, wrote and sent, how long
  it has been up, and the busiest programs on Linux machines.
- A page for each machine and each check, with graphs for a day, a week, a month or a year, % up and its outages.
- Short notes a reporter sends along with its report, shown on the machine's page (a backup's last run, for example).
- Every check's last 24 hours as a strip, an hour per tick.
- A Logs section that downloads the system logs the machines sent in, and Home Watch's own: tick All or some machines,
  pick today, 3, 7, 14 days or everything kept, and get one `.log` back, or a `.zip` with one file per machine.
- A notice at most once a day per problem; a problem that clears can alert again if it comes back.
- A machine that stops reporting (6 hours by default) is a problem too.
- The add-on reports on itself: space, how much its card writes, and the kernel's SD card errors since boot.
- A **Checks** screen to add, change, reorder and remove service checks without a restart: ping, TCP port, HTTP, DNS, a
  dynamic-DNS name still matching your public IP, "one of my machines reported lately" (and, given its address, still on
  the network), a note a reporter sends, and push checks (see below). The list is still a plain JSON file if you prefer
  to edit it by hand.
- A **Machines** screen to name the machines, put them in the order the page shows, give one its own alert limits, and
  forget one (with its stored data), all without editing a file or restarting.

Everything is kept in `/share/home-watch` as plain files: a CSV history per machine (one row every 5 minutes, 12 months
kept), the check list, the settings, and a log.

## Install

1. Settings → Add-ons → Add-on Store.
2. The three dots at the top right → **Repositories**, paste `https://github.com/ExpertLizard56/home-watch`, and add it.
3. Install **Home watch** and start it.

It opens from the sidebar as **Home watch**, or at `http://<your-home-assistant>:8790`.

On the add-on's **Info** tab, turn on **Watchdog** (Home Assistant restarts Home Watch if it hangs or crashes) and, if
you want it, **Auto update**.

Home Assistant builds it on the machine it runs on. It fits `aarch64` (64-bit Raspberry Pi), `amd64` (PCs and VMs) and
`armv7`.

## First run

**Phone notices.** Every notice is left in Home Assistant's notifications (the bell in the sidebar) and then sent to
the phones. Leave the `phones` option empty and every notify service whose name starts with `mobile_app_` gets it — that
is every phone signed in to the Home Assistant app. To pick your own, list the notify services, comma-separated:

```yaml
phones: mobile_app_my_phone, mobile_app_my_tablet
```

To silence the phones for a while, write the time to start again in `/share/phone-quiet-until.txt`, as an ISO time with
a zone:

```
2026-11-16T12:00:00-05:00
```

Until then notices still appear in Home Assistant but no phone gets them. Delete the file to end the quiet early; a
missing file, text that is not a time, or a time with no zone all mean not quiet.

`silent_hours` (6 by default) is how long a machine may stay quiet before that counts as a problem.

**`/share/home-watch/settings.json`** is where your own names and numbers go. The page's **Machines** screen (below)
writes it for you; every key is optional, so a missing file is normal, and editing it by hand and restarting still works.

```json
{
  "machines": {"pc": "Workstation", "nas": "NAS", "router": "Router"},
  "limits": {"router": {"temp": 95, "cpu_busy": 40}, "nas": {"disk_used_pct": 85}},
  "log_senders": {"192.168.1.1": "router"},
  "allowed_networks": ["192.168.1.", "127."]
}
```

- `machines` is the order the page shows them in, with the names it shows them by.
- `limits` gives one machine its own `temp`, `mem`, `disk_used_pct` or `cpu_busy` instead of the generic number.
- `log_senders` says which IP belongs to which machine, for the system-log trail (machines send their syslog to UDP
  5514).
- `allowed_networks` lists the address prefixes allowed to post reports and open the page.

**`/share/home-watch/monitors.json`** is the service check list. The page's **Checks** screen (below) is the main way
to change it: it writes the file for you, keeps each check's `id`, and the running checks pick the change up without a
restart. The file is still plain JSON, one check per line, so editing it by hand and restarting works too. On a
hand-edited list the add-on only writes it back to add a missing `id` (and an `app` for a self-heal check that has
none):

```json
[
  {"id": 1, "name": "Router", "group": "Devices", "type": "ping", "host": "192.168.1.1", "every": 60, "retries": 2},
  {"id": 2, "name": "NAS web page", "group": "Services", "type": "http", "url": "http://192.168.1.10/", "every": 60, "retries": 2},
  {"id": 3, "name": "Nightly backup", "group": "Housekeeping", "type": "push", "every": 7200, "retries": 0},
  {"id": 4, "name": "Workstation", "group": "Devices", "type": "machine", "machine": "pc", "host": "192.168.1.30", "quiet": 900, "every": 60, "retries": 2},
  {"id": 5, "name": "Backup result", "group": "Housekeeping", "type": "note", "machine": "pc", "label": "Backup", "bad_text": "failed", "every": 300, "retries": 1}
]
```

`id` is a whole number that identifies the check. Its history, uptime, outages and push code all live under that id, so
the `name` is only the label shown on the page and in alert text: **rename a check as often as you like, but never
change or reuse its `id`**. A check with no `id` gets the next free number on start, and the list is written back with
it; the add-on also moves history and push codes that were kept under the old name to the id, so an upgrade loses
nothing. Two checks with the same `id` is an error: the second one is logged and skipped.

`every` is seconds between runs; `retries` is how many failures in a row make it down. `"off_ok": true` shows a device
that is simply switched off as grey "off" instead of down. `"machine": "nas"` puts the check on that machine's page.

A `machine` check watches one of Home Watch's own machines: it is down when that machine has not reported for `quiet`
seconds (900 by default) or has a problem in its report. Give it a `host` (the machine's address) and it also pings
that address every round, the way a `ping` check does, so one check covers both questions: is it on the network, and is
its reporter still sending. It is then down with `Not on the network` when the address does not answer, and down with
`On the network, but its reporter stopped sending` when the address answers but the report is stale. Its response time
is the ping time. Without `host` the check only watches the reports.

A `note` check watches a note one of Home Watch's own machines sends in its report: `label` names the note exactly,
and `good_text` and/or `bad_text` are plain text matched anywhere in it, ignoring case. It is up when the note is there
and holds the good text (if set) and not the bad text (if set); `bad_text` is checked first, so "not answering" can be
down while "answering" is up. The detail quotes the note's text. A note check is down when its machine has not
reported for the machine check's default quiet time (900 s), and it puts itself on that machine's page.

An `http` check counts any 200-299 answer as up; add `"expect": 204` (or another code) to require exactly that status.
`"heal": true` with an `"app"` makes the check a candidate for self-heal: Home Assistant runs
`script.home_watch_heal_<app>` when it goes down or comes back. A self-heal check with no `app` gets one made from its
name on start, so renaming it later still calls the same script.

The example list in `addon/monitors.json` in this repository has one of every type.

## The Checks screen

Open **Checks** on the page (the button at the top right of the overview). It is the main way to change the list:

- the checks in the order the page shows them, with their group, type and state, and buttons to move one up or down,
  edit it, or remove it. Removing asks first and deletes that check's stored history too;
- an Add/Edit form that shows only the fields the chosen type uses, each with a one-line hint, and says what is wrong
  next to the field before anything is saved;
- after saving a push check, the URL to call and a one-line `curl` example, with a button to make a new code (the old
  URL then stops working);
- for a machine check, a picker of the machines that have reported (or type an id for one that has not), an optional
  address to ping too, and the quiet time. It is green only when the device answers the ping (if a host is set) **and**
  its reporter has sent within the quiet time;
- for a note check, the same picker of reported machines, the note's exact label (suggested from that machine's current
  notes once it is picked), and the good or bad text to look for. It is green only when the machine has reported lately
  and its note says the right thing;
- a short "Make your own reporter" panel with the `/report` URL and the JSON it accepts.

Changing checks is only allowed from Home Assistant's panel (the sidebar), because that is signed in. Opening the page
on port 8790 still shows everything, but refuses a change. Saving writes `monitors.json` at once and the running checks
pick it up without a restart; the hidden `id` of an edited check is kept, and a new check gets a new one that is never
reused.

## The Machines screen

Open **Machines** on the page (next to **Checks**). It lists every machine that has reported and every one
`settings.json` lists, in the order the page shows:

- move a machine up or down, or edit it to change the name the page calls it and its own `temp`, `mem`,
  `disk_used_pct` or `cpu_busy` limit. A blank limit means the generic one, shown under the field;
- forget a machine: it asks first, then drops its stored history, programs and logs and its entry in `settings.json`.
  A machine that keeps reporting shows up again, with no history;
- saving writes `settings.json` at once (every other key in it is kept), so the page and the alerts use the new name
  and numbers without a restart.

Like the Checks screen, changing machines is only allowed through Home Assistant's panel; port 8790 shows the list but
refuses a change.

## Reporters

Nothing needs installing on the Home Assistant machine: the add-on reports on it by itself. Other machines send their
report to port 8790 (`http://<your-home-assistant>:8790/report`). They only send raw counters; Home Watch works out the
amounts.

A report can also carry `notes`, short `{label: text}` lines the machine wants shown on its page (a backup's last run,
say). They are information only, never a problem or an alert, and each report replaces them.

### Windows: `reporters/pc.ps1`

Copy it somewhere permanent, then create a scheduled task from an administrator Command Prompt, every 5 minutes:

```bat
schtasks /Create /TN "Home watch report" /SC MINUTE /MO 5 /RU SYSTEM /TR "conhost.exe --headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Tools\pc.ps1 -Hub http://homeassistant.local:8790/report"
```

Running it as `SYSTEM` means no password is stored, and it is what lets the script read the drive's wear and temperature;
as a normal user those two numbers stay empty and everything else still works. It reports as machine `pc`, name
`This PC`; edit those two lines in
the script if you want another name. It also does the PC's own drive checks: drive health, any drive under 10% free,
and disk errors in Windows' log. `powershell -File pc.ps1 -Dry` prints what it would send instead of sending it. If the
hub stops taking reports for 30 minutes, the PC shows a Windows notice of its own.

### Linux and OpenWrt: `reporters/homewatch.sh`

```sh
install -m 755 homewatch.sh /usr/local/bin/homewatch
```

Then run it every 5 minutes from cron. On a normal Linux machine, `/etc/cron.d/home-watch`:

```
*/5 * * * * root HOMEWATCH_HUB=http://homeassistant.local:8790/report /usr/local/bin/homewatch laptop "Laptop"
```

On OpenWrt, copy it to `/usr/bin/homewatch`, add the same line (without the `root`) to root's crontab with
`crontab -e`, and add `/usr/bin/homewatch` to `/etc/sysupgrade.conf` so a firmware update keeps it. It also sends each
program's share of the CPU, from `/proc`.

### Android TV boxes: `reporters/android-tv/`

An app called Home Watch for Google TV and Android TV boxes (Android 9 or newer). Install the APK from this repo's
releases (with a sideloading app such as Downloader, or `adb install`), open it, and type the hub's address
(`http://homeassistant.local:8790`) and the name the box should have on the hub. It reports every 5 minutes, also
while the box sleeps, and starts again by itself after a restart: memory, swap, storage, network bytes and uptime.

For the CPU and the temperature it needs the box's own system access: turn on network debugging (Settings > System >
Developer options), press **Allow system access** in the app, and answer the box's "Allow USB debugging?" with
*Always allow*. Without it the rest still goes. If
[StreamBox Keeper](https://github.com/ExpertLizard56/streambox-keeper) is on the box, what its screen shows
goes in the notes (its last cleanup, restart and app update, the TV remote), and a TV remote that stops answering is a
problem.

A box can also be set from a computer, without its remote:
`adb shell am broadcast -a io.github.expertlizard56.homewatch.tv.SET -n io.github.expertlizard56.homewatch.tv/.Setup --es hub http://homeassistant.local:8790 --es name "Living room box"`
(`--es machine living-room-box` to choose the id too; by default it comes from the name the box had first).

The two scripts can be run by hand to see what it sends:
`HOMEWATCH_HUB=http://... HOMEWATCH_DRY=1 ./homewatch.sh test "Test"`.

The hub's address has no default. Without `-Hub`/`HOMEWATCH_HUB` the reporter says so and stops, rather than guessing.

## Push checks

For something that cannot be pinged but can run a command: a nightly backup that should check in, a cron job on a
machine without a reporter.

```json
{"id": 3, "name": "Nightly backup", "group": "Housekeeping", "type": "push", "every": 7200, "retries": 0}
```

Add it on the Checks screen (or in the file, then restart), and copy the push path it shows. Call it when the job
finishes, from anywhere on your home network:

```sh
curl "http://homeassistant.local:8790/api/push/<code>?status=up&msg=backup%20done"
```

No news for longer than `every` seconds (plus a 30-second grace) makes the check down; send `status=down` to report a
failure. The codes are made when a push check is added and kept in `/share/home-watch/push-codes.json` under each
check's id, not in `monitors.json`; the Checks screen and the check's own page both show the push path to copy. The path
carries the code, not the name, so renaming the check does not change it, and the Checks screen can make a new code.

## What it needs

- Home Assistant OS or Home Assistant Supervised: Home Watch is an add-on, and it keeps its files in the `share` folder.
  Home Assistant Container has no add-ons.
- The Supervisor `manager` role, which is what lets it ask the Supervisor for the host's kernel log. That log is how it
  notices SD and eMMC card errors on the machine it runs on, which is the trouble a worn-out card shows first.
- Host networking, so the other machines can reach port 8790. Only the private address ranges may post reports or open
  the page by default (`allowed_networks` in `settings.json`).
- No internet access of its own. It only makes the connections your checks ask for, and leaves its notices in Home
  Assistant.

## Credit

The reporters send raw counters and the hub works out amounts from the change between two reports, the way
[Beszel](https://github.com/henrygd/beszel) (MIT) does; CPU time comes from busy/idle ticks, also Beszel's way. The check
types, their retry rules, the strip, uptime and outage figures, and the push URL shape follow
[Uptime Kuma](https://github.com/louislam/uptime-kuma) (MIT). Netdata, Glances and Scrutiny were read for ideas only, no
code. Details are at the top of `addon/watch.py`.

## Licence

MIT, see [LICENSE](LICENSE). Copyright (c) 2026 ExpertLizard56.
