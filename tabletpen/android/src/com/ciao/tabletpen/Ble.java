package com.ciao.tabletpen;

import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothGatt;
import android.bluetooth.BluetoothGattCharacteristic;
import android.bluetooth.BluetoothGattDescriptor;
import android.bluetooth.BluetoothGattServer;
import android.bluetooth.BluetoothGattServerCallback;
import android.bluetooth.BluetoothGattService;
import android.bluetooth.BluetoothManager;
import android.bluetooth.BluetoothProfile;
import android.bluetooth.le.AdvertiseCallback;
import android.bluetooth.le.AdvertiseData;
import android.bluetooth.le.AdvertiseSettings;
import android.bluetooth.le.BluetoothLeAdvertiser;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.os.Handler;
import android.os.Looper;
import android.os.ParcelUuid;

import java.io.ByteArrayOutputStream;
import java.util.ArrayDeque;
import java.util.UUID;

/**
 * Bluetooth LE: the tablet advertises the TabletPen service and the PC connects to it.
 * Pen packets go to the PC as notifications on INPUT; the PC writes to EVENT.
 */
class Ble extends BluetoothGattServerCallback {
    static final UUID SVC = UUID.fromString("7a1b0001-5e2d-4c0f-9b7a-7ab1e7be0001");
    static final UUID INPUT = UUID.fromString("7a1b0002-5e2d-4c0f-9b7a-7ab1e7be0001");
    static final UUID EVENT = UUID.fromString("7a1b0003-5e2d-4c0f-9b7a-7ab1e7be0001");
    static final UUID CCCD = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb");

    private final Context ctx;
    private final Hub hub;
    private final Hub.Js js;
    private final Handler h = new Handler(Looper.getMainLooper());
    private final BluetoothManager bm;
    private final BluetoothAdapter ad;

    private BluetoothGattServer server;
    private BluetoothLeAdvertiser adv;
    private BluetoothGattCharacteristic input;
    private boolean running, advertising;
    private BleLink link;

    private final BroadcastReceiver btState = new BroadcastReceiver() {
        @Override
        public void onReceive(Context c, Intent i) {
            int s = i.getIntExtra(BluetoothAdapter.EXTRA_STATE, -1);
            if (s == BluetoothAdapter.STATE_ON) h.postDelayed(Ble.this::open, 500);
            else if (s == BluetoothAdapter.STATE_OFF) { shutdown(); js.toJs("bt", "off"); }
        }
    };

    Ble(Context ctx, Hub hub, Hub.Js js) {
        this.ctx = ctx;
        this.hub = hub;
        this.js = js;
        bm = (BluetoothManager) ctx.getSystemService(Context.BLUETOOTH_SERVICE);
        ad = bm != null ? bm.getAdapter() : null;
    }

    void start() {
        if (running || ad == null) return;
        running = true;
        ctx.registerReceiver(btState, new IntentFilter(BluetoothAdapter.ACTION_STATE_CHANGED));
        open();
    }

    void stop() {
        if (!running) return;
        running = false;
        try { ctx.unregisterReceiver(btState); } catch (Exception ignored) {}
        shutdown();
    }

    private void open() {
        if (!running) return;
        if (!ad.isEnabled()) { js.toJs("bt", "off"); return; }
        js.toJs("bt", "on");
        if (server != null) { startAdvertising(); return; }
        adv = ad.getBluetoothLeAdvertiser();
        if (adv == null) return;
        server = bm.openGattServer(ctx, this);
        if (server == null) return;
        BluetoothGattService s = new BluetoothGattService(SVC, BluetoothGattService.SERVICE_TYPE_PRIMARY);
        input = new BluetoothGattCharacteristic(INPUT,
                BluetoothGattCharacteristic.PROPERTY_NOTIFY | BluetoothGattCharacteristic.PROPERTY_READ,
                BluetoothGattCharacteristic.PERMISSION_READ);
        input.addDescriptor(new BluetoothGattDescriptor(CCCD,
                BluetoothGattDescriptor.PERMISSION_READ | BluetoothGattDescriptor.PERMISSION_WRITE));
        BluetoothGattCharacteristic ev = new BluetoothGattCharacteristic(EVENT,
                BluetoothGattCharacteristic.PROPERTY_WRITE | BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE,
                BluetoothGattCharacteristic.PERMISSION_WRITE);
        s.addCharacteristic(input);
        s.addCharacteristic(ev);
        server.addService(s);          // -> onServiceAdded -> advertise
    }

