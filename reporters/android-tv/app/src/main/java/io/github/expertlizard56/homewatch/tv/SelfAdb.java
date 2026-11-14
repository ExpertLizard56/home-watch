package io.github.expertlizard56.homewatch.tv;

import android.content.Context;
import dadb.AdbKeyPair;
import dadb.Dadb;
import java.io.File;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

/**
 * The box's own ADB (network debugging, 127.0.0.1:5555), for what only the system may read: the CPU counters and the
 * temperature, and a minute of network while the box is in deep sleep. Needs network debugging on in the box's
 * developer options, and the box asks once on its screen ("Allow USB debugging?", Always allow) for this app's key
 * (in its files). Until that's answered a connection waits for ever (dadb has no timeout), so every call gives up
 * after 15 seconds. The same as StreamBox Keeper's, cut down.
 */
final class SelfAdb {

    private static final long SECONDS = 15;
    private static final ExecutorService CALLS = Executors.newCachedThreadPool(r -> {
        Thread t = new Thread(r, "self-adb");
        t.setDaemon(true);
        return t;
    });

    private SelfAdb() {
    }

    /** Runs a shell command as the system shell and returns its output; throws if ADB isn't on, isn't allowed or hangs. */
    static String shell(Context context, String command) throws Exception {
        Future<String> result = CALLS.submit(() -> {
            try (Dadb self = open(context)) {
                return self.shell(command).getAllOutput();
            }
        });
        try {
            return result.get(SECONDS, TimeUnit.SECONDS);
        } finally {
            result.cancel(true);
        }
    }

    /**
     * True if this app may use the box's ADB now. The first time, the box asks on its screen and this waits up to 15 s
     * for the answer, so only call it when the person asked for it. Blocks.
     */
    static boolean allowed(Context context) {
        boolean ok;
        try {
            ok = shell(context, "echo ok").trim().endsWith("ok");
        } catch (Exception e) {
            ok = false;
        }
        Settings.prefs(context).edit().putBoolean("systemAccess", ok).apply();
        return ok;
    }

    /** Whether the box allowed this app last time it was asked, without connecting. */
    static boolean knownAllowed(Context context) {
        return Settings.prefs(context).getBoolean("systemAccess", false);
    }

    private static Dadb open(Context context) throws Exception {
        File priv = new File(context.getFilesDir(), "adbkey");
        File pub = new File(context.getFilesDir(), "adbkey.pub");
        if (!priv.exists() || !pub.exists()) AdbKeyPair.generate(priv, pub);
        return Dadb.create("127.0.0.1", 5555, AdbKeyPair.read(priv, pub));
    }
}
