package com.ciao.tabletpen;

import android.app.Activity;
import android.graphics.Color;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;

import org.json.JSONArray;
import org.json.JSONObject;

import java.nio.charset.StandardCharsets;

/**
 * The YouTube tab: the touch-friendly YouTube site running right here on the tablet (smooth video,
 * instant touches), always muted. Its state (video, time, play/pause, speed, ad) goes to the PC, where
 * a hidden YouTube plays the same video in sync – so the sound comes out of the PC.
 */
class YouTubeView {
    // a normal Chrome-on-Android identity: Google refuses sign-in from in-app browsers ("wv")
    private static final String UA = "Mozilla/5.0 (Linux; Android 10; AGS6-W09) AppleWebKit/537.36 "
            + "(KHTML, like Gecko) Chrome/114.0.5735.196 Mobile Safari/537.36";

    private static final String SYNC_JS = "(function(){if(window.__tp)return;window.__tp=1;"
            + "var last='',lastT=0;"
            + "function vid(){try{var u=new URL(location.href);var v=u.searchParams.get('v');"
            + "if(v)return v;var m=location.pathname.match(/^\\/shorts\\/([\\w-]+)/);return m?m[1]:null}catch(e){return null}}"
            + "function send(force){var v=document.querySelector('video');var id=vid();var st;"
            + "if(v&&id){st={v:id,t:v.currentTime,p:!v.paused&&!v.ended&&v.readyState>2,r:v.playbackRate,"
            + "ad:!!document.querySelector('.ad-showing,.ad-interrupting'),d:v.duration||0}}else{st={v:null}}"
            + "var key=JSON.stringify([st.v,st.p,st.r,st.ad]);var now=Date.now();"
            + "if(force||key!==last||now-lastT>700){last=key;lastT=now;TPYT.state(JSON.stringify(st))}}"
            + "function hook(v){if(v.__tp)return;v.__tp=1;"
            + "['play','pause','seeked','ratechange','playing','waiting','ended','loadedmetadata'].forEach(function(e){"
            + "v.addEventListener(e,function(){send(true)})});"
            + "v.addEventListener('volumechange',function(){if(!v.muted)v.muted=true})}"
            + "setInterval(function(){var vs=document.querySelectorAll('video');"
            + "for(var i=0;i<vs.length;i++){vs[i].muted=true;hook(vs[i])}send(false)},200);"
            + "})();";

    private final Activity act;
    private final FrameLayout root;
    private final Hub hub;
    private WebView web;
    private View custom;
    private WebChromeClient.CustomViewCallback customCb;
    private boolean loaded, shown;

    YouTubeView(Activity act, FrameLayout root, Hub hub) {
        this.act = act;
        this.root = root;
        this.hub = hub;
    }

    private void ensure() {
        if (web != null) return;
        web = new WebView(act);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setUserAgentString(UA);
        s.setSupportMultipleWindows(false);
        web.setBackgroundColor(Color.BLACK);
        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, true);
        web.addJavascriptInterface(new Sync(), "TPYT");
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                String scheme = r.getUrl().getScheme();
                return !"https".equals(scheme) && !"http".equals(scheme);   // never jump out to other apps
            }

            @Override
            public void onPageFinished(WebView v, String url) {
                v.evaluateJavascript(SYNC_JS, null);
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onShowCustomView(View view, CustomViewCallback cb) {     // video fullscreen
                if (custom != null) { cb.onCustomViewHidden(); return; }
                custom = view;
                customCb = cb;
                view.setBackgroundColor(Color.BLACK);
                root.addView(view, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                        ViewGroup.LayoutParams.MATCH_PARENT));
            }

            @Override
            public void onHideCustomView() {
                if (custom == null) return;
                root.removeView(custom);
                custom = null;
                if (customCb != null) customCb.onCustomViewHidden();
                customCb = null;
            }
        });
        web.setVisibility(View.INVISIBLE);
        root.addView(web);
    }

    /** Shows / hides the tab. It keeps playing while hidden (the sound is on the PC anyway). */
    void show(boolean on, int leftPx, int bottomPx) {
        ensure();
        FrameLayout.LayoutParams lp = new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT);
        lp.leftMargin = leftPx;
        lp.bottomMargin = bottomPx;
        web.setLayoutParams(lp);
        shown = on;
        web.setVisibility(on ? View.VISIBLE : View.INVISIBLE);
        if (on && !loaded) {
            loaded = true;
            web.loadUrl("https://m.youtube.com/");
        }
        if (on) web.requestFocus();
    }

    boolean shown() { return shown; }

    /** Android back: leave fullscreen, then go back inside YouTube. */
    boolean back() {
        if (custom != null) {
            root.removeView(custom);
            custom = null;
            if (customCb != null) customCb.onCustomViewHidden();
            customCb = null;
            return true;
        }
        if (web != null && shown && web.canGoBack()) {
            web.goBack();
            return true;
        }
        return false;
    }

    /** Sidebar buttons: "back" (also leaves fullscreen / Shorts) and "home". */
    void nav(String cmd) {
        if (web == null) return;
        if ("home".equals(cmd)) {
            if (custom != null) back();
            web.loadUrl("https://m.youtube.com/");
        } else if (!back()) {
            web.loadUrl("https://m.youtube.com/");                 // nothing to go back to: home
        }
    }

    /** The PC's YouTube login (cookies), so this tab is signed in to the same account. */
    void setCookies(String json) {
        try {
            JSONArray a = new JSONArray(json);
            CookieManager cm = CookieManager.getInstance();
            for (int i = 0; i < a.length(); i++) {
                JSONObject c = a.getJSONObject(i);
                String domain = c.getString("domain");
                StringBuilder sb = new StringBuilder();
                sb.append(c.getString("name")).append('=').append(c.getString("value"))
                        .append("; Domain=").append(domain).append("; Path=").append(c.optString("path", "/"));
                if (c.optBoolean("secure")) sb.append("; Secure");
                if (c.optBoolean("httpOnly")) sb.append("; HttpOnly");
                long exp = c.optLong("expires", 0);
                if (exp > 0) sb.append("; Max-Age=").append(Math.max(0, exp - System.currentTimeMillis() / 1000));
                String host = domain.startsWith(".") ? domain.substring(1) : domain;
                cm.setCookie("https://" + host + "/", sb.toString());
            }
            cm.flush();
            if (web != null && loaded) web.reload();
        } catch (Exception ignored) {}
    }

    class Sync {
        @JavascriptInterface
        public void state(String json) {
            byte[] b = json.getBytes(StandardCharsets.UTF_8);
            byte[] p = new byte[b.length + 1];
            p[0] = 0x0E;
            System.arraycopy(b, 0, p, 1, b.length);
            hub.send(p);
        }
    }
}
