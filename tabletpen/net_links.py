"""USB and Wi-Fi links. The tablet app runs a small TCP server (port 8767); the PC connects *out* to it,
so Windows Firewall never has to allow anything.

  Wi-Fi: the PC broadcasts "TABLETPEN?" on UDP 8766; the tablet answers with its id; the PC connects.
         Networks that block broadcasts: add the tablet's IP from the tray ("Connect to tablet by IP…").
  USB:   the PC runs the bundled adb, forwards a local port to the tablet's 8767, and connects to that.
Framing on the TCP stream: [u32 big-endian length][packet].
"""
import asyncio
import json
import os
import socket
import struct
import subprocess
import sys
import time

from core import PC_NAME, Link, log

TABLET_PORT = 8767
DISCOVERY_PORT = 8766
APP_ACTIVITY = "com.ciao.tabletpen/.MainActivity"
HDR = struct.Struct(">I")


def adb_path():
    base = getattr(sys, "_MEIPASS", None)
    cands = [os.path.join(base, "adb", "adb.exe")] if base else []
    here = os.path.dirname(os.path.abspath(__file__))
    cands.append(os.path.join(here, "..", "platform-tools", "adb.exe"))
    for c in cands:
        if os.path.exists(c):
            return c
    return "adb"


ADB = adb_path()
NO_WINDOW = 0x08000000


def adb(*args, timeout=10):
    try:
        r = subprocess.run([ADB, *args], capture_output=True, text=True, timeout=timeout,
                           creationflags=NO_WINDOW)
        return r.returncode, r.stdout + r.stderr
    except Exception as e:
        return 1, str(e)


class TcpLink(Link):
    preview_spec = (1920, 1200, 85, False, 1 / 25)     # sharp; resized to what the tablet asks for,
                                                       # paced by the tablet's frame acks

    def __init__(self, core, kind, reader, writer):
        super().__init__()
        self.core, self.kind, self.r, self.w = core, kind, reader, writer
        self._lock = asyncio.Lock()
        self.closed = False
        self.ip = None

    async def send(self, data: bytes):
        if self.closed:
            return
        async with self._lock:
            try:
                self.w.write(HDR.pack(len(data)) + data)
                await self.w.drain()
            except Exception:
                self.close()

    async def send_frame(self, jpg: bytes, abort=None):
        await self.send(b"\x22" + jpg)
        return not self.closed

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.w.close()
            except Exception:
                pass

    async def run(self):
        self.core.link_up(self)
        try:
            while True:
                n = HDR.unpack(await self.r.readexactly(4))[0]
                if n > 1 << 20:
                    break
                await self.core.packet(self, await self.r.readexactly(n))
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            self.close()
            self.core.link_down(self)


async def open_link(core, kind, host, port):
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 5)
    sock = writer.get_extra_info("socket")
    if sock:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    link = TcpLink(core, kind, reader, writer)
    link.ip = host
    # say hi first: the tablet only lists a connection once it hears from a PC (adb accepts
    # local connections even when the app isn't running, so this also weeds those out)
    await link.send(b"\x10" + json.dumps({"pc": PC_NAME, "auth": "probe"}).encode())
    return link


# ---------------------------------------------------------------- USB
PNP_QUERY = ("(Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -match 'VID_(12D1|339B)' }"
             " | Measure-Object).Count")


def huawei_usb_present():
    """True if a Huawei/Honor USB device is plugged in (used to spot a missing ADB driver)."""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", PNP_QUERY],
                           capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW)
        return int((r.stdout or "0").strip() or 0) > 0
    except Exception:
        return False


