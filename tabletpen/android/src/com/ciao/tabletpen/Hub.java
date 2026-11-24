package com.ciao.tabletpen;

import org.json.JSONArray;
import org.json.JSONObject;

import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * Keeps every live connection to a PC and picks the one to use: USB, then Wi-Fi, then Bluetooth
 * (or whichever the user tapped). Pen packets from the page go to that link only.
 */
class Hub {
    interface Js { void toJs(String type, String data); }

    private final Js js;
    private final byte[] hello;
    private final List<Link> links = new ArrayList<>();
    private Link active, pinned;
    private final java.util.LinkedHashMap<Integer, byte[]> frames = new java.util.LinkedHashMap<>();
    private int frameSeq;

    Hub(Js js, String tabletId, String tabletName) {
        this.js = js;
        String h;
        try {
            h = new JSONObject().put("id", tabletId).put("name", tabletName).toString();
        } catch (Exception e) {
            h = "{}";
        }
        byte[] b = h.getBytes(StandardCharsets.UTF_8);
        hello = new byte[b.length + 1];
        hello[0] = 0x06;
        System.arraycopy(b, 0, hello, 1, b.length);
        // heartbeat on every link so the PC notices quickly when the app goes away
        Thread hb = new Thread(() -> {
            byte[] ping = {0x07};
            while (true) {
                try { Thread.sleep(2000); } catch (InterruptedException e) { return; }
                List<Link> copy;
                synchronized (this) { copy = new ArrayList<>(links); }
                for (Link l : copy) l.send(ping);
            }
        }, "tp-heartbeat");
        hb.setDaemon(true);
        hb.start();
    }


    void up(Link l) {
        synchronized (this) { links.add(l); }
        l.send(hello);
        publish();
    }

    void down(Link l) {
        synchronized (this) {
            links.remove(l);
            if (pinned == l) pinned = null;
            if (active == l) active = null;
            pick();
        }
        publish();
    }

    void select(String id) {
        synchronized (this) {
            for (Link l : links) if (l.id.equals(id)) pinned = l;
            pick();
        }
        publish();
    }

    /** Called with the lock held. */
    private void pick() {
        Link best = null;
        if (pinned != null && "ok".equals(pinned.auth)) {
            best = pinned;
        } else {
            for (Link l : links)
                if ("ok".equals(l.auth) && (best == null || l.priority() > best.priority())) best = l;
        }
        if (best == active) return;
        if (active != null) {
            active.active = false;
            active.send(new byte[]{0x05, 0});
        }
        active = best;
        synchronized (frames) { frames.clear(); }
        if (best != null) {
            best.active = true;
            best.send(new byte[]{0x05, 1});
            if (best.lastMsg != null) js.toJs("msg", best.lastMsg);
        }
    }

    void data(Link l, byte[] v) {
        if (v == null || v.length == 0) return;
        if (v[0] == 0x10) {
            String s = new String(v, 1, v.length - 1, StandardCharsets.UTF_8);
            try {
                JSONObject o = new JSONObject(s);
                if (o.has("toast")) {                 // a one-off message for the page (e.g. "screenshot saved")
                    if (l == active) js.toJs("msg", s);
                    return;
                }
                String auth = o.optString("auth", "");
                if ("probe".equals(auth)) return;
                l.pc = o.optString("pc", l.pc);
                if (!auth.isEmpty()) l.auth = auth;
                l.lastMsg = s;
            } catch (Exception e) {
                return;
            }
            boolean isActive;
            synchronized (this) { pick(); isActive = l == active; }
            if (isActive) js.toJs("msg", s);
            publish();
        } else if (v[0] == 0x22) {
            frame(l, Arrays.copyOfRange(v, 1, v.length));
        }
    }

    /** A preview patch: [W, H, x, y, w, h] (u16 LE) + JPEG/WebP. The page fetches it from /frame?seq. */
    void frame(Link l, byte[] f) {
        if (l != active || f.length <= 12) return;
        int[] h = new int[6];
        for (int i = 0; i < 6; i++) h[i] = (f[2 * i] & 0xFF) | (f[2 * i + 1] & 0xFF) << 8;
        int seq;
        synchronized (frames) {
            seq = ++frameSeq;
            frames.put(seq, Arrays.copyOfRange(f, 12, f.length));
            while (frames.size() > 32) frames.remove(frames.keySet().iterator().next());
        }
        js.toJs("frame", seq + "," + h[0] + "," + h[1] + "," + h[2] + "," + h[3] + "," + h[4] + "," + h[5]);
    }

    byte[] frameBytes(int seq) {
        synchronized (frames) { return frames.get(seq); }
    }

    void send(byte[] p) {
        Link a = active;
        if (a != null) a.send(p);
    }

    void publish() {
        JSONArray arr = new JSONArray();
        synchronized (this) {
            for (Link l : links) {
                try {
                    arr.put(new JSONObject().put("id", l.id).put("kind", l.kind).put("pc", l.pc)
                            .put("auth", l.auth).put("active", l == active));
                } catch (Exception ignored) {}
            }
        }
        js.toJs("links", arr.toString());
    }

    void closeAll() {
        List<Link> copy;
        synchronized (this) { copy = new ArrayList<>(links); }
        for (Link l : copy) l.close();
    }
}
