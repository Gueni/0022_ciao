"""YouTube tab, PC side: the tablet plays YouTube itself (muted) and reports what it plays; a hidden
YouTube on the PC plays the same video in sync, so the sound comes from the PC (TV, speakers).

Tablet -> PC  0x0E + JSON  {"v": video id | null, "t": seconds, "p": playing, "r": speed, "ad": bool}
              0x0F + JSON  {"a": "yt_cookies" | "yt_signin" | "spotify"}
PC -> tablet  0x32 + JSON  the PC player's YouTube/Google cookies (so the tablet tab is signed in too)

The PC player is a dedicated Brave/Edge window (its own profile, the same one used before), started
minimized and driven over the local DevTools channel – it never shows up or takes focus.
"""
import json
import os
import subprocess
import threading
import time

from core import log
from appscreen import Cdp, YouTube
from media import child_pids, keep_focus, processes, windows_of, http_get

import ctypes
user32 = ctypes.windll.user32

OFF = (-4000, -4000)  # where the PC player window lives: on no screen
LEAD = 0.06          # seconds the PC runs ahead, to make up for the trip over the link
TOLERANCE = 0.35     # drift allowed before the PC jumps to the tablet's position

SYNC_JS = r"""(function(id,t,p,r,ad){
  var cur = new URLSearchParams(location.search).get('v');
  if (cur !== id) return 'nav';
  var v = document.querySelector('video.html5-main-video') || document.querySelector('video');
  if (!v) return 'wait';
  if (v.muted) v.muted = false;
  if (v.volume < 0.05) v.volume = 1;
  if (ad || !p) {
    if (!v.paused) v.pause();
    if (!ad && Math.abs(v.currentTime - t) > 0.4) v.currentTime = t;
    return 'paused';
  }
  if (Math.abs(v.playbackRate - r) > 0.01) v.playbackRate = r;
  var d = v.currentTime - t;
  if (Math.abs(d) > %TOL%) v.currentTime = t;
  if (v.paused) v.play();
  return 'ok ' + d.toFixed(2);
})"""


