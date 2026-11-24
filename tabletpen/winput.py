"""Mouse / keyboard injection with SendInput (remote-control mode, and a pen fallback)."""
import ctypes
from ctypes import wintypes as w

user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOVE, LDOWN, LUP, RDOWN, RUP, MDOWN, MUP = 0x1, 0x2, 0x4, 0x8, 0x10, 0x20, 0x40
WHEEL, HWHEEL, ABSOLUTE, VIRTUALDESK = 0x800, 0x1000, 0x8000, 0x4000
KEYUP, UNICODE, EXTENDED = 0x2, 0x4, 0x1

# keys that need the "extended" flag to not be read as their numpad twins
EXTENDED_VK = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C, 0x5D,
               0xA3, 0xA5, 0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3}


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD), ("dwFlags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD), ("time", w.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", w.DWORD), ("wParamL", w.WORD), ("wParamH", w.WORD)]


class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", w.DWORD), ("u", _U)]


user32.SendInput.argtypes = [w.UINT, ctypes.POINTER(INPUT), ctypes.c_int]


def _send(items):
    arr = (INPUT * len(items))(*items)
    user32.SendInput(len(items), arr, ctypes.sizeof(INPUT))


def _mouse(flags, dx=0, dy=0, data=0):
    i = INPUT(type=INPUT_MOUSE)
    i.mi = MOUSEINPUT(dx, dy, ctypes.c_ulong(data & 0xFFFFFFFF).value, flags, 0, 0)
    return i


def _key(vk=0, scan=0, flags=0):
    i = INPUT(type=INPUT_KEYBOARD)
    if vk in EXTENDED_VK:
        flags |= EXTENDED
    i.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    return i


# ---------------------------------------------------------------- mouse
def move(dx, dy):
    _send([_mouse(MOVE, int(dx), int(dy))])


BTN = {1: (LDOWN, LUP), 2: (RDOWN, RUP), 3: (MDOWN, MUP)}


def button(b, action):
    """action 0 click, 1 down, 2 up."""
    down, up = BTN.get(b, BTN[1])
    if action == 1:
        _send([_mouse(down)])
    elif action == 2:
        _send([_mouse(up)])
    else:
        _send([_mouse(down), _mouse(up)])


def scroll(dy, dx=0):
    items = []
    if dy:
        items.append(_mouse(WHEEL, data=int(dy)))
    if dx:
        items.append(_mouse(HWHEEL, data=int(dx)))
    if items:
        _send(items)


def move_abs(x, y):
    """Absolute move in virtual-desktop pixels."""
    vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    vw, vh = max(1, user32.GetSystemMetrics(78) - 1), max(1, user32.GetSystemMetrics(79) - 1)
    nx = int((x - vx) * 65535 / vw)
    ny = int((y - vy) * 65535 / vh)
    return _mouse(MOVE | ABSOLUTE | VIRTUALDESK, nx, ny)


# ---------------------------------------------------------------- keyboard
def keys(*vks):
    """Press a combination (e.g. Ctrl+C) and release in reverse order."""
    _send([_key(v) for v in vks] + [_key(v, flags=KEYUP) for v in reversed(vks)])


def text(s):
    items = []
    for ch in s:
        if ch == "\n":
            items += [_key(0x0D), _key(0x0D, flags=KEYUP)]
            continue
        b = ch.encode("utf-16-le")
        for k in range(0, len(b), 2):
            cu = int.from_bytes(b[k:k + 2], "little")
            items += [_key(0, cu, UNICODE), _key(0, cu, UNICODE | KEYUP)]
    if items:
        _send(items)


# ---------------------------------------------------------------- pen fallback
class MousePen:
    """Used when Windows can't create a synthetic pen (older Windows 10, locked-down PCs):
    the stylus drives the mouse instead – no pressure, but drawing still works."""

    def __init__(self):
        self.down_ = False

    def hover(self, x, y, *a):
        if self.down_:
            self.up(x, y)
        _send([move_abs(x, y)])

    def down(self, x, y, *a):
        _send([move_abs(x, y), _mouse(LDOWN)])
        self.down_ = True

    def move(self, x, y, *a):
        _send([move_abs(x, y)])
        if not self.down_:
            self.down(x, y)

    def up(self, x=None, y=None, *a):
        if self.down_:
            self.down_ = False
            _send([_mouse(LUP)])

    def leave(self):
        self.up()
