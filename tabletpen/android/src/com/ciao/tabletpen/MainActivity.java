package com.ciao.tabletpen;

import android.app.Activity;
import android.content.Intent;
import android.widget.FrameLayout;
import android.bluetooth.BluetoothAdapter;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.provider.Settings;
import android.util.Base64;
import android.view.View;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import org.json.JSONObject;

import java.io.ByteArrayInputStream;
import java.util.HashMap;
import java.util.Map;
import java.util.UUID;

public class MainActivity extends Activity implements Hub.Js {
    private static final String ORIGIN = "https://tabletpen.local";

    private WebView web;
    private FrameLayout root;
    private YouTubeView yt;
    private Hub hub;
    private Net net;
    private Ble ble;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON
                | WindowManager.LayoutParams.FLAG_FULLSCREEN);

        SharedPreferences prefs = getSharedPreferences("tp", MODE_PRIVATE);
        String id = prefs.getString("id", null);
        if (id == null) {
            id = UUID.randomUUID().toString();
            prefs.edit().putString("id", id).apply();
        }
        String name = Settings.Global.getString(getContentResolver(), "device_name");
        if (name == null || name.isEmpty()) name = Build.MANUFACTURER + " " + Build.MODEL;

        hub = new Hub(this, id, name);
        net = new Net(this, hub, id, name);
        ble = new Ble(this, hub, this);

        WebView.setWebContentsDebuggingEnabled(true);   // only reachable over an authorized adb connection
        web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        web.setOverScrollMode(View.OVER_SCROLL_NEVER);
        web.setBackgroundColor(0xFF111114);
        web.addJavascriptInterface(new Bridge(), "Native");
        web.setWebViewClient(new WebViewClient() {
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView v, WebResourceRequest req) {
                return serve(req.getUrl());
            }
        });
        root = new FrameLayout(this);
        root.addView(web);
        setContentView(root);
        yt = new YouTubeView(this, root, hub);
        web.loadUrl(ORIGIN + "/");
        web.setOnSystemUiVisibilityChangeListener(v -> {
            if ((v & View.SYSTEM_UI_FLAG_FULLSCREEN) == 0) web.postDelayed(this::immersive, 1500);
        });
        immersive();

        net.start();
        ble.start();
    }

    /** The page and the latest preview frame are served from memory under https://tabletpen.local/. */
    private WebResourceResponse serve(Uri u) {
        if (!"tabletpen.local".equals(u.getHost())) return null;
        Map<String, String> h = new HashMap<>();
        h.put("Cache-Control", "no-store");
        try {
            String path = u.getPath() == null ? "/" : u.getPath();
            if (path.equals("/frame")) {
                byte[] f = null;
                try { f = hub.frameBytes(Integer.parseInt(u.getQuery())); } catch (Exception ignored) {}
                if (f == null) return new WebResourceResponse("image/jpeg", null, 404, "none", h, new ByteArrayInputStream(new byte[0]));
                String mime = f.length > 12 && f[0] == 'R' && f[1] == 'I' && f[8] == 'W' ? "image/webp" : "image/jpeg";
                return new WebResourceResponse(mime, null, 200, "OK", h, new ByteArrayInputStream(f));
            }
            if (path.equals("/img")) {
                byte[] f = hub.image(u.getQueryParameter("k"));
                if (f == null) return new WebResourceResponse("image/jpeg", null, 404, "none", h, new ByteArrayInputStream(new byte[0]));
                return new WebResourceResponse("image/jpeg", null, 200, "OK", h, new ByteArrayInputStream(f));
            }
            if (path.equals("/") || path.equals("/pad.html"))
                return new WebResourceResponse("text/html", "utf-8", 200, "OK", h, getAssets().open("pad.html"));
        } catch (Exception ignored) {}
        return new WebResourceResponse("text/plain", "utf-8", 404, "not found", h, new ByteArrayInputStream(new byte[0]));
    }

    private void immersive() {
        web.setSystemUiVisibility(View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                | View.SYSTEM_UI_FLAG_FULLSCREEN | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                | View.SYSTEM_UI_FLAG_LAYOUT_STABLE);
    }

    @Override
    protected void onResume() {
        super.onResume();
        immersive();
        net.foreground(true);
    }

    @Override
    public void onBackPressed() {
        if (yt.back()) return;                     // YouTube tab: leave fullscreen / go back a page
        moveTaskToBack(true);                      // keep running (and connected) in the background
    }

    @Override
    protected void onStop() {
        net.foreground(false);
        super.onStop();
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) immersive();
    }

    @Override
    protected void onDestroy() {
        ble.stop();
        net.stop();
        hub.closeAll();
        super.onDestroy();
    }

    @Override
    public void toJs(final String type, final String data) {
        runOnUiThread(() -> web.evaluateJavascript(
                "window.onNative&&onNative(" + JSONObject.quote(type) + "," + JSONObject.quote(data) + ")", null));
    }

    class Bridge {
        /** The YouTube tab: shown right of the sidebar (and above the key bar when it's open). */
        @JavascriptInterface
        public void youtube(final boolean show, final int leftPx, final int bottomPx) {
            runOnUiThread(() -> { yt.show(show, leftPx, bottomPx); if (!show) immersive(); });
        }

        @JavascriptInterface
        public void ytNav(final String cmd) {
            runOnUiThread(() -> yt.nav(cmd));
        }

        @JavascriptInterface
        public void setYtCookies(final String json) {
            runOnUiThread(() -> yt.setCookies(json));
        }


        @JavascriptInterface
        public void start() {           // page loaded: send it the current state
            hub.publish();
            BluetoothAdapter ad = BluetoothAdapter.getDefaultAdapter();
            toJs("bt", ad != null && ad.isEnabled() ? "on" : "off");
        }

        @JavascriptInterface
        public void send(String b64) {
            hub.send(Base64.decode(b64, Base64.DEFAULT));
        }

        @JavascriptInterface
        public void select(String id) {
            hub.select(id);
        }

        /** Saves a PNG (base64) into the gallery: Pictures/TabletPen. No permission needed on Android 10+. */
        @JavascriptInterface
        public void saveShot(String b64) {
            try {
                byte[] png = Base64.decode(b64, Base64.DEFAULT);
                String name = "TabletPen_" + new java.text.SimpleDateFormat("yyyyMMdd_HHmmss", java.util.Locale.US)
                        .format(new java.util.Date()) + ".png";
                android.content.ContentValues v = new android.content.ContentValues();
                v.put(android.provider.MediaStore.Images.Media.DISPLAY_NAME, name);
                v.put(android.provider.MediaStore.Images.Media.MIME_TYPE, "image/png");
                v.put(android.provider.MediaStore.Images.Media.RELATIVE_PATH, "Pictures/TabletPen");
                android.content.ContentResolver cr = getContentResolver();
                Uri uri = cr.insert(android.provider.MediaStore.Images.Media.EXTERNAL_CONTENT_URI, v);
                if (uri == null) return;
                try (java.io.OutputStream o = cr.openOutputStream(uri)) { o.write(png); }
            } catch (Exception e) {
                toJs("msg", "{\"toast\":\"Couldn't save the copy on the tablet\"}");
            }
        }

        /** The tablet's Wi-Fi address, for networks where the PC can't discover it by itself. */
        @JavascriptInterface
        public String ip() {
            try {
                android.net.wifi.WifiManager wm = (android.net.wifi.WifiManager)
                        getApplicationContext().getSystemService(WIFI_SERVICE);
                int a = wm.getConnectionInfo().getIpAddress();
                if (a == 0) return "";
                return (a & 255) + "." + (a >> 8 & 255) + "." + (a >> 16 & 255) + "." + (a >> 24 & 255);
            } catch (Exception e) {
                return "";
            }
        }
    }
}
