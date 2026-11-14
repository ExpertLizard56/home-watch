package io.github.expertlizard56.homewatch.tv;

import android.content.Context;
import android.content.SharedPreferences;
import java.util.Locale;

/**
 * What the person set on the app's screen (or from a computer, Setup): the hub's address, and the machine id and name
 * this box reports as. Plus the last report's time and result, for the screen. No default hub: until it's set,
 * nothing is sent.
 */
final class Settings {

    private Settings() {
    }

    static SharedPreferences prefs(Context context) {
        return context.getSharedPreferences("homewatch", Context.MODE_PRIVATE);
    }

    /** The hub as typed, e.g. "http://homeassistant.local:8790"; empty until set. */
    static String hub(Context context) {
        return prefs(context).getString("hub", "");
    }

    /** The machine id (letters, digits and dashes), by default from the box's own name. */
    static String machine(Context context) {
        String m = prefs(context).getString("machine", "");
        return m.isEmpty() ? slug(boxName(context)) : m;
    }

    /** The name the hub shows, by default the box's own name. */
    static String name(Context context) {
        String n = prefs(context).getString("name", "");
        return n.isEmpty() ? boxName(context) : n;
    }

    static void save(Context context, String hub, String machine, String name) {
        SharedPreferences.Editor e = prefs(context).edit();
        if (hub != null) e.putString("hub", hub.trim());
        if (machine != null) e.putString("machine", slug(machine));
        if (name != null) e.putString("name", name.trim());
        e.apply();
    }

    /** The box's own name (Settings > System > About > Device name), or "TV box". */
    static String boxName(Context context) {
        String n = android.provider.Settings.Global.getString(context.getContentResolver(), "device_name");
        return n == null || n.trim().isEmpty() ? "TV box" : n.trim();
    }

    /** "Living Room TV 3" to "living-room-tv-3": lower case, anything else between words a dash. */
    static String slug(String text) {
        String s = text.trim().toLowerCase(Locale.ROOT).replaceAll("[^a-z0-9]+", "-").replaceAll("^-+|-+$", "");
        return s.isEmpty() ? "tv-box" : s;
    }

    /** The report's address: the hub with /report added unless it's there already. */
    static String reportUrl(String hub) {
        String h = hub.trim();
        if (h.isEmpty()) return "";
        if (!h.contains("://")) h = "http://" + h;
        while (h.endsWith("/")) h = h.substring(0, h.length() - 1);
        return h.endsWith("/report") ? h : h + "/report";
    }
}
