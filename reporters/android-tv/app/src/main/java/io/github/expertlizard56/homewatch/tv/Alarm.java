package io.github.expertlizard56.homewatch.tv;

import android.app.AlarmManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.PowerManager;
import android.os.SystemClock;

/**
 * Every 5 minutes, even while the box sleeps: a wake-up alarm (allowed in deep sleep), then the report. An alarm in
 * deep sleep gets about 10 seconds of network, which the report's first shell call stretches to a minute.
 */
public class Alarm extends BroadcastReceiver {

    private static final long EVERY_MS = 5 * 60 * 1000;

    /** Sets the next report, replacing any already set. */
    static void schedule(Context context) {
        AlarmManager alarms = context.getSystemService(AlarmManager.class);
        PendingIntent next = PendingIntent.getBroadcast(context, 0, new Intent(context, Alarm.class),
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        long when = SystemClock.elapsedRealtime() + EVERY_MS;
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S || alarms.canScheduleExactAlarms()) {
            alarms.setExactAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, when, next);
        } else {
            alarms.setAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, when, next);
        }
    }

    @Override
    public void onReceive(Context context, Intent intent) {
        schedule(context);
        run(context.getApplicationContext(), null);
    }

    /** Sends a report now, in the background; then runs done (on that thread), if given. */
    static void run(Context context, Runnable done) {
        PowerManager.WakeLock awake = context.getSystemService(PowerManager.class)
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "homewatch:report");
        awake.acquire(60 * 1000);
        new Thread(() -> {
            try {
                Report.send(context);
            } finally {
                if (awake.isHeld()) awake.release();
                if (done != null) done.run();
            }
        }, "report").start();
    }
}
