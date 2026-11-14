package io.github.expertlizard56.homewatch.tv;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/**
 * The same settings as the app's screen, from a computer, so a box can be set up without its remote:
 * `adb shell am broadcast -a io.github.expertlizard56.homewatch.tv.SET -n io.github.expertlizard56.homewatch.tv/.Setup
 * --es hub http://homeassistant.local:8790 --es machine living-room-box --es name "Living room box"` (each optional).
 * Only the system shell may send it (DUMP). It sends a report straight after.
 */
public class Setup extends BroadcastReceiver {

    @Override
    public void onReceive(Context context, Intent intent) {
        Settings.save(context, intent.getStringExtra("hub"), intent.getStringExtra("machine"), intent.getStringExtra("name"));
        Alarm.schedule(context);
        PendingResult pending = goAsync();
        Alarm.run(context.getApplicationContext(), pending::finish);
    }
}
