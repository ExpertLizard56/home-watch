# Home watch reporter for a Windows PC. A scheduled task (e.g. "Home watch report") runs it every 5 minutes, hidden,
# through conhost --headless. It sends raw counters to the hub, which works out amounts from the change between
# reports (Beszel's way, MIT; see addon/watch.py), plus the drive checks: drive health, low space and disk errors in
# Windows' log, and the things listed in checks.json that must be running. If the hub stops taking reports for 30
# minutes, it shows a Windows notice (phone alerts come from it).
# -Dry prints the report instead of sending it.
# The hub's address comes from -Hub (or HOMEWATCH_HUB): there is no default, so it says so and stops if it is missing.

param([switch]$Dry, [string]$Hub, [string]$Checks)

$hub   = if ($Hub) { $Hub } else { $env:HOMEWATCH_HUB }
$local = Join-Path $env:LOCALAPPDATA 'Home watch'
if (-not $Dry -and -not $hub) {
    Write-Error 'Home watch reporter: no hub address. Pass -Hub http://hub:8790/report (or set HOMEWATCH_HUB).'
    exit 2
}
New-Item -ItemType Directory -Force $local | Out-Null

# CPU busy/idle ticks since boot, like Linux's /proc/stat (GetSystemTimes: kernel time includes idle).
Add-Type -Namespace HomeWatch -Name Win -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern bool GetSystemTimes(out long idle, out long kernel, out long user);
'@
$idle = 0L; $kernel = 0L; $user = 0L
[HomeWatch.Win]::GetSystemTimes([ref]$idle, [ref]$kernel, [ref]$user) | Out-Null

$os  = Get-CimInstance Win32_OperatingSystem
$mem = [math]::Round(100 * (1 - $os.FreePhysicalMemory / $os.TotalVisibleMemorySize), 1)
$up  = [math]::Round(((Get-Date) - $os.LastBootUpTime).TotalHours, 1)
$c   = Get-Volume -DriveLetter C
$disk = Get-CimInstance Win32_PerfRawData_PerfDisk_PhysicalDisk -Filter "Name='_Total'"
$net = Get-NetAdapter -Physical -ErrorAction SilentlyContinue | Where-Object Status -eq 'Up' | Get-NetAdapterStatistics -ErrorAction SilentlyContinue |
    Measure-Object ReceivedBytes, SentBytes -Sum

$metrics = [ordered]@{
    mem = $mem; uptime_h = $up
    disk_used_pct = [math]::Round(100 * (1 - $c.SizeRemaining / $c.Size), 1)
    disk_free_gb  = [math]::Round($c.SizeRemaining / 1GB, 1)
}
$gpu = & nvidia-smi --query-gpu=temperature.gpu,utilization.gpu,power.draw --format=csv,noheader,nounits 2>$null
if ($LASTEXITCODE -eq 0 -and $gpu) {
    $g = ($gpu | Select-Object -First 1) -split ',\s*'
    $metrics.gpu_temp = [double]$g[0]; $metrics.gpu_use = [double]$g[1]; $metrics.gpu_power = [double]$g[2]
}

# Drive checks: drive health, under 10% free, disk errors in Windows' log in the last 2 hours.
$problems = [ordered]@{}
Get-PhysicalDisk | Where-Object { $_.HealthStatus -ne 'Healthy' } | ForEach-Object {
    $problems["health-$($_.DeviceId)"] = "$($_.FriendlyName) reports $($_.HealthStatus)"
}
Get-Volume | Where-Object { $_.DriveLetter -and $_.DriveType -eq 'Fixed' -and $_.Size -gt 1GB -and ($_.SizeRemaining / $_.Size) -lt 0.10 } | ForEach-Object {
    $problems["space-$($_.DriveLetter)"] = "$($_.DriveLetter): is under 10% free ($([int]($_.SizeRemaining / 1GB)) GB left)"
}
$errors = @(Get-WinEvent -FilterHashtable @{ LogName = 'System'; ProviderName = 'disk', 'Ntfs', 'stornvme'; Level = 1, 2; StartTime = (Get-Date).AddHours(-2) } -MaxEvents 20 -ErrorAction SilentlyContinue)
if ($errors.Count -gt 0) { $problems['disk-errors'] = "$($errors.Count) disk errors in Windows' log in the last 2 hours" }

