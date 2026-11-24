"""TabletPen core: links to tablets, approval, pen dispatch, screen preview.

Tablet -> PC packets:
  0x01 + N x [kind u8, x u16, y u16, pressure u16, tiltX i8, tiltY i8, flags u8]   pen events
        kind 0 hover, 1 down, 2 move, 3 up, 4 leave; flags 1 barrel, 4 eraser
  0x02 cmd u8      1 undo, 2 redo, 3 next screen, 4 active window, 5 preview on, 6 preview off, 7 screenshot
  0x05 u8          1 = the tablet is using this link now, 0 = standby
  0x06 + JSON      hello {"id": tablet id, "name": tablet name}
  0x07             heartbeat
  0x08 w u16 h u16 preview size in device pixels
  0x09 sub ...     remote control (mouse, scroll, text, keys)
  0x0A             the last preview frame is on screen (the PC sends the next one only after this)
PC -> tablet:
  0x10 + JSON      {"pc": name, "auth": "ok"|"pending"|"denied", "cfg": {w, h, name}}
  0x22 + frame     preview patch (USB / Wi-Fi)      0x20 ...  the same, in chunks (Bluetooth)
                   frame = [W, H, x, y, w, h] u16 LE (tablet pixels) + JPEG or WebP
"""
import asyncio
import ctypes
import io
import json
import math
import os
import socket
import struct
import threading
import time

import mss
from PIL import Image, ImageChops

import pen as penlib
import winput

PC_NAME = socket.gethostname()
DATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "TabletPen")
TRUST_FILE = os.path.join(DATA_DIR, "trusted.json")
EV = struct.Struct("<BHHHbbB")
FRAME_HDR = struct.Struct("<6H")       # canvas W, H, patch x, y, w, h (tablet pixels)
DEBUG = bool(os.environ.get("TABLETPEN_DEBUG"))