class YtSync:
    def __init__(self):
        self.proc = None
        self.cdp = None
        self.state = None
        self.recv = 0.0
        self.nav_at = 0.0
        self.ev = threading.Event()
        self.lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True).start()

    # ---- the hidden PC player
    def pids(self):
        return child_pids(self.proc.pid) if self.proc and self.proc.poll() is None else set()

    def _alive(self):
        return self.cdp is not None and self.cdp.alive() and self.proc is not None and self.proc.poll() is None

    def _start(self, url="https://www.youtube.com/"):
        exe = YouTube.browser()
        if not exe:
            raise RuntimeError("Brave, Edge or Chrome is needed for YouTube sound on the PC")
        YouTube()._close_leftover()
        prof = YouTube.PROFILE
        os.makedirs(prof, exist_ok=True)
        port_file = os.path.join(prof, "DevToolsActivePort")
        try:
            os.remove(port_file)
        except OSError:
            pass
        args = [exe, f"--user-data-dir={prof}", "--remote-debugging-port=0",
                "--remote-allow-origins=http://127.0.0.1", "--no-first-run", "--no-default-browser-check",
                "--autoplay-policy=no-user-gesture-required", "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows",
                "--disable-features=CalculateNativeWinOcclusion,MediaSessionService",
                "--disable-blink-features=AutomationControlled", "--test-type",
                "--disable-background-media-suspend",
                # a real, visible window – just placed off-screen. A window that starts minimized counts as
                # "never shown", and the browser then holds back video sound until it's opened once.
                f"--window-position={OFF[0]},{OFF[1]}", "--window-size=960,540", f"--app={url}"]
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 4                                   # shown, not activated
        prev = user32.GetForegroundWindow()
        self.proc = subprocess.Popen(args, startupinfo=si, close_fds=True)
        threading.Thread(target=keep_focus, args=(prev, self.pids, 8), daemon=True).start()
        port = None
        for _ in range(150):
            if os.path.exists(port_file):
                try:
                    port = int(open(port_file).read().split()[0])
                    break
                except Exception:
                    pass
            time.sleep(0.1)
        if not port:
            raise RuntimeError("the PC's YouTube player didn't start")
        self.cdp = Cdp(port, lambda u: "youtube" in u)
        for _ in range(50):                                  # wait for the page, then make sure it's hidden
            try:
                self.cdp.js("1")
                break
            except Exception:
                time.sleep(0.2)
        self._hide()
        log("[yt] PC player started")

    def _hide(self):
        """Keep the player window off-screen, out of the taskbar / Alt+Tab, never minimized."""
        wins = windows_of(self.pids())
        if time.monotonic() < getattr(self, "_signin_until", 0):
            # the user is signing in on the PC: leave it alone until they minimize or close it
            if not any(user32.IsIconic(h) for h in wins):
                return
            self._signin_until = 0
        for h in wins:
            ex = user32.GetWindowLongW(h, -20)
            want = (ex | 0x80 | 0x08000000) & ~0x40000          # TOOLWINDOW | NOACTIVATE, not APPWINDOW
            if ex != want:
                user32.ShowWindow(h, 0)                        # (style change needs a hide/show)
                user32.SetWindowLongW(h, -20, want)
            if user32.IsIconic(h) or not user32.IsWindowVisible(h):
                user32.ShowWindow(h, 4)                        # SW_SHOWNOACTIVATE
            user32.SetWindowPos(h, 0, OFF[0], OFF[1], 960, 540, 0x10 | 0x04)   # NOACTIVATE | NOZORDER

    def ensure(self):
        with self.lock:
            if not self._alive():
                self._start()

    # ---- sync
    def update(self, st):
        self.state = st
        self.recv = time.monotonic()
        self.ev.set()

    def _loop(self):
        while True:
            self.ev.wait(0.5)                    # every new tablet state, and twice a second to fix drift
            self.ev.clear()
            st = self.state
            if not st:
                continue
            try:
                self._apply(st)
            except Exception as e:
                log(f"[yt] {type(e).__name__}: {e}")
                time.sleep(1)

    def _apply(self, st):
        vid = st.get("v")
        if not vid:
            if self._alive():
                self.cdp.js("(function(){var v=document.querySelector('video'); if(v&&!v.paused) v.pause()})()")
            self.state = None
            return
        self.ensure()
        playing, rate = bool(st.get("p")), float(st.get("r") or 1)
        target = float(st.get("t") or 0) + (time.monotonic() - self.recv) * (rate if playing else 0)
        if playing:
            target += LEAD
        call = SYNC_JS.replace("%TOL%", str(TOLERANCE)) + f"({json.dumps(vid)},{target:.3f},{'true' if playing else 'false'},{rate},{'true' if st.get('ad') else 'false'})"
        res = self.cdp.js(call)
        if time.monotonic() - getattr(self, "_hid_at", 0) > 5:      # someone minimized / moved it: put it back
            self._hid_at = time.monotonic()
            self._hide()
        if res == "nav" and time.monotonic() - self.nav_at > 2:
            self.nav_at = time.monotonic()
            self.cdp.call("Page.navigate", {"url": f"https://www.youtube.com/watch?v={vid}&t={int(target)}s"})

    # ---- sign-in sharing
    def cookies(self):
        """The PC player's YouTube/Google cookies if it's signed in, else []."""
        self.ensure()
        allc = self.cdp.call("Network.getAllCookies").get("cookies", [])
        keep = [c for c in allc if c.get("domain", "").endswith(("youtube.com", "google.com"))]
        if not any(c.get("name") in ("SAPISID", "__Secure-3PAPISID", "SID") for c in keep):
            return []
        return [{k: c.get(k) for k in ("name", "value", "domain", "path", "secure", "httpOnly", "expires")} for c in keep]

    def show_for_signin(self):
        """Brings the PC player up on the screen so the user can sign in to YouTube once (minimize it when
        done – it then goes back into hiding)."""
        self.ensure()
        self._signin_until = time.monotonic() + 900
        self.cdp.call("Page.navigate", {"url": "https://accounts.google.com/ServiceLogin?service=youtube&continue=https://www.youtube.com/"})
        for h in windows_of(self.pids()):
            ex = user32.GetWindowLongW(h, -20)
            user32.SetWindowLongW(h, -20, (ex & ~0x80 & ~0x08000000) | 0x40000)   # a normal window again
            user32.SetWindowPos(h, 0, 200, 120, 1100, 760, 0x04)
            user32.ShowWindow(h, 9)
            user32.keybd_event(0x12, 0, 0, 0)
            user32.keybd_event(0x12, 0, 2, 0)
            user32.SetForegroundWindow(h)


# ---------------------------------------------------------------- Spotify (the tablet uses the Spotify app)
def spotify_pids():
    return {p for p, _, e in processes() if e.lower() == "spotify.exe"}


def ensure_spotify():
    """The tablet's Spotify app plays on the PC through Spotify Connect: the PC's Spotify just has to be
    running. Start it minimized (and hand focus back) if it isn't."""
    if spotify_pids():
        return False
    exe = next((c for c in (os.path.join(os.environ.get("APPDATA", ""), "Spotify", "Spotify.exe"),
                            os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Microsoft\WindowsApps\Spotify.exe"))
                if os.path.exists(c)), None)
    if not exe:
        return False
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 7
    prev = user32.GetForegroundWindow()
    subprocess.Popen([exe, "--minimized"], startupinfo=si, close_fds=True)
    threading.Thread(target=keep_focus, args=(prev, spotify_pids, 10, True), daemon=True).start()
    log("[spotify] started on the PC")
    return True
