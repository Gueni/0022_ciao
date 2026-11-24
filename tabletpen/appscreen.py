"""Media screen: YouTube and Spotify on a hidden virtual monitor that only the tablet shows.

* The monitor comes from the Parsec virtual display driver (vdd.py). It is added to the right of the
  real screens without changing how those are set up (TV, duplicate, …).
* Spotify = the user's own Spotify app; YouTube = a dedicated Brave/Edge window (sign in once, then
  it's the user's account). Both sit on the virtual monitor, never on the TV / laptop screen.
* Input from the tablet goes into the app through Chromium's DevTools channel (127.0.0.1 only). That
  is not real Windows input, so the game or film in front keeps focus the whole time.
"""
import base64
import ctypes
import json
import os
import queue
import socket
import subprocess
import threading
import time
from ctypes import wintypes as w

import vdd
from core import DATA_DIR, log
from media import keep_focus, processes, windows_of, child_pids, http_get

user32 = ctypes.windll.user32
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [w.HWND, ctypes.c_int, ctypes.c_ssize_t]
# HWND_TOPMOST (-1) / HWND_NOTOPMOST (-2) must go in as pointer-sized handles on 64-bit Windows
user32.SetWindowPos.argtypes = [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT]
GWL_EXSTYLE, WS_EX_NOACTIVATE = -20, 0x08000000
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x1, 0x2, 0x4, 0x10, 0x40
VM_W, VM_H = 1920, 1200          # the tablet's own resolution, so the picture is 1:1 sharp


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT), ("rcWork", w.RECT), ("dwFlags", w.DWORD)]


def work_area(rect):
    """Work area (monitor minus taskbar) of the monitor at rect = (x, y, w, h)."""
    pt = w.POINT(rect[0] + rect[2] // 2, rect[1] + rect[3] // 2)
    hmon = user32.MonitorFromPoint(pt, 2)
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(mi)
    user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
    r = mi.rcWork
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ---------------------------------------------------------------- DevTools connection to one app page
KEYS = {8: ("Backspace", "Backspace"), 9: ("Tab", "Tab"), 13: ("Enter", "Enter"), 27: ("Escape", "Escape"),
        32: (" ", "Space"), 33: ("PageUp", "PageUp"), 34: ("PageDown", "PageDown"), 35: ("End", "End"),
        36: ("Home", "Home"), 37: ("ArrowLeft", "ArrowLeft"), 38: ("ArrowUp", "ArrowUp"),
        39: ("ArrowRight", "ArrowRight"), 40: ("ArrowDown", "ArrowDown"), 46: ("Delete", "Delete")}


class Cdp:
    def __init__(self, port, match):
        self.port, self.match = port, match
        self.ws = None
        self.n = 0
        self.lock = threading.Lock()

    def alive(self):
        try:
            http_get(f"http://127.0.0.1:{self.port}/json/version", timeout=1)
            return True
        except Exception:
            return False

    def _connect(self):
        import websocket
        pages = json.loads(http_get(f"http://127.0.0.1:{self.port}/json", timeout=2))
        page = next((p for p in pages if p.get("type") == "page" and self.match(p.get("url", ""))), None) \
            or next((p for p in pages if p.get("type") == "page"), None)
        if not page:
            raise RuntimeError("app page not ready")
        self.ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=5, suppress_origin=True)

    def call(self, method, params=None):
        with self.lock:
            for attempt in (0, 1):
                try:
                    if self.ws is None:
                        self._connect()
                    self.n += 1
                    self.ws.send(json.dumps({"id": self.n, "method": method, "params": params or {}}))
                    while True:
                        r = json.loads(self.ws.recv())
                        if r.get("id") == self.n:
                            return r.get("result", {})
                except Exception:
                    try:
                        self.ws and self.ws.close()
                    except Exception:
                        pass
                    self.ws = None
                    if attempt:
                        raise

    def js(self, expr):
        r = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return (r.get("result") or {}).get("value")

    # ---- input (goes into the page directly: no Windows input, no focus change)
    def touch(self, kind, x=0, y=0):
        t = {"down": "touchStart", "move": "touchMove", "up": "touchEnd"}[kind]
        self.call("Input.dispatchTouchEvent", {"type": t, "touchPoints": [] if kind == "up" else [{"x": x, "y": y}]})

    def mouse_move(self, x, y):
        self.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})

    def click(self, x, y, count=1, button="left"):
        """A real mouse click: works on everything (menus, player controls, hover buttons)."""
        self.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        for t in ("mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", {"type": t, "x": x, "y": y, "button": button,
                                                   "buttons": 1 if t == "mousePressed" else 0, "clickCount": count})

    def wheel(self, x, y, dy, dx=0):
        self.call("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": x, "y": y, "deltaX": dx, "deltaY": dy})

    def text(self, s):
        if s:
            self.call("Input.insertText", {"text": s})

    def key(self, vk, mods=0):
        k = KEYS.get(vk)
        if not k:
            return
        key, code = k
        down = {"type": "rawKeyDown", "key": key, "code": code, "windowsVirtualKeyCode": vk,
                "nativeVirtualKeyCode": vk, "modifiers": mods}
        if vk in (13, 32):
            down = dict(down, type="keyDown", text="\r" if vk == 13 else " ")
        self.call("Input.dispatchKeyEvent", down)
        self.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": key, "code": code, "windowsVirtualKeyCode": vk,
                                             "nativeVirtualKeyCode": vk, "modifiers": mods})