def pictures_dir():
    """The user's Pictures folder (follows OneDrive / moved folders)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
            return os.path.expandvars(winreg.QueryValueEx(k, "My Pictures")[0])
    except OSError:
        return os.path.join(os.path.expanduser("~"), "Pictures")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class Link:
    """One connection to a tablet (USB, Wi-Fi or Bluetooth). Subclasses implement send / send_frame / close."""
    kind = "?"

    def __init__(self):
        self.tablet_id = None
        self.tablet_name = "tablet"
        self.active = False
        self.preview = True
        self.auth = "new"
        self.acked = asyncio.Event()     # set when the tablet has shown the last frame
        self.acked.set()
        self._ref = None                 # capture the tablet currently shows (None = unknown, send all)
        self._ref_key = None
        self._blurry = False             # Bluetooth: the tablet shows a low-res frame
        self._big_at = 0.0
        self._sending = None
        self._raw = None                 # last capture's raw pixels (cheap "nothing changed" check)
        self._still_since = None

    @property
    def label(self):
        return {"usb": "USB", "wifi": "Wi-Fi", "ble": "Bluetooth"}.get(self.kind, self.kind)

    async def send(self, data: bytes): ...
    async def send_frame(self, jpg: bytes, abort=None) -> bool: ...
    def close(self): ...

    # preview settings per link type: (max width, max height, jpeg quality, grayscale, frame interval s)
    preview_spec = (1280, 800, 65, False, 1 / 12)
    want = None          # (w, h) in device pixels the tablet asked for (0x08)


class Core:
    def __init__(self, on_status=None):
        try:
            self.pen = penlib.Pen()
        except Exception as e:      # older / locked-down Windows: the stylus drives the mouse instead
            log(f"[pen] synthetic pen unavailable ({e}) – using mouse input")
            self.pen = winput.MousePen()
        self.sct = mss.MSS()
        self.diag = {"usb": "starting…", "wifi": "starting…", "ble": "starting…"}
        self.manual_ips = self._load_settings().get("manual_ips", [])
        self.links = set()
        self.on_status = on_status or (lambda text: None)
        self.trusted = self._load_trust()
        self.asking = {}            # tablet id -> future of the approval popup
        self._tls = threading.local()
        self.rect = None
        self.target_name = ""
        self.mon = 1
        self.set_monitor(1)

    # ------------------------------------------------------------ trust
    def _load_trust(self):
        try:
            with open(TRUST_FILE) as f:
                return set(json.load(f))
        except Exception:
            return set()

    @staticmethod
    def _load_settings():
        try:
            with open(os.path.join(DATA_DIR, "settings.json")) as f:
                return json.load(f)
        except Exception:
            return {}

    def add_manual_ip(self, ip):
        ip = ip.strip()
        if ip and ip not in self.manual_ips:
            self.manual_ips.append(ip)
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, "settings.json"), "w") as f:
            json.dump({"manual_ips": self.manual_ips}, f)

    def _save_trust(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(TRUST_FILE, "w") as f:
            json.dump(sorted(self.trusted), f)

    async def _approve(self, link):
        tid = link.tablet_id
        if link.kind == "usb" or tid in self.trusted:
            if tid not in self.trusted:
                self.trusted.add(tid)
                self._save_trust()
            if tid in self.asking:            # plugged in by cable = approved: close the open popup
                self._close_popup()
                fut = self.asking.pop(tid)
                if not fut.done():
                    fut.set_result(True)
            return True
        # a USB link usually shows up within a couple of seconds; give it the chance to approve first
        await asyncio.sleep(3)
        if tid in self.trusted:
            return True
        if tid not in self.asking:
            fut = asyncio.get_running_loop().create_future()
            self.asking[tid] = fut

            def ask():
                MB_YESNO, MB_ICONQUESTION, MB_TOPMOST, MB_SETFOREGROUND = 0x4, 0x20, 0x40000, 0x10000
                r = ctypes.windll.user32.MessageBoxW(
                    None, f'Allow "{link.tablet_name}" to use this PC?\n(pen, mouse, keyboard and media keys)\n\n'
                          f"It connected over {link.label}. You'll only be asked once.",
                    "TabletPen", MB_YESNO | MB_ICONQUESTION | MB_TOPMOST | MB_SETFOREGROUND)
                return r == 6

            async def run():
                ok = await asyncio.to_thread(ask)
                if fut.done():
                    return
                if ok:
                    self.trusted.add(tid)
                    self._save_trust()
                fut.set_result(ok)
                self.asking.pop(tid, None)

            asyncio.get_running_loop().create_task(run())
        return await self.asking[tid]

    @staticmethod
    def _close_popup():
        u = ctypes.windll.user32
        hwnd = u.FindWindowW("#32770", "TabletPen")
        if hwnd:
            u.PostMessageW(hwnd, 0x0111, 6, 0)     # WM_COMMAND, IDYES

    # ------------------------------------------------------------ target area
    def set_monitor(self, i):
        mons = self.sct.monitors
        i = max(1, min(i, len(mons) - 1))
        m = mons[i]
        self.rect = (m["left"], m["top"], m["width"], m["height"])
        self.mon = i
        self.target_name = f"Screen {i}/{len(mons) - 1}"

    def cfg(self, link):
        l, t, w, h = self.rect
        return {"pc": PC_NAME, "auth": "ok" if link.auth == "ok" else link.auth,
                "cfg": {"w": w, "h": h, "name": self.target_name}}

    async def send_cfg(self, link):
        await link.send(b"\x10" + json.dumps(self.cfg(link), separators=(",", ":")).encode())

    async def broadcast_cfg(self):
        for l in list(self.links):
            await self.send_cfg(l)

    # ------------------------------------------------------------ links
    def link_up(self, link):
        self.links.add(link)
        link.last_rx = time.monotonic()
        loop = asyncio.get_running_loop()
        link._tasks = [loop.create_task(self._preview_loop(link)), loop.create_task(self._watchdog(link))]
        self.status()

    async def _watchdog(self, link):
        """The tablet sends a heartbeat every 2 s on every link; a silent link is dead (app closed, out of range)."""
        while True:
            await asyncio.sleep(2)
            if time.monotonic() - link.last_rx > 7:
                log(f"[{link.kind}] no heartbeat – dropping link")
                link.close()
                return

    def link_down(self, link):
        self.links.discard(link)
        for t in getattr(link, "_tasks", []):
            if t is not asyncio.current_task():
                t.cancel()
        if link.active:
            self.pen.leave()
        if link.tablet_id:
            log(f"[{link.kind}] {link.tablet_name} disconnected")
        self.status()

    def status(self):
        ok = [l for l in self.links if l.auth == "ok"]
        act = [l for l in ok if l.active] or ok
        if act:
            self.on_status(f"Connected to {act[0].tablet_name} ({act[0].label})")
        else:
            self.on_status("Waiting for the tablet…")

    async def packet(self, link, data: bytes):
        if not data:
            return
        link.last_rx = time.monotonic()
        t = data[0]
        if t == 0x06:
            try:
                hello = json.loads(data[1:].decode())
            except Exception:
                return
            link.tablet_id = str(hello.get("id", ""))[:64] or None
            link.tablet_name = str(hello.get("name", "tablet"))[:40]
            if not link.tablet_id:
                return
            log(f"[{link.kind}] hello from {link.tablet_name}")
            link.auth = "pending"
            await self.send_cfg(link)
            ok = await self._approve(link)
            link.auth = "ok" if ok else "denied"
            await self.send_cfg(link)
            log(f"[{link.kind}] {link.tablet_name}: {link.auth}")
            self.status()
            return
        if link.auth != "ok":
            return
        if t == 0x05 and len(data) > 1:
            link.active = data[1] == 1
            link._ref = None                 # the tablet starts with an empty preview: send a full frame
            link.acked.set()
            if not link.active:
                self.pen.leave()
            self.status()
        elif t == 0x01:
            if DEBUG:
                log(f"[pen] {link.kind}: {(len(data) - 1) // EV.size} events, kind {data[1]}")
            self._pen(data)
        elif t == 0x02 and len(data) > 1:
            await self._command(link, data[1])
        elif t == 0x08 and len(data) >= 5:
            w, h = struct.unpack_from("<HH", data, 1)
            link.want = (max(160, min(w, 3840)), max(120, min(h, 2160)))
            link._ref = None                 # the tablet cleared its preview: send a full frame
        elif t == 0x09 and len(data) > 1:
            self._remote(data)
        elif t == 0x0A:
            link.acked.set()

    def _remote(self, data):
        """Remote-control mode: touchpad, keyboard, media keys."""
        sub = data[1]
        if sub == 1 and len(data) >= 6:                       # relative mouse move
            dx, dy = struct.unpack_from("<hh", data, 2)
            winput.move(dx, dy)
        elif sub == 2 and len(data) >= 4:                     # button: which, action (0 click 1 down 2 up)
            winput.button(data[2], data[3])
        elif sub == 3 and len(data) >= 6:                     # scroll (wheel units, 120 = one notch)
            dy, dx = struct.unpack_from("<hh", data, 2)
            winput.scroll(dy, dx)
        elif sub == 4:                                        # typed text
            winput.text(data[2:].decode("utf-8", errors="ignore"))
        elif sub == 5 and len(data) > 2:                      # key combination (virtual-key codes)
            winput.keys(*data[2:2 + 6])

    def _pen(self, data):
        p = self.pen
        l, t, w, h = self.rect
        for off in range(1, len(data) - EV.size + 1, EV.size):
            kind, x, y, pr, tx, ty, fl = EV.unpack_from(data, off)
            sx, sy = l + x / 65535 * (w - 1), t + y / 65535 * (h - 1)
            pr /= 65535
            if kind == 0:
                p.hover(sx, sy, tx, ty, fl)
            elif kind == 1:
                p.down(sx, sy, pr, tx, ty, fl)
            elif kind == 2:
                p.move(sx, sy, pr, tx, ty, fl)
            elif kind == 3:
                p.up(sx, sy, fl)
            elif kind == 4:
                p.leave()

    async def _command(self, link, c):
        if c == 1:
            penlib.hotkey(0x11, 0x5A)
        elif c == 2:
            penlib.hotkey(0x11, 0x59)
        elif c == 3:
            if self.target_name.startswith("Screen"):
                nxt = self.mon + 1
                self.set_monitor(1 if nxt >= len(self.sct.monitors) else nxt)
            else:
                self.set_monitor(self.mon)
            await self.broadcast_cfg()
        elif c == 4:
            r = penlib.foreground_client_rect()
            if r:
                self.rect, title = r
                self.target_name = (title or "Window")[:40]
            await self.broadcast_cfg()
        elif c in (5, 6):
            link.preview = c == 5
            link._ref = None
            link.acked.set()
        elif c == 7:
            try:
                path = await asyncio.to_thread(self._save_screenshot)
                msg = f"Screenshot saved on the PC: {os.path.basename(path)}"
                log(f"[shot] {path}")
            except Exception as e:
                msg = "Couldn't save the screenshot on the PC"
                log(f"[shot] failed: {e}")
            await link.send(b"\x10" + json.dumps({"toast": msg}).encode())

    def _save_screenshot(self):
        """Full-resolution PNG of the current target (screen or window) in Pictures\\Screenshots."""
        shot = self._shot()
        folder = os.path.join(pictures_dir(), "Screenshots")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, time.strftime("TabletPen_%Y-%m-%d_%H-%M-%S.png"))
        Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX").save(path, optimize=False, compress_level=3)
        return path

    # ------------------------------------------------------------ preview
    def _shot(self):
        if not hasattr(self._tls, "sct"):
            self._tls.sct = mss.MSS()
        l, t, w, h = self.rect
        return self._tls.sct.grab({"left": l, "top": t, "width": w, "height": h})

    @staticmethod
    def _patch(img, W, H, box, q, gray=False, webp=False, small=None):
        """Encodes the `box` area of the capture `img`, scaled to the tablet's W x H canvas.
        Payload: [W, H, x, y, w, h] (u16, tablet pixels) + JPEG/WebP. Patch edges are snapped to
        tablet pixels, so patches line up exactly with what's already on the tablet."""
        sx, sy = W / img.width, H / img.height
        x0, y0, x1, y1 = box
        tx0, ty0 = max(0, math.floor((x0 - 2) * sx)), max(0, math.floor((y0 - 2) * sy))
        tx1, ty1 = min(W, math.ceil((x1 + 2) * sx)), min(H, math.ceil((y1 + 2) * sy))
        tw, th = max(1, tx1 - tx0), max(1, ty1 - ty0)
        ow, oh = tw, th
        if small and (ow > small[0] or oh > small[1]):
            k = min(small[0] / ow, small[1] / oh)
            ow, oh = max(1, round(ow * k)), max(1, round(oh * k))
        part = img.resize((ow, oh), Image.BILINEAR if gray else Image.LANCZOS,
                          box=(tx0 / sx, ty0 / sy, (tx0 + tw) / sx, (ty0 + th) / sy))
        if gray:
            part = part.convert("L")
        buf = io.BytesIO()
        if webp:
            part.save(buf, "WEBP", quality=q, method=3)
        else:
            part.save(buf, "JPEG", quality=q, optimize=gray, subsampling=0 if q >= 80 else 2)
        return FRAME_HDR.pack(W, H, tx0, ty0, tw, th) + buf.getvalue()

    def _grab(self, link):
        """Returns (payload, is_sharp_refresh), or None when the tablet already shows the screen.

        Only the part of the screen that changed is sent (a click or a typed letter is a few KB).
        USB / Wi-Fi: always sharp. Bluetooth: small changes go out sharp and in color; big ones
        (scrolling, switching apps) go out as a quick low-res gray frame, followed by a sharp
        full frame as soon as the screen settles."""
        maxw, maxh, q, gray, _ = link.preview_spec
        shot = self._shot()
        raw, now = shot.bgra, time.monotonic()
        # fast path: identical capture (a plain byte compare) – skip the image work entirely
        if raw == link._raw and link._ref is not None and not (gray and link._blurry):
            if link._still_since is None:
                link._still_since = now
            return None
        link._raw, link._still_since = raw, None
        img = Image.frombytes("RGB", shot.size, raw, "raw", "BGRX")
        if link.want:
            W, H = link.want
        else:
            k = min(1, maxw / img.width, maxh / img.height)
            W, H = round(img.width * k), round(img.height * k)
        key = (W, H, self.rect)
        full = (0, 0, img.width, img.height)
        ref = link._ref if link._ref_key == key else None
        if ref is not None and ref.size == img.size:
            if gray and link._blurry and now - link._big_at >= 0.5:       # settled: make it sharp
                link._ref, link._blurry, link._sending = img, False, img
                return self._patch(img, W, H, full, 72, webp=True), True
            box = ImageChops.difference(img, ref).getbbox()
            if box is None:
                return None
        else:
            box = full
        area = (box[2] - box[0]) * (box[3] - box[1]) / (img.width * img.height)
        link._ref_key = key
        if not gray:
            link._ref = img
            return self._patch(img, W, H, full if area > 0.6 else box, q), False
        if area <= 0.12:
            data = self._patch(img, W, H, box, 70, webp=True)
            if len(data) <= 48_000:
                link._ref = img
                return data, False
        link._ref, link._blurry, link._big_at = img, True, now
        return self._patch(img, W, H, full, q, gray=True, small=(maxw, maxh)), False

    def _moved_on(self, link):
        """True if a big part of the screen changed since the sharp frame being sent was captured."""
        try:
            shot = self._shot()
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            box = ImageChops.difference(img, link._sending).getbbox()
            return bool(box) and (box[2] - box[0]) * (box[3] - box[1]) > 0.12 * img.width * img.height
        except Exception:
            return False

    async def _preview_loop(self, link):
        while True:
            if link.active and link.preview and link.auth == "ok":
                t0 = time.perf_counter()
                try:
                    got = await asyncio.to_thread(self._grab, link)
                    if got:
                        data, sharp = got
                        abort = (lambda: asyncio.to_thread(self._moved_on, link)) if sharp else None
                        link.acked.clear()
                        t1 = time.perf_counter()
                        ok = await link.send_frame(data, abort=abort) is not False
                        if DEBUG:
                            log(f"[frame] {link.kind} {len(data) // 1024} KB{' sharp' if sharp else ''} "
                                f"{'sent' if ok else 'dropped'} in {(time.perf_counter() - t1) * 1000:.0f} ms")
                        if ok:
                            # wait until the tablet has shown it: frames never pile up, so the
                            # preview stays live and taps/keys aren't stuck behind old frames
                            try:
                                await asyncio.wait_for(link.acked.wait(), 6 if link.kind == "ble" else 2)
                            except asyncio.TimeoutError:
                                link._ref = None          # not sure what the tablet shows: resend all
                        else:
                            link._ref = None
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log(f"[preview] {e}")
                    await asyncio.sleep(1)
                # screen idle for a while: look ~7x a second (saves CPU, still feels instant)
                idle = link._still_since is not None and time.monotonic() - link._still_since > 1
                await asyncio.sleep(max(0.01, (0.15 if idle else link.preview_spec[4]) - (time.perf_counter() - t0)))
            else:
                await asyncio.sleep(0.25)
