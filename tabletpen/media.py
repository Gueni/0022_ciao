"""Now playing: see and control what plays on the PC (Spotify, YouTube in a browser, any app that shows
up in Windows' media overlay) from the tablet, without touching the PC or taking focus from the game.
YouTube and Spotify themselves are shown on the tablet by the media screen (appscreen.py).

Tablet -> PC   0x0B + JSON   {"a": "sub", "on": bool} | {"a": "ctl", "k": session, "c": action, "v": value}
PC -> tablet   0x30 + JSON   {"t": "state", "sessions": [...]}
               0x31 + [key len u8][key utf-8][JPEG]   cover art

Also shared helpers for the media screen: window lookup, focus guard, process list.
"""
import asyncio
import ctypes
import io
import json
import time
import urllib.request
import zlib
from ctypes import wintypes

from PIL import Image

from core import log

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
user32 = ctypes.windll.user32


# ---------------------------------------------------------------- small helpers
def http_get(url, headers=None, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def thumb(data, size, q=78):
    """Any image bytes -> small JPEG."""
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img.thumbnail(size, Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=q)
    return buf.getvalue()


def img_key(*parts):
    return "i" + format(zlib.crc32("|".join(map(str, parts)).encode()), "08x")


def window_pid(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def windows_of(pids):
    """Visible top-level windows that belong to the given process ids."""
    found = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(h, _):
        if user32.IsWindowVisible(h) and window_pid(h) in pids and user32.GetWindowTextLengthW(h):
            found.append(h)
        return True
    user32.EnumWindows(proc(cb), 0)
    return found


def keep_focus(prev, pids_fn, seconds=6.0, minimize=False):
    """If a window we just opened grabs the foreground, hand the foreground straight back to what was
    there (the game). With minimize=True the intruding window is also minimized."""
    if not prev:
        return
    end = time.time() + seconds
    while time.time() < end:
        fg = user32.GetForegroundWindow()
        if fg and fg != prev and window_pid(fg) in pids_fn():
            if minimize:
                user32.ShowWindow(fg, 7)                     # SW_SHOWMINNOACTIVE
            user32.keybd_event(0x12, 0, 0, 0)                # a tap of Alt lets us set the foreground
            user32.keybd_event(0x12, 0, 2, 0)
            user32.SetForegroundWindow(prev)
        time.sleep(0.1)


class _PE(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


def processes():
    """[(pid, parent pid, exe name)] for every running process."""
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(2, 0)                  # TH32CS_SNAPPROCESS
    out = []
    e = _PE()
    e.dwSize = ctypes.sizeof(_PE)
    ok = k32.Process32FirstW(snap, ctypes.byref(e))
    while ok:
        out.append((e.th32ProcessID, e.th32ParentProcessID, e.szExeFile))
        ok = k32.Process32NextW(snap, ctypes.byref(e))
    k32.CloseHandle(snap)
    return out


def child_pids(root):
    """root and all its descendants (Chromium runs many processes)."""
    kids = {}
    for pid, ppid, _ in processes():
        kids.setdefault(ppid, []).append(pid)
    res, todo = set(), [root]
    while todo:
        x = todo.pop()
        if x not in res:
            res.add(x)
            todo += kids.get(x, [])
    return res


def app_name(aumid):
    a = (aumid or "").lower()
    for k, v in (("spotify", "Spotify"), ("brave", "YouTube · Brave"), ("msedge", "Edge"), ("chrome", "Chrome"),
                 ("firefox", "Firefox"), ("vlc", "VLC"), ("zune", "Media Player"), ("music", "Music")):
        if k in a:
            return v
    return (aumid or "App").split("!")[-1].replace(".exe", "")[:20]


# ---------------------------------------------------------------- the now-playing service
class Media:
    def __init__(self, core):
        self.core = core
        self.subs = set()          # links whose tablet has the media panel open
        self.mgr = None
        self.art = {}              # session key -> (signature, image key, jpeg)
        self.sent_imgs = {}        # link -> set(image keys)
        self.last = {}             # link -> last state json
        self.sessions = {}         # key -> GSMTC session

    async def send(self, link, obj):
        await link.send_big(b"\x30" + json.dumps(obj, separators=(",", ":")).encode())

    async def send_img(self, link, key, jpg):
        seen = self.sent_imgs.setdefault(link, set())
        if key in seen:
            return
        seen.add(key)
        k = key.encode()
        await link.send_big(b"\x31" + bytes([len(k)]) + k + jpg)

    def link_down(self, link):
        self.subs.discard(link)
        self.sent_imgs.pop(link, None)
        self.last.pop(link, None)

    async def run(self):
        try:
            from winrt.windows.media.control import \
                GlobalSystemMediaTransportControlsSessionManager as M
            self.mgr = await M.request_async()
        except Exception as e:
            log(f"[media] now-playing unavailable: {e}")
        while True:
            try:
                if self.subs:
                    await self.push()
            except Exception as e:
                log(f"[media] {type(e).__name__}: {e}")
            await asyncio.sleep(1)

    async def _art(self, key, sig, props):
        old = self.art.get(key)
        if old and old[0] == sig:
            return old[1]
        self.art[key] = (sig, None, None)
        try:
            ref = props.thumbnail
            if ref is None:
                return None
            from winrt.windows.storage.streams import DataReader
            stream = await ref.open_read_async()
            n = int(stream.size)
            reader = DataReader(stream.get_input_stream_at(0))
            await reader.load_async(n)
            buf = bytearray(n)
            reader.read_bytes(buf)
            jpg = thumb(bytes(buf), (240, 240))
            ik = img_key(key, sig)
            self.art[key] = (sig, ik, jpg)
            return ik
        except Exception:
            return None

    async def state(self):
        out = []
        self.sessions = {}
        if not self.mgr:
            return out
        for s in self.mgr.get_sessions():
            try:
                aumid = s.source_app_user_model_id
                props = await s.try_get_media_properties_async()
                info = s.get_playback_info()
                tl = s.get_timeline_properties()
            except Exception:
                continue
            title = props.title or ""
            key = aumid
            self.sessions[key] = s
            status = int(info.playback_status)                  # 4 playing, 5 paused
            secs = lambda t: t.total_seconds() if hasattr(t, "total_seconds") else (t or 0) / 1e7
            pos, dur = secs(tl.position), secs(tl.end_time)
            try:                                                # the timeline is a snapshot: bring it up to date
                if status == 4 and tl.last_updated_time:
                    import datetime
                    lu = tl.last_updated_time
                    now = datetime.datetime.now(lu.tzinfo) if lu.tzinfo else datetime.datetime.utcnow()
                    pos += max(0.0, (now - lu).total_seconds())
            except Exception:
                pass
            c = info.controls
            art = await self._art(key, (title, props.artist), props)
            a = (aumid or "").lower()
            kind = "sp" if "spotify" in a else "yt" if "brave" in a else "other"
            out.append({"k": key, "app": app_name(aumid), "kind": kind, "title": title or app_name(aumid),
                        "artist": props.artist or "", "album": props.album_title or "",
                        "playing": status == 4, "pos": min(pos, dur) if dur else pos, "dur": dur,
                        "canSeek": bool(c.is_playback_position_enabled) and dur > 0,
                        "canNext": bool(c.is_next_enabled), "canPrev": bool(c.is_previous_enabled), "art": art})
        out.sort(key=lambda x: (not x["playing"], x["kind"] == "other"))
        return out

    async def push(self, force=False):
        sessions = await self.state()
        msg = {"t": "state", "sessions": sessions}
        for link in list(self.subs):
            if not link.active:
                continue
            for s in sessions:                                  # cover art first, once per link
                a = self.art.get(s["k"])
                if s.get("art") and a and a[2]:
                    await self.send_img(link, s["art"], a[2])
            # skip unchanged states (positions move every second, so compare without them)
            sig = json.dumps([{k: v for k, v in s.items() if k != "pos"} for s in sessions])
            if force or self.last.get(link) != sig or int(time.time()) % 5 == 0:
                self.last[link] = sig
                await self.send(link, msg)

    async def request(self, link, req):
        a = req.get("a")
        try:
            if a == "sub":
                if req.get("on"):
                    self.subs.add(link)
                    self.last.pop(link, None)
                    await self.push(force=True)
                else:
                    self.subs.discard(link)
            elif a == "ctl":
                s = self.sessions.get(req.get("k"))
                c, v = req.get("c"), req.get("v")
                if s is not None:
                    if c == "toggle":
                        await s.try_toggle_play_pause_async()
                    elif c == "next":
                        await s.try_skip_next_async()
                    elif c == "prev":
                        await s.try_skip_previous_async()
                    elif c == "seek":
                        await s.try_change_playback_position_async(int(float(v) * 10_000_000))
                await asyncio.sleep(0.3)
                await self.push(force=True)
        except Exception as e:
            log(f"[media] {a}: {type(e).__name__}: {e}")