# Drive wear, temperature and uncorrected errors (only readable when the task runs with highest privileges). NVMe
# drives: their own health log (page 2), as Windows' disk settings page reads it, since Windows' Wear counter is often
# blank for them. Other drives: Windows' reliability counters.
Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices; using Microsoft.Win32.SafeHandles;
public static class NvmeHealth {
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern SafeFileHandle CreateFile(string n, uint a, uint s, IntPtr sa, uint c, uint f, IntPtr t);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool DeviceIoControl(SafeFileHandle h, uint code, byte[] i, int il, byte[] o, int ol, out int r, IntPtr ov);
    public static int Error;
    public static byte[] Log(string disk) {
        using (var h = CreateFile(@"\\.\PhysicalDrive" + disk, 0xC0000000, 3, IntPtr.Zero, 3, 0, IntPtr.Zero)) {
            if (h.IsInvalid) { Error = Marshal.GetLastWin32Error(); return null; }
            var buf = new byte[8 + 40 + 512];
            BitConverter.GetBytes(50).CopyTo(buf, 0);   // StorageDeviceProtocolSpecificProperty, standard query
            BitConverter.GetBytes(3).CopyTo(buf, 8);    // NVMe
            BitConverter.GetBytes(2).CopyTo(buf, 12);   // a log page
            BitConverter.GetBytes(2).CopyTo(buf, 16);   // page 2: SMART / health
            BitConverter.GetBytes(40).CopyTo(buf, 24);  // the page follows the 40-byte request
            BitConverter.GetBytes(512).CopyTo(buf, 28);
            int got;
            if (!DeviceIoControl(h, 0x2D1400, buf, buf.Length, buf, buf.Length, out got, IntPtr.Zero)) { Error = Marshal.GetLastWin32Error(); return null; }
            var log = new byte[512]; Array.Copy(buf, 48, log, 0, 512); return log;
        }
    }
}
'@ -ErrorAction SilentlyContinue
$wear = @(); $temps = @()
foreach ($d in @(Get-PhysicalDisk)) {
    $id = $d.DeviceId
    $log = if ($d.BusType -eq 'NVMe') { [NvmeHealth]::Log($id) }
    if ($log) {
        $temps += [BitConverter]::ToUInt16($log, 1) - 273
        $wear += [int]$log[5]
        $media = [BitConverter]::ToUInt64($log, 160)
        if ($media -gt 0) { $problems["drive-errors-$id"] = "Drive $id has $media errors it couldn't correct. Back it up." }
        if ($log[0] -band 0x0D) { $problems["drive-warning-$id"] = "Drive $id is warning about itself (spare space low, unreliable or read-only). Back it up." }
        continue
    }
    try {
        $r = $d | Get-StorageReliabilityCounter -ErrorAction Stop
        if ($r.Temperature -gt 0) { $temps += $r.Temperature }
        if ($null -ne $r.Wear) { $wear += [int]$r.Wear }
        $bad = [long]$r.ReadErrorsUncorrected + [long]$r.WriteErrorsUncorrected
        if ($bad -gt 0) { $problems["drive-errors-$id"] = "Drive $id has $bad errors it couldn't correct. Back it up." }
    } catch { }
}
if ($temps.Count) { $metrics.drive_temp = ($temps | Measure-Object -Maximum).Maximum }
if ($wear.Count) { $metrics.drive_wear = ($wear | Measure-Object -Maximum).Maximum }  # the hub's drive_wear limit decides

