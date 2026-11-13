# Home watch reporter for a Windows PC. A scheduled task (e.g. "Home watch report") runs it every 5 minutes, hidden,
# through conhost --headless. It sends raw counters to the hub, which works out amounts from the change between
# reports (Beszel's way, MIT; see addon/watch.py), plus the drive checks: drive health, low space and disk errors in
# Windows' log. If the hub stops taking reports for 30 minutes, it shows a Windows notice (phone alerts come from it).
# -Dry prints the report instead of sending it.
# The hub's address comes from -Hub (or HOMEWATCH_HUB): there is no default, so it says so and stops if it is missing.

param([switch]$Dry, [string]$Hub)

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

# Drive wear and temperature from Windows' own counters (only readable when the task runs with highest privileges).
try {
    $rel = Get-PhysicalDisk | Get-StorageReliabilityCounter -ErrorAction Stop
    $temps = @($rel | Where-Object Temperature -gt 0 | ForEach-Object Temperature)
    if ($temps) { $metrics.drive_temp = ($temps | Measure-Object -Maximum).Maximum }
    foreach ($r in $rel) {
        if ($r.Wear -ge 90) { $problems["wear-$($r.DeviceId)"] = "Drive $($r.DeviceId) says $($r.Wear)% of its life is used" }
    }
} catch { }

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
