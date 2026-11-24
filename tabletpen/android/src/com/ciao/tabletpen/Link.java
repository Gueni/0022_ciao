package com.ciao.tabletpen;

/** One connection to a PC: "usb", "wifi" or "ble". */
abstract class Link {
    private static int counter;

    final String id;
    final String kind;
    volatile String pc = "";
    volatile String auth = "new";        // new | pending | ok | denied
    volatile String lastMsg;             // last 0x10 JSON from the PC (screen size etc.)
    volatile boolean active;

    Link(String kind) {
        this.kind = kind;
        synchronized (Link.class) { id = kind + (++counter); }
    }

    int priority() {
        return "usb".equals(kind) ? 3 : "wifi".equals(kind) ? 2 : 1;
    }

    abstract void send(byte[] packet);

    abstract void close();
}
