"""Paint mode: the tablet becomes a touch-friendly Paint, and everything happens live in Windows Paint.

Strokes go in as real pen input (pressure included) on Paint's canvas. Tools, colors, shapes and the
brush size are set by pressing Paint's own buttons through UI Automation, using a small PowerShell
helper (Windows' built-in UI Automation, no extra packages), so the PC shows exactly what you pick.
"""
import json
import os
import subprocess
import threading
import time
import ctypes
from ctypes import wintypes as w

from core import DATA_DIR, log

NO_WINDOW = 0x08000000
user32 = ctypes.windll.user32

HELPER = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$AE = [System.Windows.Automation.AutomationElement]
$TS = [System.Windows.Automation.TreeScope]
$CT = [System.Windows.Automation.ControlType]
function Win {
  $p = Get-Process mspaint -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
  if ($p) { $AE::FromHandle($p.MainWindowHandle) } else { $null }
}
function Cond($name, $type) {
  $c = New-Object System.Windows.Automation.PropertyCondition($AE::NameProperty, $name)
  if ($type) {
    $t = New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, $CT::$type)
    $c = New-Object System.Windows.Automation.AndCondition($c, $t)
  }
  $c
}
function ById($root, $id) {
  $root.FindFirst($TS::Descendants, (New-Object System.Windows.Automation.PropertyCondition($AE::AutomationIdProperty, $id)))
}
function Press($el) {
  $p = $null
  if ($el.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$p)) { $p.Select(); return 'select' }
  if ($el.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$p)) { $p.Invoke(); return 'invoke' }
  if ($el.TryGetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern, [ref]$p)) {
    if ($p.Current.ToggleState -ne [System.Windows.Automation.ToggleState]::On) { $p.Toggle() }
    return 'toggle'
  }
  return 'none'
}
function Rect($el) { $r = $el.Current.BoundingRectangle; @([int]$r.X, [int]$r.Y, [int]$r.Width, [int]$r.Height) }
$stdin = [Console]::In
while ($true) {
  $line = $stdin.ReadLine()
  if ($line -eq $null) { break }
  try {
    $c = $line | ConvertFrom-Json
    $win = Win
    if (-not $win) { $out = @{ ok = $false; err = 'paint' } }
    elseif ($c.a -eq 'press') {
      $el = $win.FindFirst($TS::Descendants, (Cond $c.n $c.t))
      if ($el) { $out = @{ ok = $true; how = (Press $el) } } else { $out = @{ ok = $false; err = 'notfound' } }
    }
    elseif ($c.a -eq 'size') {
      $el = $win.FindFirst($TS::Descendants, (Cond 'Size' 'Slider'))
      $p = $el.GetCurrentPattern([System.Windows.Automation.RangeValuePattern]::Pattern)
      $v = [Math]::Max($p.Current.Minimum, [Math]::Min($p.Current.Maximum, [double]$c.v))
      $p.SetValue($v)
      $out = @{ ok = $true; v = $v; min = $p.Current.Minimum; max = $p.Current.Maximum }
    }
    elseif ($c.a -eq 'canvas') {
      $img = ById $win 'image'; $view = ById $win 'scrollViewer'
      if ($img -and $view) { $out = @{ ok = $true; img = (Rect $img); view = (Rect $view) } } else { $out = @{ ok = $false; err = 'canvas' } }
    }
    else { $out = @{ ok = $false; err = 'cmd' } }
  } catch { $out = @{ ok = $false; err = $_.Exception.Message } }
  [Console]::Out.WriteLine(($out | ConvertTo-Json -Compress))
  [Console]::Out.Flush()
}
"""


class Paint:
    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()
        self.rect = None

    # ---- the UI Automation helper
    def _helper(self):
        if self.proc and self.proc.poll() is None:
            return self.proc
        path = os.path.join(DATA_DIR, "paint_helper.ps1")
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8-sig") as f:
            f.write(HELPER)
        self.proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            encoding="utf-8", creationflags=NO_WINDOW, bufsize=1)
        return self.proc

    def ask(self, **cmd):
        """Blocking call into the helper; returns its JSON answer (or {'ok': False})."""
        with self.lock:
            for attempt in (0, 1):
                try:
                    p = self._helper()
                    p.stdin.write(json.dumps(cmd) + "\n")
                    p.stdin.flush()
                    line = p.stdout.readline()
                    if line:
                        return json.loads(line)
                except Exception as e:
                    log(f"[paint] helper: {e}")
                try:
                    self.proc and self.proc.kill()
                except Exception:
                    pass
                self.proc = None
        return {"ok": False}

    # ---- Paint itself
    @staticmethod
    def window():
        from media import processes, windows_of
        pids = {p for p, _, e in processes() if e.lower() == "mspaint.exe"}
        wins = windows_of(pids)
        return wins[0] if wins else None

    def open(self):
        """Starts Paint if needed, brings it to the front maximized; returns the canvas rect."""
        h = self.window()
        if not h:
            subprocess.Popen(["cmd", "/c", "start", "", "mspaint"], creationflags=NO_WINDOW)
            for _ in range(50):
                time.sleep(0.2)
                h = self.window()
                if h:
                    break
        if not h:
            raise RuntimeError("Paint didn't open")
        user32.ShowWindow(h, 3)                                  # SW_MAXIMIZE
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 2, 0)
        user32.SetForegroundWindow(h)
        time.sleep(0.4)
        self.ask(a="press", n="Fit to window", t="Button")
        time.sleep(0.3)
        return self.canvas()

    def canvas(self):
        """Visible part of the canvas on screen (x, y, w, h)."""
        r = self.ask(a="canvas")
        if not r.get("ok"):
            return self.rect
        ix, iy, iw, ih = r["img"]
        vx, vy, vw, vh = r["view"]
        x0, y0 = max(ix, vx), max(iy, vy)
        x1, y1 = min(ix + iw, vx + vw), min(iy + ih, vy + vh)
        if x1 - x0 > 20 and y1 - y0 > 20:
            self.rect = (x0, y0, x1 - x0, y1 - y0)
        return self.rect

    def command(self, req):
        """From the tablet: {"a": "tool"|"shape"|"color"|"size"|"undo"|"redo"|"clear"|"save", ...}"""
        a = req.get("a")
        if a == "tool":
            n = str(req.get("n", ""))
            kind = "RadioButton" if n in ("Brushes", "Select") else "Button"
            return self.ask(a="press", n=n, t=kind)
        if a == "shape":
            return self.ask(a="press", n=str(req.get("n", "")), t="Button")
        if a == "color":
            return self.ask(a="press", n=str(req.get("n", "")), t="ListItem")
        if a == "size":
            return self.ask(a="size", v=float(req.get("v", 1)))
        if a in ("undo", "redo", "save"):
            return self.ask(a="press", n=a.capitalize(), t="Button")
        if a == "clear":                                         # select all + delete, in Paint
            h = self.window()
            if h:
                user32.SetForegroundWindow(h)
                time.sleep(0.1)
                from winput import keys
                keys(0x11, 0x41)
                time.sleep(0.15)
                keys(0x2E)
            return {"ok": True}
        if a == "fit":
            return self.ask(a="press", n="Fit to window", t="Button")
        return {"ok": False}
