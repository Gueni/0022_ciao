"""TabletPen for Windows — a tray app. Double-click TabletPen.exe; it finds the tablet by itself
over USB, Wi-Fi or Bluetooth. Run `py main.py --console` to see the log while developing."""
import asyncio
import ctypes
import filecmp
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import winreg

import pystray
from PIL import Image

import pen as penlib

APP = "TabletPen"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
INSTALL_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Programs", APP)
INSTALLED = os.path.join(INSTALL_DIR, APP + ".exe")
SHORTCUT = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs", APP + ".lnk")
NO_WINDOW = 0x08000000


def resource(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def single_instance():
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\TabletPenSingleInstance")
    if ctypes.windll.kernel32.GetLastError() == 183:     # ERROR_ALREADY_EXISTS
        ctypes.windll.user32.MessageBoxW(None, "TabletPen is already running – look for its icon "
                                               "in the system tray.", APP, 0x40)
        sys.exit(0)


def exe_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --autostart'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return f'"{pyw}" "{os.path.abspath(__file__)}" --autostart'


def make_shortcut():
    """Start-menu entry, so TabletPen can be found with the Windows search."""
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:SC);"
          "$s.TargetPath=$env:TG;$s.WorkingDirectory=$env:WD;$s.Description='Tablet pen and remote for Windows';$s.Save()")
    env = dict(os.environ, SC=SHORTCUT, TG=INSTALLED, WD=INSTALL_DIR)
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], env=env, timeout=20,
                   creationflags=NO_WINDOW, capture_output=True)


def self_install():
    """When TabletPen.exe is started from anywhere else (Downloads, a USB stick, the tablet's
    Download folder) it installs itself for this user, with no admin rights needed: it copies itself to
    %LOCALAPPDATA%\\Programs\\TabletPen, adds a Start-menu entry, and starts with Windows (first time
    only, so turning that off in the tray sticks). Then it runs the installed copy. Running a newer
    exe the same way updates the installed one."""
    if not getattr(sys, "frozen", False) or "--portable" in sys.argv:
        return
    me = os.path.abspath(sys.executable)
    if os.path.normcase(me) == os.path.normcase(INSTALLED):
        return
    try:
        first = not os.path.exists(INSTALLED)
        if first or not filecmp.cmp(me, INSTALLED, shallow=False):
            # stop a running copy (other than this process and its one-file launcher) so it can be replaced
            subprocess.run(["taskkill", "/F", "/IM", APP + ".exe", "/FI", f"PID ne {os.getpid()}",
                            "/FI", f"PID ne {os.getppid()}"], creationflags=NO_WINDOW, capture_output=True)
            os.makedirs(INSTALL_DIR, exist_ok=True)
            for _ in range(20):
                try:
                    shutil.copy2(me, INSTALLED)
                    break
                except OSError:
                    time.sleep(0.5)
            else:
                return                              # couldn't install: just run from here
        if not os.path.exists(SHORTCUT):
            make_shortcut()
        if first:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, f'"{INSTALLED}" --autostart')
        subprocess.Popen([INSTALLED], close_fds=True, creationflags=0x00000008)   # DETACHED_PROCESS
        sys.exit(0)
    except SystemExit:
        raise
    except Exception:
        return


def autostart_enabled(_=None):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP)
            return True
    except OSError:
        return False


def set_autostart(on):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, exe_command())
        else:
            try:
                winreg.DeleteValue(k, APP)
            except OSError:
                pass


def ask_ip():
    """Small input box for the tablet's IP address (shown in the tablet's link sheet)."""
    import tkinter as tk
    from tkinter import simpledialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    ip = simpledialog.askstring(APP, "Tablet IP address (shown on the tablet under “link”):", parent=root)
    root.destroy()
    return ip


