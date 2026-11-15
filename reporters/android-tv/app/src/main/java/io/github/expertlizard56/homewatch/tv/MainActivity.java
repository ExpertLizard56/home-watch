package io.github.expertlizard56.homewatch.tv;

import android.app.Activity;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.StateListDrawable;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.text.format.DateUtils;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

/**
 * The app's one screen: where to send the reports (the hub), what this box is called there, system access, and how
 * the last report went. Everything the app does is set here; Setup does the same from a computer.
 */
public class MainActivity extends Activity {

    private final Handler main = new Handler(Looper.getMainLooper());
    private EditText hub, machine, name;
    private TextView access, last;

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        LinearLayout page = new LinearLayout(this);
        page.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(32);
        page.setPadding(pad * 2, pad, pad * 2, pad);
        page.setBackgroundColor(Color.rgb(18, 22, 28));

        page.addView(text("Home Watch Reporter", 30, Color.WHITE));
        page.addView(text("Sends this box's health (CPU, memory, temperature, storage, network) to your Home Watch hub "
                + "every 5 minutes, even while it sleeps.", 16, Color.LTGRAY));

        page.addView(label("Hub address (e.g. http://homeassistant.local:8790)"));
        hub = field(Settings.hub(this), InputType.TYPE_TEXT_VARIATION_URI);
        page.addView(hub);
        page.addView(label("Shown on the hub as"));
        name = field(Settings.name(this), InputType.TYPE_TEXT_FLAG_CAP_WORDS);
        page.addView(name);
        page.addView(label("Machine id (letters, digits and dashes; keeps its history on the hub)"));
        machine = field(Settings.machine(this), InputType.TYPE_TEXT_VARIATION_NORMAL);
        page.addView(machine);

        LinearLayout buttons = new LinearLayout(this);
        buttons.setPadding(0, dp(16), 0, 0);
        Button save = button("Save and send now", v -> {
            Settings.save(this, hub.getText().toString(), machine.getText().toString(), name.getText().toString());
            machine.setText(Settings.machine(this));
            Alarm.schedule(this);
            send();
        });
        buttons.addView(save);
        buttons.addView(button("Allow system access", v -> {
            access.setText("Asking the box... answer \"Allow USB debugging?\" with Always allow.");
            new Thread(() -> {
                boolean ok = SelfAdb.allowed(getApplicationContext());
                main.post(this::showAccess);
                if (ok) send();
            }).start();
        }));
        page.addView(buttons);

        page.addView(label("System access"));
        access = text("", 16, Color.WHITE);
        page.addView(access);
        page.addView(label("Last report"));
        last = text("", 16, Color.WHITE);
        page.addView(last);

        ScrollView scroll = new ScrollView(this);
        scroll.addView(page);
        setContentView(scroll);
        scroll.setDescendantFocusability(ViewGroup.FOCUS_AFTER_DESCENDANTS);
        scroll.post(save::requestFocus);
        Alarm.schedule(this);
    }

    @Override
    protected void onResume() {
        super.onResume();
        showAccess();
        showLast();
    }

    private void send() {
        last.setText("Sending...");
        Alarm.run(getApplicationContext(), () -> main.post(this::showLast));
    }

    private void showAccess() {
        access.setText(SelfAdb.knownAllowed(this)
                ? "Allowed: CPU and temperature are in the reports."
                : "Not allowed: reports go without CPU and temperature, and may miss while the box sleeps. Needs "
                        + "network debugging on (Settings > System > Developer options).");
    }

    private void showLast() {
        long at = Settings.prefs(this).getLong("lastAt", 0);
        String result = Settings.prefs(this).getString("lastResult", "");
        long now = System.currentTimeMillis();
        last.setText(at == 0 ? "None yet." : result + " (" + (now - at < DateUtils.MINUTE_IN_MILLIS ? "just now"
                : DateUtils.getRelativeTimeSpanString(at, now, DateUtils.MINUTE_IN_MILLIS)) + ")");
    }

    private TextView text(String s, int sp, int color) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(sp);
        t.setTextColor(color);
        t.setPadding(0, dp(4), 0, dp(4));
        return t;
    }

    private TextView label(String s) {
        TextView t = text(s, 14, Color.rgb(140, 170, 200));
        t.setPadding(0, dp(14), 0, 0);
        return t;
    }

    private EditText field(String value, int variation) {
        EditText e = new EditText(this);
        e.setText(value);
        e.setSingleLine(true);
        e.setInputType(InputType.TYPE_CLASS_TEXT | variation);
        e.setTextColor(Color.WHITE);
        e.setTextSize(18);
        e.setLayoutParams(new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT));
        return e;
    }

    private Button button(String s, View.OnClickListener click) {
        Button b = new Button(this);
        b.setText(s);
        b.setAllCaps(false);
        b.setGravity(Gravity.CENTER);
        b.setTextColor(Color.WHITE);
        b.setPadding(dp(20), dp(10), dp(20), dp(10));
        StateListDrawable look = new StateListDrawable();
        look.addState(new int[] {android.R.attr.state_focused}, rounded(Color.rgb(64, 140, 220)));
        look.addState(new int[] {}, rounded(Color.rgb(52, 60, 72)));
        b.setBackground(look);
        b.setOnClickListener(click);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.setMarginEnd(dp(16));
        b.setLayoutParams(lp);
        return b;
    }

    private GradientDrawable rounded(int color) {
        GradientDrawable d = new GradientDrawable();
        d.setColor(color);
        d.setCornerRadius(dp(8));
        return d;
    }

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }
}
