# Home Watch

A Home Assistant add-on that watches every machine on your home network and tells your phone when something is wrong.

Each machine runs a small reporter every 5 minutes. Home Watch keeps a history of what they send, runs the checks (hot,
low on space, memory full for an hour, a drive or SD card in trouble, a service not answering), shows a page in Home
Assistant, and sends an alert through a webhook. It needs no cloud account and nothing extra installed.

## What you get

- A card per machine: CPU, memory, temperature, disk space and free space, how much it read, wrote and sent, how long
  it has been up, and the busiest programs on Linux machines.
- A page for each machine and each check, with graphs for a day, a week, a month or a year, % up and its outages.
- Every check's last 24 hours as a strip, an hour per tick.
- A Logs section that downloads the system logs the machines sent in, and Home Watch's own: tick All or some machines,
  pick today, 3, 7, 14 days or everything kept, and get one `.log` back, or a `.zip` with one file per machine.
- Alerts at most once a day per problem; a problem that clears can alert again if it comes back.
- A machine that stops reporting (6 hours by default) is a problem too.
- The add-on reports on itself: space, how much its card writes, and the kernel's SD card errors since boot.
- Service checks written by hand in a plain JSON file: ping, TCP port, HTTP, DNS, a dynamic-DNS name still matching your
  public IP, "one of my machines reported lately", and push checks (see below).

Everything is kept in `/share/home-watch` as plain files: a CSV history per machine (one row every 5 minutes, 12 months
kept), the check list, the settings, and a log.

## Install

1. Settings → Add-ons → Add-on Store.
2. The three dots at the top right → **Repositories**, paste `https://github.com/ExpertLizard56/home-watch`, and add it.
3. Install **Home watch** and start it.

It opens from the sidebar as **Home watch**, or at `http://<your-home-assistant>:8790`.

Home Assistant builds it on the machine it runs on. It fits `aarch64` (64-bit Raspberry Pi), `amd64` (PCs and VMs) and
`armv7`.

## First run

**Alerts.** In the add-on's Configuration tab, set `alert_webhook` to a Home Assistant webhook URL, for example
`http://homeassistant.local:8123/api/webhook/home_watch_alert`. Home Watch posts a form with `title` and `message`.
An automation that sends it to your phone:

```yaml
automation:
  - alias: Home watch alerts
    triggers:
      - trigger: webhook
        webhook_id: home_watch_alert
        allowed_methods:
          - POST
    actions:
      - action: notify.mobile_app_my_phone
        data:
          title: "{{ trigger.data.title }}"
          message: "{{ trigger.data.message }}"
```

Leave it empty and alerts are only written to the add-on's log. `silent_hours` (6 by default) is how long a machine may
stay quiet before that counts as a problem.

**`/share/home-watch/settings.json`** is where your own names and numbers go. Every key is optional, so a missing file
is normal; it is read when the add-on starts, so restart the add-on after editing.

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

**`/share/home-watch/monitors.json`** is the service check list. The add-on writes the example list there on its first
start and never overwrites it. One check per line, edit by hand, restart the add-on:

```json
[
  {"name": "Router", "group": "Devices", "type": "ping", "host": "192.168.1.1", "every": 60, "retries": 2},
  {"name": "NAS web page", "group": "Services", "type": "http", "url": "http://192.168.1.10/", "every": 60, "retries": 2},
  {"name": "Nightly backup", "group": "Housekeeping", "type": "push", "every": 7200, "retries": 0}
]
```

`every` is seconds between runs; `retries` is how many failures in a row make it down. `"off_ok": true` shows a device
that is simply switched off as grey "off" instead of down. `"machine": "nas"` puts the check on that machine's page.

The example list in `addon/monitors.json` in this repository has one of every type.

## Reporters

Nothing needs installing on the Home Assistant machine: the add-on reports on it by itself. Other machines send their
report to port 8790 (`http://<your-home-assistant>:8790/report`). They only send raw counters; Home Watch works out the
amounts.

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

Either reporter can be run by hand to see what it sends:
`HOMEWATCH_HUB=http://... HOMEWATCH_DRY=1 ./homewatch.sh test "Test"`.

The hub's address has no default. Without `-Hub`/`HOMEWATCH_HUB` the reporter says so and stops, rather than guessing.

## Push checks

For something that cannot be pinged but can run a command: a nightly backup that should check in, a cron job on a
machine without a reporter.

```json
{"name": "Nightly backup", "group": "Housekeeping", "type": "push", "every": 7200, "retries": 0}
```

Restart the add-on, open that check's page from Home Watch, and copy the push path it shows. Call it when the job
finishes, from anywhere on your home network:

```sh
curl "http://homeassistant.local:8790/api/push/<code>?status=up&msg=backup%20done"
```

No news for longer than `every` seconds (plus a 30-second grace) makes the check down; send `status=down` to report a
failure. The codes are made on first start and kept in `/share/home-watch/push-codes.json`, not in `monitors.json`, and
a check's page is where you copy its push path from.

## What it needs

- Home Assistant OS or Home Assistant Supervised: Home Watch is an add-on, and it keeps its files in the `share` folder.
  Home Assistant Container has no add-ons.
- The Supervisor `manager` role, which is what lets it ask the Supervisor for the host's kernel log. That log is how it
  notices SD and eMMC card errors on the machine it runs on, which is the trouble a worn-out card shows first.
- Host networking, so the other machines can reach port 8790. Only the private address ranges may post reports or open
  the page by default (`allowed_networks` in `settings.json`).
- No internet access of its own. It only makes the connections your checks ask for, and posts your alerts to your own
  webhook.

## Credit

The reporters send raw counters and the hub works out amounts from the change between two reports, the way
[Beszel](https://github.com/henrygd/beszel) (MIT) does; CPU time comes from busy/idle ticks, also Beszel's way. The check
types, their retry rules, the strip, uptime and outage figures, and the push URL shape follow
[Uptime Kuma](https://github.com/louislam/uptime-kuma) (MIT). Netdata, Glances and Scrutiny were read for ideas only, no
code. Details are at the top of `addon/watch.py`.

## Licence

MIT, see [LICENSE](LICENSE). Copyright (c) 2026 ExpertLizard56.