# ---------------------------------------------------------------- frames straight from the app
class Screencast:
    """Chromium's own screencast of the app page: frames come from the renderer as JPEGs whenever the
    page changes, with no Windows screen capture at all, so video runs far smoother (also over Wi-Fi)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.seq = 0
        self.jpeg = None
        self.ok = False
        self._token = None
        self._ws = None

    def start(self, cdp, maxw, maxh):
        self.stop()
        token = object()
        self._token = token
        threading.Thread(target=self._run, args=(cdp, maxw, maxh, token), daemon=True).start()

    def stop(self):
        self._token = None
        self.ok = False
        ws, self._ws = self._ws, None
        try:
            ws and ws.close()
        except Exception:
            pass

    def frame(self):
        with self.lock:
            return self.seq, self.jpeg

    def _run(self, cdp, maxw, maxh, token):
        import websocket
        try:
            pages = json.loads(http_get(f"http://127.0.0.1:{cdp.port}/json", timeout=3))
            page = next((p for p in pages if p.get("type") == "page" and cdp.match(p.get("url", ""))), None) \
                or next((p for p in pages if p.get("type") == "page"), None)
            ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=10, suppress_origin=True)
            self._ws = ws
            ws.send(json.dumps({"id": 1, "method": "Page.startScreencast", "params": {
                "format": "jpeg", "quality": 85, "maxWidth": int(maxw), "maxHeight": int(maxh), "everyNthFrame": 1}}))
            self.ok = True
            log(f"[screen] app frames streaming ({maxw}x{maxh})")
            while self._token is token:
                try:
                    msg = json.loads(ws.recv())
                except websocket.WebSocketTimeoutException:
                    continue                                   # nothing changed on the page for a while
                if msg.get("method") == "Page.screencastFrame":
                    prm = msg["params"]
                    data = base64.b64decode(prm["data"])
                    with self.lock:
                        self.jpeg = data
                        self.seq += 1
                    ws.send(json.dumps({"id": 2, "method": "Page.screencastFrameAck",
                                        "params": {"sessionId": prm["sessionId"]}}))
        except Exception as e:
            if self._token is token:
                log(f"[screen] screencast stopped: {type(e).__name__}: {e}")
        finally:
            if self._token is token:
                self.ok = False


# ---------------------------------------------------------------- the two apps
class App:
    name = "?"
    title = "?"

    def __init__(self):
        self.cdp = None
        self.hwnd = None
        self.offset = (0, 0, 1.0)     # page origin on screen (x, y) and device pixel ratio
        self.page = None              # page rect on screen (x, y, w, h)

    def pids(self):
        return set()

    def window(self):
        if self.hwnd and user32.IsWindow(self.hwnd):
            return self.hwnd
        best, area = None, 0
        for h in windows_of(self.pids()):
            r = w.RECT()
            user32.GetWindowRect(h, ctypes.byref(r))
            a = (r.right - r.left) * (r.bottom - r.top)
            if a > area:
                best, area = h, a
        self.hwnd = best
        return best

    def ready(self):
        return self.cdp is not None and self.cdp.alive() and self.window() is not None

    def start(self, work):
        raise NotImplementedError

    def measure(self):
        """Where the web page sits on screen (physical pixels), so tablet touches land on the right spot.
        The page fills the bottom of the window's client area (an app window draws its title bar above)."""
        h = self.window()
        v = self.cdp.js("[innerWidth, innerHeight, devicePixelRatio]")
        if not h or not v:
            return
        iw, ih, dpr = v
        rc = w.RECT()
        user32.GetClientRect(h, ctypes.byref(rc))
        pt = w.POINT(0, 0)
        user32.ClientToScreen(h, ctypes.byref(pt))
        pw, ph = iw * dpr, ih * dpr
        self.offset = (pt.x + max(0.0, (rc.right - pw) / 2), pt.y + max(0.0, rc.bottom - ph), dpr)
        # the page itself on screen: what the tablet shows (no title bar), and where its touches land
        self.page = (round(self.offset[0]), round(self.offset[1]), round(pw), round(ph))

    def to_page(self, x, y):
        ox, oy, dpr = self.offset
        return (x - ox) / dpr, (y - oy) / dpr