def main():
    self_install()
    from core import DATA_DIR
    os.makedirs(DATA_DIR, exist_ok=True)
    log_path = os.path.join(DATA_DIR, "log.txt")
    if "--console" not in sys.argv and sys.stdout is None:   # windowed exe: log to a file instead
        sys.stdout = sys.stderr = open(log_path, "w", buffering=1, encoding="utf-8")
    single_instance()
    penlib.make_dpi_aware()

    from core import Core, log
    from net_links import usb_loop, wifi_loop
    from ble_link import ble_loop

    status = {"text": "Starting…"}
    holder = {}
    icon = pystray.Icon(APP, Image.open(resource("icon.png")), APP)

    def set_status(text):
        if text != status["text"]:
            status["text"] = text
            log("[status]", text)
            icon.title = f"{APP} – {text}"[:127]
        try:
            icon.update_menu()
        except Exception:
            pass

    def notify(msg):
        try:
            icon.notify(msg, APP)
        except Exception:
            pass

    loop = asyncio.new_event_loop()

    def guard(coro, name):
        async def run():
            try:
                await coro
            except Exception:
                log(f"[{name}] crashed:\n" + traceback.format_exc())
        return run()

    def run_loop():
        asyncio.set_event_loop(loop)
        try:
            core = Core(on_status=set_status)
        except Exception:
            log("[core] failed to start:\n" + traceback.format_exc())
            set_status("Couldn't start – see log")
            return
        holder["core"] = core
        try:
            from media import Media
            core.media = Media(core)
            loop.create_task(guard(core.media.run(), "media"))
        except Exception:
            log("[media] failed to start:\n" + traceback.format_exc())
        try:
            from paint import Paint
            core.paint = Paint()
        except Exception:
            log("[paint] failed to start:\n" + traceback.format_exc())
        try:
            from appscreen import AppScreen
            if AppScreen.available():
                core.screen = AppScreen(core.settings, core.save_settings)
            else:
                log("[screen] virtual display driver not installed - media screen off")
        except Exception:
            log("[screen] failed to start:\n" + traceback.format_exc())
        try:
            from ytsync import YtSync
            core.yt = YtSync()
        except Exception:
            log("[yt] failed to start:\n" + traceback.format_exc())
        loop.create_task(guard(core.mode_loop(), "mode"))
        core.status()
        loop.create_task(guard(usb_loop(core, notify), "usb"))
        loop.create_task(guard(wifi_loop(core), "wifi"))
        loop.create_task(guard(ble_loop(core), "ble"))
        loop.run_forever()

    threading.Thread(target=run_loop, daemon=True).start()

    def diag(key, label):
        return lambda _: f"{label}: {holder['core'].diag.get(key, '')}" if "core" in holder else f"{label}: …"

    def add_ip(icon_, _):
        def work():
            ip = ask_ip()
            if ip and "core" in holder:
                loop.call_soon_threadsafe(holder["core"].add_manual_ip, ip)
                notify(f"Looking for the tablet at {ip.strip()}…")
        threading.Thread(target=work, daemon=True).start()

    def yt_signin():
        c = holder.get("core")
        if c and c.yt:
            threading.Thread(target=c.yt.show_for_signin, daemon=True).start()

    def screen_off():
        c = holder.get("core")
        if c and c.screen:
            threading.Thread(target=c.screen.unplug, daemon=True).start()

    def quit_app(icon_, _):
        c = holder.get("core")
        if c and c.screen:
            try:
                c.screen.unplug()               # tuck the media-screen apps away before the monitor goes
            except Exception:
                pass
        icon_.stop()
        os._exit(0)

    icon.menu = pystray.Menu(
        pystray.MenuItem(lambda _: status["text"], None, enabled=False),
        pystray.MenuItem(diag("usb", "USB"), None, enabled=False),
        pystray.MenuItem(diag("wifi", "Wi-Fi"), None, enabled=False),
        pystray.MenuItem(diag("ble", "Bluetooth"), None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Connect to tablet by IP…", add_ip),
        pystray.MenuItem("Sign in to YouTube (for the tablet)…", lambda i, _: yt_signin()),
        pystray.MenuItem("Turn off media screen", lambda i, _: screen_off()),
        pystray.MenuItem("Open screenshots folder", lambda i, _: open_shots()),
        pystray.MenuItem("Open log", lambda i, _: os.startfile(log_path)),
        pystray.MenuItem("Start with Windows", lambda i, _: set_autostart(not autostart_enabled()),
                         checked=autostart_enabled),
        pystray.MenuItem("Quit", quit_app),
    )
    def open_shots():
        from core import pictures_dir
        d = os.path.join(pictures_dir(), "Screenshots")
        os.makedirs(d, exist_ok=True)
        os.startfile(d)

    quiet = "--autostart" in sys.argv          # started with Windows: no balloon
    icon.run(setup=lambda i: (setattr(i, "visible", True),
                              None if quiet else notify("TabletPen is running. Open the app on your tablet.")))


if __name__ == "__main__":
    main()