async def usb_loop(core, notify):
    """Watches for the tablet on USB; forwards a port and keeps a link open while it's plugged in."""
    links = {}           # serial -> TcpLink
    launched = set()     # serials we already opened the app on during this plug-in
    warned = set()
    driver_check = 0.0
    while True:
        code, out = await asyncio.to_thread(adb, "devices")
        seen = {}
        if code == 0:
            for line in out.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 2:
                    seen[parts[0]] = parts[1]
        else:
            last = out.strip().splitlines()[-1][:70] if out.strip() else ""
            core.diag["usb"] = "adb couldn't start " + last
        for serial in list(launched):
            if serial not in seen:
                launched.discard(serial)
                warned.discard(serial)
        if code == 0 and not seen and time.monotonic() - driver_check > 15:
            # nothing on adb: is the tablet plugged in without a driver?
            driver_check = time.monotonic()
            if await asyncio.to_thread(huawei_usb_present):
                core.diag["usb"] = "tablet plugged in, but Windows has no USB (ADB) driver for it"
                if "driver" not in warned:
                    warned.add("driver")
                    notify("The tablet is plugged in but this PC has no USB driver for it. "
                           "Wi-Fi and Bluetooth still work.")
            else:
                core.diag["usb"] = "no tablet plugged in"
                warned.discard("driver")
        for i, (serial, state) in enumerate(seen.items()):
            if state == "unauthorized":
                core.diag["usb"] = "tap “Allow USB debugging” on the tablet"
                if serial not in warned:
                    warned.add(serial)
                    notify("Tablet plugged in – tap “Allow” on the tablet to use USB.")
                continue
            if state != "device":
                core.diag["usb"] = f"tablet is {state}"
                continue
            l = links.get(serial)
            if l and not l.closed:
                core.diag["usb"] = "connected" if l.auth == "ok" else "waiting for the app"
                continue
            core.diag["usb"] = "tablet found – waiting for the TabletPen app"
            port = 28767 + i
            await asyncio.to_thread(adb, "-s", serial, "forward", f"tcp:{port}", f"tcp:{TABLET_PORT}")
            try:
                link = await open_link(core, "usb", "127.0.0.1", port)
                links[serial] = link
                asyncio.get_running_loop().create_task(link.run())
                log(f"[usb] linked to {serial}")
            except Exception:
                pass
            # open the app on the tablet the first time we see it plugged in
            if serial not in launched:
                launched.add(serial)
                await asyncio.to_thread(adb, "-s", serial, "shell", "am", "start", "-n", APP_ACTIVITY)
        core.status()
        await asyncio.sleep(2)


# ---------------------------------------------------------------- Wi-Fi
def local_ipv4():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    return [i for i in ips if not i.startswith(("127.", "169.254."))]


class _Discovery(asyncio.DatagramProtocol):
    def __init__(self, found):
        self.found = found

    def datagram_received(self, data, addr):
        if data.startswith(b"TABLETPEN!"):
            try:
                info = json.loads(data[10:].decode())
                self.found(addr[0], int(info.get("port", TABLET_PORT)), str(info.get("id", "")))
            except Exception:
                pass


async def wifi_loop(core):
    loop = asyncio.get_running_loop()
    links = {}            # tablet id (or "ip:<addr>") -> TcpLink
    connecting = set()
    last_try = {}

    async def connect(ip, port, key):
        if key in connecting or any(not l.closed and (k == key or l.ip == ip) for k, l in links.items()):
            return
        connecting.add(key)
        last_try[ip] = time.monotonic()
        try:
            link = await open_link(core, "wifi", ip, port)
            links[key] = link
            log(f"[wifi] linked to tablet at {ip}")
            loop.create_task(link.run())
        except Exception as e:
            if key.startswith("ip:"):
                core.diag["wifi_manual"] = f"{ip}: {type(e).__name__}"
        finally:
            connecting.discard(key)

    def found(ip, port, tid):
        if not ip.startswith("127."):
            loop.create_task(connect(ip, port, tid))

    socks = {}
    probe = b"TABLETPEN?" + json.dumps({"pc": PC_NAME}).encode()
    while True:
        nets = local_ipv4()
        # one socket per network interface so the broadcast goes out on every network the PC is on
        for ip in nets:
            if ip not in socks:
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    s.bind((ip, 0))
                    tr, _ = await loop.create_datagram_endpoint(lambda: _Discovery(found), sock=s)
                    socks[ip] = tr
                except Exception:
                    continue
        for ip, tr in list(socks.items()):
            try:
                tr.sendto(probe, ("255.255.255.255", DISCOVERY_PORT))
                parts = ip.split(".")
                tr.sendto(probe, (".".join(parts[:3] + ["255"]), DISCOVERY_PORT))
                for m in core.manual_ips:          # typed-in tablets (networks that block broadcasts)
                    tr.sendto(probe, (m, DISCOVERY_PORT))
            except Exception:
                tr.close()
                socks.pop(ip, None)
        # typed-in addresses: also try TCP straight away, in case UDP is filtered
        for m in core.manual_ips:
            if time.monotonic() - last_try.get(m, 0) > 5:
                loop.create_task(connect(m, TABLET_PORT, "ip:" + m))
        live = [l for l in links.values() if not l.closed]
        if live:
            core.diag["wifi"] = "connected" if any(l.auth == "ok" for l in live) else "found the tablet, connecting"
        elif nets:
            extra = f" and {', '.join(core.manual_ips)}" if core.manual_ips else ""
            core.diag["wifi"] = f"searching on network {', '.join(sorted(nets))}{extra}"
        else:
            core.diag["wifi"] = "this PC isn't on a network"
        await asyncio.sleep(2)