class YouTube(App):
    name, title = "youtube", "YouTube"
    PROFILE = os.path.join(DATA_DIR, "player")
    HOME = "https://m.youtube.com/"
    # the touch-friendly mobile site (what YouTube shows an Android tablet), drawn at the tablet's scale
    UA = ("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/131.0.0.0 Mobile Safari/537.36")
    SCALE = 1.6

    def __init__(self):
        super().__init__()
        self.proc = None

    def pids(self):
        return child_pids(self.proc.pid) if self.proc and self.proc.poll() is None else set()

    def ready(self):
        if not super().ready():
            return False
        try:                       # a window from an older launch (desktop site / flagged as automated): restart it
            return self.cdp.js("navigator.webdriver !== true && /Mobile/.test(navigator.userAgent)") is True
        except Exception:
            return False

    @staticmethod
    def browser():
        pf, pf86, la = (os.environ.get(k, "") for k in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"))
        for p in (rf"{pf}\BraveSoftware\Brave-Browser\Application\brave.exe",
                  rf"{la}\BraveSoftware\Brave-Browser\Application\brave.exe",
                  rf"{pf86}\Microsoft\Edge\Application\msedge.exe",
                  rf"{pf}\Microsoft\Edge\Application\msedge.exe",
                  rf"{pf}\Google\Chrome\Application\chrome.exe"):
            if p and os.path.exists(p):
                return p
        return None

    def _close_leftover(self):
        """A window left from an earlier TabletPen run would swallow the new launch."""
        ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like ('*' + $env:TP_PROFILE + '*') }"
              " | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=20, capture_output=True,
                       creationflags=0x08000000, env=dict(os.environ, TP_PROFILE=self.PROFILE))

    def start(self, work):
        exe = self.browser()
        if not exe:
            raise RuntimeError("Brave, Edge or Chrome is needed for YouTube")
        self._close_leftover()
        os.makedirs(self.PROFILE, exist_ok=True)
        port_file = os.path.join(self.PROFILE, "DevToolsActivePort")
        try:
            os.remove(port_file)
        except OSError:
            pass
        x, y, ww, hh = work
        args = [exe, f"--user-data-dir={self.PROFILE}", "--remote-debugging-port=0",
                "--remote-allow-origins=http://127.0.0.1", "--no-first-run", "--no-default-browser-check",
                "--autoplay-policy=no-user-gesture-required", "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows",
                "--disable-features=CalculateNativeWinOcclusion",
                # without this the remote channel marks the browser as "automated" (navigator.webdriver)
                # and Google refuses to sign in
                "--disable-blink-features=AutomationControlled", "--test-type",  # (no "unsupported flag" banner)
                f"--user-agent={self.UA}", f"--force-device-scale-factor={self.SCALE}",
                f"--window-position={x},{y}",
                f"--window-size={ww},{hh}", f"--app={self.HOME}"]
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 4                                   # SW_SHOWNOACTIVATE
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
            raise RuntimeError("YouTube window didn't start")
        self.cdp = Cdp(port, lambda u: "youtube" in u)
        self.hwnd = None


