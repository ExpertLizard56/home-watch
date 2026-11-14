package io.github.expertlizard56.homewatch.tv;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import java.util.LinkedHashMap;
import java.util.Map;
import org.junit.Test;

public class ReportTest {

    @Test
    public void slug() {
        assertEquals("living-room-tv-3", Settings.slug("  Living Room TV 3 "));
        assertEquals("onn-4k-pro", Settings.slug("Onn 4K Pro!"));
        assertEquals("tv-box", Settings.slug("***"));
    }

    @Test
    public void reportUrl() {
        assertEquals("", Settings.reportUrl(" "));
        assertEquals("http://192.168.1.50:8790/report", Settings.reportUrl("192.168.1.50:8790"));
        assertEquals("http://hub:8790/report", Settings.reportUrl("http://hub:8790/"));
        assertEquals("https://hub/report", Settings.reportUrl("https://hub/report"));
    }

    @Test
    public void jsonStrings() {
        assertEquals("\"a\\\"b\\\\c\\nd\\u0001\"", Json.str("a\"b\\c\nd\u0001"));
        Map<String, String> m = new LinkedHashMap<>();
        m.put("x", "1");
        m.put("y", "two");
        assertEquals("{\"x\":\"1\",\"y\":\"two\"}", Json.strings(m));
        assertEquals("{}", Json.strings(new LinkedHashMap<>()));
    }

    @Test
    public void cpuTicks() {
        assertArrayEquals(new long[] {28, 9}, Report.cpuTicks("cpu  1 2 3 4 5 6 7"));
        assertNull(Report.cpuTicks("cpu0 1 2 3 4 5"));
    }

    @Test
    public void boxKeeperNotes() {
        Map<String, String> status = new LinkedHashMap<>();
        status.put("version", "0.1.3");
        status.put("lastTidy", "Today 3:00 AM");
        status.put("tvRemote", "Roku TV");
        status.put("tvRemoteProblem", "The TV doesn't answer.");
        Map<String, String> notes = new LinkedHashMap<>();
        Map<String, String> problems = new LinkedHashMap<>();
        Report.boxKeeper(status, notes, problems);
        assertEquals("0.1.3", notes.get("Box Keeper"));
        assertEquals("Today 3:00 AM", notes.get("Last cleanup"));
        assertEquals("Roku TV", notes.get("TV remote"));
        assertEquals("The TV doesn't answer.", problems.get("tv-remote"));

        notes.clear();
        problems.clear();
        status.put("tvRemoteProblem", "");
        Report.boxKeeper(status, notes, problems);
        assertTrue(problems.isEmpty());
    }

    @Test
    public void body() {
        Map<String, Number> metrics = new LinkedHashMap<>();
        metrics.put("mem", 41.5);
        String json = Report.body("bedroom", "Bedroom", new long[] {10, 4}, metrics, new LinkedHashMap<>(),
                new LinkedHashMap<>(), new LinkedHashMap<>());
        assertEquals("{\"machine\":\"bedroom\",\"name\":\"Bedroom\",\"kind\":\"android\",\"cpu_ticks\":[10,4],"
                + "\"metrics\":{\"mem\":41.5},\"counters\":{},\"problems\":{},\"notes\":{}}", json);
    }
}
