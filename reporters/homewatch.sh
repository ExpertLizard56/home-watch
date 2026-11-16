#!/bin/sh
# Home watch reporter for Linux machines: Debian and the like, and OpenWrt routers and access points (busybox).
# Cron runs it every 5 minutes as root:  homewatch <machine> "<Name>"
# It sends raw counters; the hub works out amounts from the change between reports (Beszel's way, MIT, see watch.py).
# Also the drive checks: drive space (judged on the hub) and whether the drive still takes writes.
# The hub's address is HOMEWATCH_HUB, e.g. HOMEWATCH_HUB=http://hub:8790/report (there is no default: a
# report has to go somewhere, so it says so and stops if the address is missing). HOMEWATCH_DRY=1 only prints it.
MACHINE=${1:?machine}
NAME=${2:-$1}

cpu=$(awk '/^cpu /{t=0; for(i=2;i<=NF;i++) t+=$i; print t "," ($5+$6); exit}' /proc/stat)
mem=$(awk '/^MemTotal/{t=$2} /^MemAvailable/{a=$2} /^SwapTotal/{st=$2} /^SwapFree/{sf=$2}
  END{printf "\"mem\":%.1f", (t-a)*100/t; if (st>0) printf ",\"swap\":%.1f", (st-sf)*100/st}' /proc/meminfo)
load=$(cut -d' ' -f1 /proc/loadavg)

# Every program's share of the CPU since the last report (all cores = 100%, like "cpu"), busiest first. Worked out from
# each process's CPU time now and at the last report (kept in /tmp, memory on OpenWrt); kernel threads count too.
# Reading every process is needed to find the busiest anyway, so the full list costs nothing more. A machine can run
# with one core stuck busy for days and then hang, and nothing says which program it was.
SNAP=/tmp/.homewatch-procs
total=${cpu%%,*}
procs=$(cat /proc/[0-9]*/stat 2>/dev/null | awk -v total="$total" -v snap="$SNAP" '
  BEGIN { if ((getline line < snap) > 0) { pt = line; while ((getline line < snap) > 0) { split(line, a, " ");
            prev[a[1]] = a[2]; pname[a[1]] = substr(line, length(a[1]) + length(a[2]) + 3) } } }
  { o = index($0, "("); c = 0; for (i = length($0); i > o; i--) if (substr($0, i, 1) == ")") { c = i; break }
    if (!o || !c) next
    pid = substr($0, 1, o - 2); name = substr($0, o + 1, c - o - 1); gsub(/["\\]/, "", name)
    split(substr($0, c + 2), f, " "); t = f[12] + f[13]; now[pid] = t; nm[pid] = name }
  END { printf "%s\n", total > snap
        for (pid in now) printf "%s %s %s\n", pid, now[pid], nm[pid] > snap
        if (!pt || total <= pt) exit
        for (pid in now) { d = (pid in prev && pname[pid] == nm[pid]) ? now[pid] - prev[pid] : now[pid]
                           if (d > 0) use[nm[pid]] += d; else if (!(nm[pid] in use)) use[nm[pid]] += 0 }
        n = 0; for (k in use) { n++; key[n] = k }
        for (i = 2; i <= n; i++) { k = key[i]; for (j = i - 1; j > 0 && use[key[j]] < use[k]; j--) key[j + 1] = key[j]; key[j + 1] = k }
        for (i = 1; i <= n; i++) printf "%s[\"%s\",%.1f]", (i > 1 ? "," : ""), key[i], 100 * use[key[i]] / (total - pt) }')
up=$(awk '{printf "%.1f", $1/3600}' /proc/uptime)

# Hottest sensor (CPU or board), in C.
temp=$(cat /sys/class/hwmon/hwmon*/temp*_input /sys/class/thermal/thermal_zone*/temp 2>/dev/null |
  awk '$1>0 && $1<150000 {if ($1>m) m=$1} END{if (m) printf "%.1f", m/1000}')

# Space: OpenWrt's settings live on /overlay, a normal machine on /.
mount=/; [ -d /overlay ] && grep -q ' /overlay ' /proc/mounts && mount=/overlay
space=$(df -P "$mount" | awk 'NR==2 {sub("%","",$5); printf "\"disk_used_pct\":%s,\"disk_free_gb\":%.1f", $5, $4/1048576}')

# Whole drives only (sda, nvme0n1, mmcblk0), not partitions: bytes read and written.
disk=$(awk '$3 ~ /^(sd[a-z]|nvme[0-9]+n[0-9]+|mmcblk[0-9]+|vd[a-z])$/ {r+=$6; w+=$10}
  END{printf "\"disk_read_bytes\":%.0f,\"disk_write_bytes\":%.0f", r*512, w*512}' /proc/diskstats)

# Network: the router counts its internet port, an access point its LAN bridge, anything else its real ports.
dev=""
command -v ifstatus >/dev/null && dev=$(ifstatus wan 2>/dev/null | sed -n 's/.*"l3_device": "\(.*\)".*/\1/p')
[ -z "$dev" ] && [ -d /sys/class/net/br-lan ] && dev=br-lan
net=$(awk -v dev="$dev" -F'[: ]+' 'NR>2 { i=($1=="")?2:1; n=$i;
  if ((dev!="" && n==dev) || (dev=="" && n ~ /^(eth|en|wl)/)) {rx+=$(i+1); tx+=$(i+9)} }
  END{printf "\"net_rx_bytes\":%.0f,\"net_tx_bytes\":%.0f", rx, tx}' /proc/net/dev)

# Wi-Fi devices connected (access points and router).
wifi=""
if command -v iw >/dev/null; then
  n=0; for i in $(iw dev 2>/dev/null | awk '/Interface/{print $2}'); do
    n=$((n + $(iw dev "$i" station dump 2>/dev/null | grep -c '^Station'))); done
  wifi=",\"wifi_clients\":$n"
fi

# Drive temperature from SMART where smartctl is installed. Drive health alerts stay with smartd.
dtemp=""
if command -v smartctl >/dev/null; then
  for d in /dev/sda /dev/nvme0; do [ -e "$d" ] || continue
    t=$(smartctl -A "$d" 2>/dev/null | awk '/Temperature_Celsius|Airflow_Temperature/ {print $10; exit} /^Temperature:/ {print $2; exit}')
    [ -n "$t" ] && dtemp=",\"drive_temp\":$t" && break; done
fi

# Does the drive still take writes? Not on OpenWrt: its flash wears from writes, and /tmp there is memory.
problems=""
if [ "$mount" = / ] && ! { echo ok > /var/tmp/.homewatch && rm -f /var/tmp/.homewatch; } 2>/dev/null; then
  problems="\"drive-write\":\"The drive is refusing writes. It may be failing\""
fi

# Extra checks: every executable in /etc/homewatch.d (e.g. the router's AdGuard health) prints lines
# "problem <key> <message>" (alerts) or "note <label>: <text>" (shown on the machine's page).
notes=""
for x in /etc/homewatch.d/*; do
  [ -x "$x" ] || continue
  # JSON-safe without escaping: backslashes dropped, double quotes become single, tabs spaces.
  out=$("$x" 2>/dev/null | tr -d '\' | tr '"\t' "' " | awk '
    $1 == "problem" && NF > 2 { k = $2; sub(/^problem +[^ ]+ +/, ""); p = p (p ? "," : "") "\"" k "\":\"" $0 "\"" }
    $1 == "note" && index($0, ": ") { sub(/^note +/, ""); i = index($0, ": ")
      n = n (n ? "," : "") "\"" substr($0, 1, i - 1) "\":\"" substr($0, i + 2) "\"" }
    END { print p; print n }')
  p=$(echo "$out" | sed -n 1p); n=$(echo "$out" | sed -n 2p)
  [ -n "$p" ] && problems="${problems:+$problems,}$p"
  [ -n "$n" ] && notes="${notes:+$notes,}$n"
done

body="{\"machine\":\"$MACHINE\",\"name\":\"$NAME\",\"kind\":\"linux\",\"cpu_ticks\":[$cpu],
\"metrics\":{$mem,\"load\":$load,\"uptime_h\":$up${temp:+,\"temp\":$temp},$space$wifi$dtemp},
\"counters\":{$disk,$net},\"procs\":[$procs],\"problems\":{$problems},\"notes\":{$notes}}"

if [ -n "$HOMEWATCH_DRY" ]; then
  echo "$body"
else
  HUB=${HOMEWATCH_HUB:?HOMEWATCH_HUB is not set: set it to the hub report address, e.g. http://hub:8790/report}
  if command -v curl >/dev/null; then
    curl -s -m 20 -H 'Content-Type: application/json' -d "$body" "$HUB" >/dev/null
  else
    wget -q -T 20 -O /dev/null --header='Content-Type: application/json' --post-data="$body" "$HUB"
  fi
fi