    private void shutdown() {
        stopAdvertising();
        if (link != null) { BleLink l = link; link = null; l.gone(); }
        if (server != null) {
            try { server.close(); } catch (Exception ignored) {}
            server = null;
        }
    }

    private void startAdvertising() {
        if (advertising || adv == null || !running || link != null) return;
        AdvertiseSettings st = new AdvertiseSettings.Builder()
                .setAdvertiseMode(AdvertiseSettings.ADVERTISE_MODE_LOW_LATENCY)
                .setTxPowerLevel(AdvertiseSettings.ADVERTISE_TX_POWER_HIGH)
                .setConnectable(true).setTimeout(0).build();
        AdvertiseData data = new AdvertiseData.Builder()
                .addServiceUuid(new ParcelUuid(SVC)).setIncludeDeviceName(false).build();
        AdvertiseData scanResp = new AdvertiseData.Builder().setIncludeDeviceName(true).build();
        try {
            adv.startAdvertising(st, data, scanResp, advCb);
            advertising = true;
        } catch (Exception ignored) {}
    }

    private void stopAdvertising() {
        if (!advertising) return;
        advertising = false;
        try { adv.stopAdvertising(advCb); } catch (Exception ignored) {}
    }

    private final AdvertiseCallback advCb = new AdvertiseCallback() {
        @Override
        public void onStartFailure(int code) {
            advertising = false;
            if (running) h.postDelayed(Ble.this::startAdvertising, 3000);
        }
    };

    // ------------------------------------------------------------ GATT server callbacks
    @Override
    public void onServiceAdded(int status, BluetoothGattService s) {
        h.post(this::startAdvertising);
    }

    @Override
    public void onConnectionStateChange(BluetoothDevice d, int status, int state) {
        h.post(() -> {
            if (state == BluetoothProfile.STATE_CONNECTED) {
                if (link == null) {
                    link = new BleLink(d);
                    stopAdvertising();
                }
            } else if (state == BluetoothProfile.STATE_DISCONNECTED && link != null && d.equals(link.dev)) {
                BleLink l = link;
                link = null;
                l.gone();
                startAdvertising();
            }
        });
    }

    @Override
    public void onMtuChanged(BluetoothDevice d, int m) {
        BleLink l = link;
        if (l != null) l.mtu = m;
    }

    @Override
    public void onDescriptorWriteRequest(BluetoothDevice d, int id, BluetoothGattDescriptor desc,
                                         boolean prepared, boolean needResp, int offset, byte[] value) {
        if (needResp) server.sendResponse(d, id, BluetoothGatt.GATT_SUCCESS, 0, value);
        BleLink l = link;
        if (l == null || !d.equals(l.dev) || !CCCD.equals(desc.getUuid())) return;
        boolean on = value != null && value.length > 0 && (value[0] & 1) != 0;
        l.subscribed(on);
    }

    @Override
    public void onDescriptorReadRequest(BluetoothDevice d, int id, int offset, BluetoothGattDescriptor desc) {
        server.sendResponse(d, id, BluetoothGatt.GATT_SUCCESS, 0, BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE);
    }

    @Override
    public void onCharacteristicReadRequest(BluetoothDevice d, int id, int offset, BluetoothGattCharacteristic c) {
        server.sendResponse(d, id, BluetoothGatt.GATT_SUCCESS, 0, new byte[0]);
    }

    @Override
    public void onCharacteristicWriteRequest(BluetoothDevice d, int id, BluetoothGattCharacteristic c,
                                             boolean prepared, boolean needResp, int offset, byte[] v) {
        if (needResp) server.sendResponse(d, id, BluetoothGatt.GATT_SUCCESS, 0, null);
        BleLink l = link;
        if (l != null && d.equals(l.dev)) l.received(v);
    }