# Things that must be running: a list of checks in checks.json (-Checks, else "Home watch\checks.json" in the user's
# local app data; no file = no checks). Each is {"name": ..., and one of:}
#   "service": "<name>"                       the Windows service is running
#   "process": "<name>"                       a process by that name is running (signed-in things)
#   "task": "<name>", "after": "<process>"    the scheduled task ran since sign-in and ended with 0 (and, with
#                                             "after", ran after that process last started, for a setting it overwrites)
#   "url": "<http address>"                   it answers
#   "share": "<name>", "path": "<folder>"     the file share is there (and points at that folder)
#   "folder": "<path>", "only": [accounts]    nobody but those accounts is on the folder's permissions
# Each failing one is a problem "down-<name>". Sign-in things get 10 minutes after sign-in to start.
$checksFile = if ($Checks) { $Checks } else { Join-Path $local 'checks.json' }
if (Test-Path $checksFile) {
    $signIn = (Get-Process explorer -ErrorAction SilentlyContinue | Sort-Object StartTime | Select-Object -First 1).StartTime
    $settled = $signIn -and ((Get-Date) - $signIn).TotalMinutes -ge 10
    $booted = ((Get-Date) - $os.LastBootUpTime).TotalMinutes -ge 10
    try { $list = Get-Content $checksFile -Raw -Encoding UTF8 | ConvertFrom-Json; $list = @($list) } catch { $list = @(); $problems['checks-file'] = "checks.json can't be read: $($_.Exception.Message)" }
    foreach ($k in $list) {
        $why = $null
        try {
            if ($k.service) {
                if (-not $booted) { continue }
                $s = Get-Service $k.service -ErrorAction SilentlyContinue
                if (-not $s) { $why = 'service missing' } elseif ($s.Status -ne 'Running') { $why = "service $($s.Status)" }
            } elseif ($k.process) {
                if (-not $settled) { continue }
                if (-not (Get-Process $k.process -ErrorAction SilentlyContinue)) { $why = 'not running' }
            } elseif ($k.task) {
                if (-not $settled) { continue }
                $i = Get-ScheduledTaskInfo -TaskName $k.task -ErrorAction SilentlyContinue
                $after = if ($k.after) { Get-Process $k.after -ErrorAction SilentlyContinue | Sort-Object StartTime -Descending | Select-Object -First 1 }
                if (-not $i) { $why = 'its task is missing' }
                elseif ($i.LastRunTime -lt $signIn) { $why = "its task didn't run after sign-in" }
                elseif ($after -and $after.StartTime -gt $i.LastRunTime) { $why = "$($k.after) started after its task ran" }
                elseif ($i.LastTaskResult -ne 0 -and $i.LastTaskResult -ne 267009) { $why = "its task ended with code $($i.LastTaskResult)" }
            } elseif ($k.url) {
                if (-not $booted) { continue }
                try { [void](Invoke-WebRequest $k.url -UseBasicParsing -TimeoutSec 15) } catch { $why = "not answering ($($_.Exception.Message))" }
            } elseif ($k.share) {
                if (-not $booted) { continue }
                $sh = Get-SmbShare -Name $k.share -ErrorAction SilentlyContinue
                if (-not $sh) { $why = 'the share is gone (or file sharing is off)' }
                elseif ($k.path -and $sh.Path -ne $k.path) { $why = "the share points at $($sh.Path)" }
            } elseif ($k.folder) {
                $who = @((Get-Acl $k.folder -ErrorAction Stop).Access | ForEach-Object { "$($_.IdentityReference)" } | Select-Object -Unique)
                $other = @($who | Where-Object { $w = $_; -not (@($k.only) | Where-Object { $w -eq $_ -or $w -like "*\$_" }) })
                if ($other.Count) { $why = "also has $($other -join ', ') on it" }
            }
        } catch { $why = "couldn't check: $($_.Exception.Message)" }
        if ($why) { $problems["down-$($k.name)"] = "$($k.name): $why" }
    }
}

$report = [ordered]@{
    machine = 'pc'; name = 'This PC'; kind = 'windows'
    cpu_ticks = @(($kernel + $user), $idle)
    metrics = $metrics
    counters = [ordered]@{
        disk_read_bytes = [double]$disk.DiskReadBytesPersec; disk_write_bytes = [double]$disk.DiskWriteBytesPersec
        net_rx_bytes = [double]($net | Where-Object Property -eq 'ReceivedBytes').Sum
        net_tx_bytes = [double]($net | Where-Object Property -eq 'SentBytes').Sum
    }
    problems = $problems
}
$json = $report | ConvertTo-Json -Depth 4 -Compress
if ($Dry) { $json; exit 0 }

$failFile = Join-Path $local 'Hub not answering since.txt'
try {
    Invoke-RestMethod $hub -Method Post -Body ([Text.Encoding]::UTF8.GetBytes($json)) -ContentType 'application/json' -TimeoutSec 20 | Out-Null
    Remove-Item $failFile -ErrorAction SilentlyContinue
} catch {
    if (-not (Test-Path $failFile)) { Set-Content $failFile (Get-Date -Format o) -Encoding UTF8 }
    $since = [datetime](Get-Content $failFile -Raw).Trim()
    $toldFile = Join-Path $local 'Told about the hub.txt'
    if (((Get-Date) - $since).TotalMinutes -ge 30 -and -not ((Test-Path $toldFile) -and (Get-Item $toldFile).LastWriteTime -gt $since)) {
        # A Windows notice, since phone alerts come from the hub.
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
        $xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
        $text = $xml.GetElementsByTagName('text')
        $text.Item(0).AppendChild($xml.CreateTextNode('The hub is not answering')) | Out-Null
        $text.Item(1).AppendChild($xml.CreateTextNode("Home watch hasn't reached it since $($since.ToString('h:mm tt')). Phone alerts and Home Assistant may be down.")) | Out-Null
        $app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
        Set-Content $toldFile (Get-Date -Format o) -Encoding UTF8
    }
}
