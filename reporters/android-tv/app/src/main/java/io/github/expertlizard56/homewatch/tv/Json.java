package io.github.expertlizard56.homewatch.tv;

import java.util.Locale;
import java.util.Map;

/** Writing the report's JSON by hand (no library for one small object). */
final class Json {

    private Json() {
    }

    /** A string as a JSON value, quotes included: backslash, quote and control characters escaped. */
    static String str(String text) {
        StringBuilder out = new StringBuilder(text.length() + 8).append('"');
        for (int i = 0; i < text.length(); i++) {
            char c = text.charAt(i);
            switch (c) {
                case '"': out.append("\\\""); break;
                case '\\': out.append("\\\\"); break;
                case '\n': out.append("\\n"); break;
                case '\r': out.append("\\r"); break;
                case '\t': out.append("\\t"); break;
                default:
                    if (c < 0x20) out.append(String.format(Locale.ROOT, "\\u%04x", (int) c));
                    else out.append(c);
                    break;
            }
        }
        return out.append('"').toString();
    }

    /** {"a":"x","b":"y"} from text values. */
    static String strings(Map<String, String> values) {
        StringBuilder out = new StringBuilder("{");
        for (Map.Entry<String, String> e : values.entrySet()) {
            if (out.length() > 1) out.append(',');
            out.append(str(e.getKey())).append(':').append(str(e.getValue()));
        }
        return out.append('}').toString();
    }

    /** {"a":1.5,"b":2} from numbers. */
    static String numbers(Map<String, ? extends Number> values) {
        StringBuilder out = new StringBuilder("{");
        for (Map.Entry<String, ? extends Number> e : values.entrySet()) {
            if (out.length() > 1) out.append(',');
            out.append(str(e.getKey())).append(':').append(e.getValue());
        }
        return out.append('}').toString();
    }
}
