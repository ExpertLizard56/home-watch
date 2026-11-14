package io.github.expertlizard56.homewatch.tv;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Starts the 5-minute reports after the box boots and after the app is installed or updated. */
public class Start extends BroadcastReceiver {

    @Override
    public void onReceive(Context context, Intent intent) {
        Alarm.schedule(context);
        Alarm.run(context.getApplicationContext(), null);
    }
}
