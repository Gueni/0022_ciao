package com.ciao.tabletpen;

import android.content.Context;
import android.net.wifi.WifiManager;

import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * USB + Wi-Fi: a TCP server on 8767 that the PC connects to (directly over Wi-Fi, or through
 * `adb forward` over USB), and a UDP responder on 8766 so the PC can find the tablet on the network.
 */
class Net {
    static final int TCP_PORT = 8767, UDP_PORT = 8766;

    private final Hub hub;
    private final String announce;
    private final WifiManager.MulticastLock mlock;
    private volatile boolean running;
    private ServerSocket server;
    private DatagramSocket udp;

    Net(Context ctx, Hub hub, String tabletId, String tabletName) {
        this.hub = hub;
        String a;
        try {
            a = new JSONObject().put("port", TCP_PORT).put("id", tabletId).put("name", tabletName).toString();
        } catch (Exception e) {
            a = "{}";
        }
        announce = "TABLETPEN!" + a;
        WifiManager wm = (WifiManager) ctx.getApplicationContext().getSystemService(Context.WIFI_SERVICE);
        mlock = wm != null ? wm.createMulticastLock("tabletpen") : null;
        // keep the Wi-Fi radio awake between packets (power-save naps add 100–300 ms to every tap)
        WifiManager.WifiLock w = null;
        try {
            if (wm != null) w = wm.createWifiLock(android.os.Build.VERSION.SDK_INT >= 29
                    ? WifiManager.WIFI_MODE_FULL_LOW_LATENCY : WifiManager.WIFI_MODE_FULL_HIGH_PERF, "tabletpen");
        } catch (Exception ignored) {}
        wlock = w;
    }

    private final WifiManager.WifiLock wlock;

    /** Low-latency Wi-Fi only while the app is on screen (it costs battery). */
    void foreground(boolean on) {
        try {
            if (wlock == null) return;
            if (on && running && !wlock.isHeld()) wlock.acquire();
            if (!on && wlock.isHeld()) wlock.release();
        } catch (Exception ignored) {}
    }

    void start() {
        if (running) return;
        running = true;
        if (mlock != null) mlock.acquire();
        try { if (wlock != null) wlock.acquire(); } catch (Exception ignored) {}
        new Thread(this::serve, "tp-tcp").start();
        new Thread(this::discovery, "tp-udp").start();
    }

    void stop() {
        running = false;
        try { if (server != null) server.close(); } catch (Exception ignored) {}
        try { if (udp != null) udp.close(); } catch (Exception ignored) {}
        if (mlock != null && mlock.isHeld()) mlock.release();
        try { if (wlock != null && wlock.isHeld()) wlock.release(); } catch (Exception ignored) {}
    }

    private void serve() {
        while (running) {
            try {
                server = new ServerSocket();
                server.setReuseAddress(true);
                server.bind(new InetSocketAddress(TCP_PORT));
                while (running) {
                    Socket s = server.accept();
                    s.setTcpNoDelay(true);
                    String kind = s.getInetAddress().isLoopbackAddress() ? "usb" : "wifi";
                    new TcpLink(kind, s).start();
                }
            } catch (Exception e) {
                try { if (server != null) server.close(); } catch (Exception ignored) {}
                sleep(1000);
            }
        }
    }

    private void discovery() {
        byte[] reply = announce.getBytes(StandardCharsets.UTF_8);
        byte[] buf = new byte[512];
        while (running) {
            try {
                udp = new DatagramSocket(null);
                udp.setReuseAddress(true);
                udp.setBroadcast(true);
                udp.bind(new InetSocketAddress(UDP_PORT));
                while (running) {
                    DatagramPacket p = new DatagramPacket(buf, buf.length);
                    udp.receive(p);
                    String msg = new String(p.getData(), 0, p.getLength(), StandardCharsets.UTF_8);
                    if (msg.startsWith("TABLETPEN?"))
                        udp.send(new DatagramPacket(reply, reply.length, p.getSocketAddress()));
                }
            } catch (Exception e) {
                try { if (udp != null) udp.close(); } catch (Exception ignored) {}
                sleep(1000);
            }
        }
    }

    private static void sleep(long ms) {
        try { Thread.sleep(ms); } catch (InterruptedException ignored) {}
    }

    /** Framing: [int32 big-endian length][packet] in both directions. */
    private class TcpLink extends Link {
        private final Socket sock;
        private final ExecutorService writer = Executors.newSingleThreadExecutor();
        private DataOutputStream out;
        private volatile boolean closed, announced;

        TcpLink(String kind, Socket s) {
            super(kind);
            sock = s;
        }

        void start() {
            new Thread(() -> {
                try {
                    out = new DataOutputStream(new BufferedOutputStream(sock.getOutputStream()));
                    DataInputStream in = new DataInputStream(new BufferedInputStream(sock.getInputStream()));
                    while (!closed) {
                        int n = in.readInt();
                        if (n < 0 || n > (4 << 20)) break;
                        byte[] b = new byte[n];
                        in.readFully(b);
                        if (!announced) {          // only real PCs (not stray connections) show up
                            announced = true;
                            hub.up(this);
                        }
                        hub.data(this, b);
                    }
                } catch (Exception ignored) {
                } finally {
                    close();
                }
            }, "tp-link").start();
        }

        @Override
        void send(byte[] p) {
            if (closed) return;
            writer.execute(() -> {
                try {
                    out.writeInt(p.length);
                    out.write(p);
                    out.flush();
                } catch (Exception e) {
                    close();
                }
            });
        }

        @Override
        void close() {
            if (closed) return;
            closed = true;
            try { sock.close(); } catch (Exception ignored) {}
            writer.shutdownNow();
            if (announced) hub.down(this);
        }
    }
}