class Spotify(App):
    name, title = "spotify", "Spotify"

    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.port = settings.get("spotify_port") or free_port()
        settings["spotify_port"] = self.port

    def pids(self):
        return {p for p, _, e in processes() if e.lower() == "spotify.exe"}

    @staticmethod
    def exe():
        cand = [os.path.join(os.environ.get("APPDATA", ""), "Spotify", "Spotify.exe"),
                os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Microsoft\WindowsApps\Spotify.exe")]
        return next((c for c in cand if os.path.exists(c)), None)

    def ready(self):
        if self.cdp is None:
            self.cdp = Cdp(self.port, lambda u: "xpui" in u)
        return super().ready()

    def start(self, work):
        exe = self.exe()
        if not exe:
            raise RuntimeError("Spotify isn't installed on this PC")
        prev = user32.GetForegroundWindow()
        if self.pids():           # running without the remote channel: restart it once with it
            subprocess.run(["taskkill", "/F", "/IM", "Spotify.exe"], capture_output=True, creationflags=0x08000000)
            for _ in range(30):
                if not self.pids():
                    break
                time.sleep(0.2)
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 4
        subprocess.Popen([exe, f"--remote-debugging-port={self.port}", "--remote-allow-origins=http://127.0.0.1"],
                         startupinfo=si, close_fds=True)
        threading.Thread(target=keep_focus, args=(prev, self.pids, 10), daemon=True).start()
        self.cdp = Cdp(self.port, lambda u: "xpui" in u)
        self.hwnd = None
        for _ in range(100):
            if self.cdp.alive():
                return
            time.sleep(0.2)
        raise RuntimeError("Spotify didn't start")


# ---------------------------------------------------------------- the media screen
class AppScreen:
    def __init__(self, settings, save_settings):
        self.vdd = vdd.Vdd()
        self.settings, self.save = settings, save_settings
        self.apps = {"youtube": YouTube(), "spotify": Spotify(settings)}
        save_settings()
        self.current = None
        self.work = None
        self.lock = threading.Lock()
        self.touching = False
        self.cast = Screencast()
        self.cast_size = (1920, 1200)  # what the tablet asked for
        self.q = queue.Queue()          # tablet input, handled in order on one worker thread
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        while True:
            fn, args = self.q.get()
            if fn is self._pen and args[0] in (0, 2) and not self.q.empty():
                nxt = self.q.queue[0]
                if nxt[0] is self._pen and nxt[1][0] == args[0]:
                    continue                             # a newer finger position is already waiting
            try:
                fn(*args)
            except Exception as e:
                log(f"[screen] {getattr(fn, '__name__', fn)}: {e}")

    def post(self, fn, *args):
        self.q.put((fn, args))

    @staticmethod
    def available():
        return vdd.Vdd.installed()

    def monitor(self):
        """Plugs the virtual monitor in (to the right of the real screens) and returns its work area."""
        name = vdd.virtual_display_name()
        if name is None:
            saved = vdd.real_layout()
            if not self.vdd.plug():
                raise RuntimeError("virtual display driver not available")
            for _ in range(30):
                time.sleep(0.1)
                name = vdd.virtual_display_name()
                if name:
                    break
            rect = vdd.display_rect(name) if name else None
            if not name or rect is None or rect[2] != VM_W or rect[3] != VM_H:
                vdd.apply_with_virtual(saved, VM_W, VM_H)
                for _ in range(30):
                    time.sleep(0.1)
                    name = vdd.virtual_display_name()
                    if name:
                        break
        if not name:
            raise RuntimeError("the virtual monitor didn't come up")
        self.work = work_area(vdd.display_rect(name))
        return self.work

    def park(self, app):
        """Puts the app's window on the virtual monitor, on top of the other app, without activating it."""
        h = app.window()
        if not h:
            return False
        user32.SetWindowLongPtrW(h, GWL_EXSTYLE, user32.GetWindowLongPtrW(h, GWL_EXSTYLE) | WS_EX_NOACTIVATE)
        if user32.IsIconic(h) or user32.IsZoomed(h):
            user32.ShowWindow(h, 4)                          # SW_SHOWNOACTIVATE (restore)
        x, y, ww, hh = self.work
        user32.SetWindowPos(h, 0, x, y, ww, hh, SWP_NOACTIVATE | SWP_SHOWWINDOW)
        # bring it in front of the other app without activating it: topmost for a moment, then back to
        # normal (a window that isn't active can't simply be raised, and staying topmost would be risky)
        user32.SetWindowPos(h, -1, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)    # HWND_TOPMOST
        user32.SetWindowPos(h, -2, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)    # HWND_NOTOPMOST
        return True

    def open(self, name):
        """Blocking: makes `name` the app on the media screen. Returns the rect the tablet should show."""
        with self.lock:
            app = self.apps[name]
            work = self.monitor()
            if not app.ready():
                log(f"[screen] starting {app.title}")
                app.start(work)
                for _ in range(100):
                    if app.window():
                        break
                    time.sleep(0.1)
            prev = user32.GetForegroundWindow()
            for _ in range(3):                               # windows can take a moment to accept moves
                self.park(app)
                time.sleep(0.15)
            if user32.GetForegroundWindow() != prev and prev:
                keep_focus(prev, app.pids, 0.5)
            try:
                app.measure()
            except Exception:
                pass
            self.current = app
            self.restart_cast()
            return app.page or work

    def restart_cast(self, size=None):
        if size:
            self.cast_size = size
        if self.current and self.current.cdp:
            self.cast.start(self.current.cdp, *self.cast_size)

    def refresh(self):
        """Windows adds a taskbar to a new monitor a moment after it appears: keep the app window inside
        the work area. Returns the new rect if it changed, else None."""
        with self.lock:
            name = vdd.virtual_display_name()
            if not name or not self.current:
                return None
            wa = work_area(vdd.display_rect(name))
            old = self.current.page
            if wa != self.work:
                self.work = wa
                self.park(self.current)
            try:
                self.current.measure()
            except Exception:
                pass
            if not self.cast.ok:
                self.restart_cast()
            new = self.current.page or self.work
            return new if new != old else None

    def unplug(self):
        """Media screen off: tuck the apps away (minimized, not activated) and remove the monitor."""
        with self.lock:
            for app in self.apps.values():
                h = app.window() if app.cdp else None
                if h:
                    user32.ShowWindow(h, 7)                  # SW_SHOWMINNOACTIVE
            self.current = None
            self.cast.stop()
            self.vdd.unplug()

    # ---- input from the tablet while it shows the media screen
    def pen(self, kind, sx, sy):
        self.post(self._pen, kind, sx, sy)

    def _pen(self, kind, sx, sy):
        """Tablet finger / pen on the media screen (screen pixels; kind 0 hover, 1 down, 2 move, 3 up).

        A tap becomes a real mouse click (desktop sites like YouTube only react properly to the mouse:
        menus, player controls, hover buttons), two quick taps a double-click. Once the finger moves,
        it becomes a touch drag so scrolling feels natural. Pen hover moves the mouse (hover menus)."""
        app = self.current
        if not app or not app.cdp:
            return
        x, y = app.to_page(sx, sy)
        try:
            if kind == 0:
                if not self.touching and time.monotonic() - getattr(self, "_hover_t", 0) > 0.04:
                    self._hover_t = time.monotonic()
                    app.cdp.mouse_move(x, y)
            elif kind == 1:
                app.measure()
                x, y = app.to_page(sx, sy)
                self.touching = True
                self._start = (sx, sy, x, y)
                self._dragging = False
            elif kind == 2 and self.touching:
                if not self._dragging and abs(sx - self._start[0]) + abs(sy - self._start[1]) > 14:
                    self._dragging = True
                    app.cdp.touch("down", self._start[2], self._start[3])
                if self._dragging:
                    app.cdp.touch("move", x, y)
            elif kind in (3, 4) and self.touching:
                self.touching = False
                if self._dragging:
                    app.cdp.touch("up")
                else:
                    px, py = self._start[2], self._start[3]
                    last = getattr(self, "_last_click", (0, -999, -999))
                    double = time.monotonic() - last[0] < 0.4 and abs(px - last[1]) + abs(py - last[2]) < 30
                    app.cdp.click(px, py, 2 if double else 1)
                    self._last_click = (0, -999, -999) if double else (time.monotonic(), px, py)
        except Exception as e:
            log(f"[screen] input: {e}")

    def text(self, s):
        self.post(self._text, s)

    def _text(self, s):
        if self.current and self.current.cdp:
            self.current.cdp.text(s)

    def keys(self, *a):
        self.post(self._keys, *a)

    def _keys(self, vks):
        app = self.current
        if not app or not app.cdp:
            return
        mods = 0
        for vk in vks:
            if vk in (16, 160, 161):
                mods |= 8
            elif vk in (17, 162, 163):
                mods |= 2
            elif vk in (18, 164, 165):
                mods |= 1
        for vk in vks:
            if vk in KEYS:
                app.cdp.key(vk, mods)

    def back(self):
        self.post(self._back)

    def _back(self):
        if self.current and self.current.cdp:
            self.current.cdp.js("history.back()")

    def scroll(self, *a):
        self.post(self._scroll, *a)

    def _scroll(self, sx, sy, dy):
        app = self.current
        if app and app.cdp:
            x, y = app.to_page(sx, sy)
            app.cdp.wheel(x, y, dy)