    @Override
    public void onNotificationSent(BluetoothDevice d, int status) {
        BleLink l = link;
        if (l != null) l.sent();
    }

    // ------------------------------------------------------------ one PC connected over Bluetooth
    private class BleLink extends Link {
        final BluetoothDevice dev;
        volatile int mtu = 23;
        private boolean subscribed, sending, announced;
        private final ArrayDeque<byte[]> cmds = new ArrayDeque<>();
        private final ArrayDeque<byte[]> pen = new ArrayDeque<>();
        private byte[] retry;
        private int frameId = -1, frameGot;
        private byte[][] frame;

        BleLink(BluetoothDevice d) {
            super("ble");
            dev = d;
        }

        void subscribed(boolean on) {
            synchronized (this) { subscribed = on; sending = false; }
            if (on && !announced) {
                announced = true;
                hub.up(this);
            }
            pump();
        }

        void gone() {
            synchronized (this) { subscribed = false; cmds.clear(); pen.clear(); retry = null; }
            if (announced) { announced = false; hub.down(this); }
        }

        void received(byte[] v) {
            if (v == null || v.length == 0) return;
            if (v[0] == 0x20 && v.length > 6) {
                int fid = v[1] & 0xFF;
                int idx = (v[2] & 0xFF) | (v[3] & 0xFF) << 8;
                int total = (v[4] & 0xFF) | (v[5] & 0xFF) << 8;
                if (fid != frameId || frame == null || frame.length != total) {
                    frameId = fid;
                    frame = new byte[total][];
                    frameGot = 0;
                }
                if (idx < total && frame[idx] == null) {
                    byte[] part = new byte[v.length - 6];
                    System.arraycopy(v, 6, part, 0, part.length);
                    frame[idx] = part;
                    if (++frameGot == total) {
                        ByteArrayOutputStream out = new ByteArrayOutputStream();
                        for (byte[] p : frame) out.write(p, 0, p.length);
                        frame = null;
                        hub.frame(this, out.toByteArray());
                    }
                }
            } else {
                hub.data(this, v);
            }
        }

        @Override
        void send(byte[] pkt) {
            if (pkt.length == 0) return;
            synchronized (this) {
                if (pkt[0] == 0x01) {
                    for (int o = 1; o + 10 <= pkt.length; o += 10) {
                        byte[] rec = new byte[10];
                        System.arraycopy(pkt, o, rec, 0, 10);
                        if (rec[0] == 0 && pen.size() > 40) continue;   // drop stale hover under backlog
                        pen.add(rec);
                    }
                } else {
                    cmds.add(pkt);
                }
            }
            pump();
        }

        void sent() {
            synchronized (this) { sending = false; }
            pump();
        }

        private void pump() {
            byte[] out;
            synchronized (this) {
                if (sending || !subscribed || server == null) return;
                if (retry != null) {
                    out = retry;
                    retry = null;
                } else if (!cmds.isEmpty()) {
                    out = cmds.poll();
                } else if (!pen.isEmpty()) {
                    int max = Math.max(1, (mtu - 3 - 1) / 10);
                    int n = Math.min(max, pen.size());
                    out = new byte[1 + n * 10];
                    out[0] = 0x01;
                    for (int i = 0; i < n; i++) System.arraycopy(pen.poll(), 0, out, 1 + i * 10, 10);
                } else {
                    return;
                }
                sending = true;
            }
            boolean ok;
            try {
                synchronized (input) {
                    input.setValue(out);
                    ok = server.notifyCharacteristicChanged(dev, input, false);
                }
            } catch (Exception e) {
                ok = false;
            }
            if (!ok) {
                synchronized (this) { sending = false; retry = out; }
                h.postDelayed(this::pump, 4);
            }
        }

        @Override
        void close() {
            try { if (server != null) server.cancelConnection(dev); } catch (Exception ignored) {}
        }
    }
}
