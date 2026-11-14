package io.github.expertlizard56.homewatch.tv;

import android.content.Context;
import android.database.Cursor;
import android.net.Uri;
import android.os.Environment;
import android.os.StatFs;
import android.os.SystemClock;
import android.util.Log;
import java.io.File;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * The box's report to Home Watch, like the other reporters (README, Reporters): CPU ticks, memory, swap, temperature,
 * storage, network bytes and uptime, as raw counters the hub works the amounts out from. The CPU counters and the
 * temperature need the system shell (SelfAdb), and the same call lets the app use the network for a minute while the
 * box is in deep sleep; until system access is allowed on the screen, the report goes without those. When StreamBox Keeper is
 * installed, what its screen shows goes in the notes (its version, last cleanup, restart and app update, the TV
 * remote), and a TV remote problem goes in the problems.
 */
final class Report {

    static final String TAG = "HomeWatch";
    private static final Uri BOX_KEEPER = Uri.parse("content://io.github.expertlizard56.streamingboxkeeper.status");
    /** StreamBox Keeper's status names, and the note label each one gets. */
    private static final String[][] BOX_KEEPER_NOTES = {{"version", "Box Keeper"}, {"lastTidy", "Last cleanup"},
            {"lastRestart", "Last restart"}, {"lastAppUpdate", "Last app update"}, {"tvRemote", "TV remote"}};

    private Report() {
    }

    /** Builds and sends the report; returns what happened, in words, and saves it for the screen. Blocks. */
    static String send(Context context) {
        String url = Settings.reportUrl(Settings.hub(context));
        String result;
        if (url.isEmpty()) {
            result = "Not sent: set the hub's address.";
        } else {
            try {
                int code = post(url, build(context));
                result = code == 200 ? "Sent." : "The hub answered " + code + ".";
            } catch (Exception e) {
                result = "Not sent: " + e.getMessage();
            }
        }
        Log.i(TAG, result);
        Settings.prefs(context).edit().putLong("lastAt", System.currentTimeMillis()).putString("lastResult", result).apply();
        return result;
    }

    static String build(Context context) throws Exception {
        Map<String, Number> metrics = new LinkedHashMap<>();
        long[] ticks = null;
        // Only once allowed from the screen: unasked, the box's question would come up behind whatever is on, and
        // while it waits there the button's own question can't come up.
        if (SelfAdb.knownAllowed(context)) try {
            String out = SelfAdb.shell(context, "cmd deviceidle tempwhitelist -d 60000 " + context.getPackageName()
                    + " >/dev/null 2>&1; head -1 /proc/stat; dumpsys thermalservice | grep -m1 -o 'mValue=[0-9.]*'");
            for (String line : out.split("\n")) {
                line = line.trim();
                if (line.startsWith("cpu ")) ticks = cpuTicks(line);
                else if (line.startsWith("mValue=")) metrics.put("temp", Double.parseDouble(line.substring(7)));
            }
        } catch (Exception e) {
            Log.w(TAG, "report without CPU and temperature: " + e);
        }
        long memTotal = 0, memAvail = 0, swapTotal = 0, swapFree = 0;
        for (String line : Files.readAllLines(new File("/proc/meminfo").toPath())) {
            String[] f = line.split("\\s+");
            if (f.length < 2) continue;
            long kb = Long.parseLong(f[1]);
            switch (f[0]) {
                case "MemTotal:": memTotal = kb; break;
                case "MemAvailable:": memAvail = kb; break;
                case "SwapTotal:": swapTotal = kb; break;
                case "SwapFree:": swapFree = kb; break;
                default: break;
            }
        }
        if (memTotal > 0) metrics.put("mem", Math.round(1000.0 * (memTotal - memAvail) / memTotal) / 10.0);
        if (swapTotal > 0) metrics.put("swap", Math.round(1000.0 * (swapTotal - swapFree) / swapTotal) / 10.0);
        StatFs data = new StatFs(Environment.getDataDirectory().getPath());
        double free = data.getAvailableBytes(), size = data.getTotalBytes();
        metrics.put("disk_used_pct", Math.round(1000 * (1 - free / size)) / 10.0);
        metrics.put("disk_free_gb", Math.round(free / 1e8) / 10.0);
        metrics.put("uptime_h", Math.round(SystemClock.elapsedRealtime() / 360000.0) / 10.0);
        Map<String, Number> counters = new LinkedHashMap<>();
        counters.put("net_rx_bytes", android.net.TrafficStats.getTotalRxBytes());
        counters.put("net_tx_bytes", android.net.TrafficStats.getTotalTxBytes());
        Map<String, String> notes = new LinkedHashMap<>();
        Map<String, String> problems = new LinkedHashMap<>();
        boxKeeper(boxKeeperStatus(context), notes, problems);
        return body(Settings.machine(context), Settings.name(context), ticks, metrics, counters, problems, notes);
    }

    /** The report's JSON. */
    static String body(String machine, String name, long[] ticks, Map<String, Number> metrics,
            Map<String, Number> counters, Map<String, String> problems, Map<String, String> notes) {
        return "{\"machine\":" + Json.str(machine) + ",\"name\":" + Json.str(name) + ",\"kind\":\"android\""
                + (ticks == null ? "" : ",\"cpu_ticks\":[" + ticks[0] + "," + ticks[1] + "]")
                + ",\"metrics\":" + Json.numbers(metrics)
                + ",\"counters\":" + Json.numbers(counters)
                + ",\"problems\":" + Json.strings(problems)
                + ",\"notes\":" + Json.strings(notes) + "}";
    }

    /** /proc/stat's first line to {all ticks, idle + iowait ticks}; null if it isn't one. */
    static long[] cpuTicks(String line) {
        String[] f = line.trim().split("\\s+");
        if (f.length < 6 || !"cpu".equals(f[0])) return null;
        long total = 0;
        for (int i = 1; i < f.length; i++) total += Long.parseLong(f[i]);
        return new long[] {total, Long.parseLong(f[4]) + Long.parseLong(f[5])};
    }

    /** StreamBox Keeper's status rows into notes, and its TV remote problem (if any) into problems. */
    static void boxKeeper(Map<String, String> status, Map<String, String> notes, Map<String, String> problems) {
        if (status.isEmpty()) return;
        for (String[] n : BOX_KEEPER_NOTES) {
            String value = status.get(n[0]);
            if (value != null && !value.isEmpty()) notes.put(n[1], value);
        }
        String problem = status.get("tvRemoteProblem");
        if (problem != null && !problem.isEmpty()) problems.put("tv-remote", problem);
    }

    /** StreamBox Keeper's status (its StatusProvider), name to value; empty if it isn't installed or says nothing. */
    static Map<String, String> boxKeeperStatus(Context context) {
        Map<String, String> status = new LinkedHashMap<>();
        try (Cursor rows = context.getContentResolver().query(BOX_KEEPER, null, null, null, null)) {
            while (rows != null && rows.moveToNext()) status.put(rows.getString(0), rows.getString(1));
        } catch (Exception e) {
            Log.i(TAG, "no StreamBox Keeper status: " + e.getMessage());
        }
        return status;
    }

    private static int post(String url, String body) throws Exception {
        HttpURLConnection http = (HttpURLConnection) new URL(url).openConnection();
        http.setConnectTimeout(10000);
        http.setReadTimeout(10000);
        http.setDoOutput(true);
        http.setRequestProperty("Content-Type", "application/json");
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        http.setFixedLengthStreamingMode(bytes.length);
        try (OutputStream out = http.getOutputStream()) {
            out.write(bytes);
        }
        int code = http.getResponseCode();
        http.disconnect();
        return code;
    }
}
