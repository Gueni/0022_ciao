"""Windows synthetic pen injection (InjectSyntheticPointerInput, Win10 1809+)."""
import ctypes
from ctypes import wintypes as w

user32 = ctypes.WinDLL("user32", use_last_error=True)

PT_PEN = 3
POINTER_FEEDBACK_DEFAULT = 1

F_NEW, F_INRANGE, F_INCONTACT = 0x1, 0x2, 0x4
F_FIRSTBUTTON, F_PRIMARY, F_CONFIDENCE = 0x10, 0x2000, 0x4000
F_DOWN, F_UPDATE, F_UP = 0x10000, 0x20000, 0x40000

PEN_FLAG_BARREL, PEN_FLAG_INVERTED, PEN_FLAG_ERASER = 1, 2, 4
PEN_MASK_PRESSURE, PEN_MASK_TILT_X, PEN_MASK_TILT_Y = 1, 4, 8

CHANGE_NONE, CHANGE_FIRST_DOWN, CHANGE_FIRST_UP = 0, 1, 2


class POINTER_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerType", ctypes.c_uint32), ("pointerId", ctypes.c_uint32),
        ("frameId", ctypes.c_uint32), ("pointerFlags", ctypes.c_uint32),
        ("sourceDevice", w.HANDLE), ("hwndTarget", w.HWND),
        ("ptPixelLocation", w.POINT), ("ptHimetricLocation", w.POINT),
        ("ptPixelLocationRaw", w.POINT), ("ptHimetricLocationRaw", w.POINT),
        ("dwTime", w.DWORD), ("historyCount", ctypes.c_uint32),
        ("InputData", ctypes.c_int32), ("dwKeyStates", w.DWORD),
        ("PerformanceCount", ctypes.c_uint64), ("ButtonChangeType", ctypes.c_int),
    ]


class POINTER_PEN_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerInfo", POINTER_INFO), ("penFlags", ctypes.c_uint32),
        ("penMask", ctypes.c_uint32), ("pressure", ctypes.c_uint32),
        ("rotation", ctypes.c_uint32), ("tiltX", ctypes.c_int32), ("tiltY", ctypes.c_int32),
    ]


class POINTER_TOUCH_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerInfo", POINTER_INFO), ("touchFlags", ctypes.c_uint32),
        ("touchMask", ctypes.c_uint32), ("rcContact", w.RECT), ("rcContactRaw", w.RECT),
        ("orientation", ctypes.c_uint32), ("pressure", ctypes.c_uint32),
    ]


class _U(ctypes.Union):
    _fields_ = [("touchInfo", POINTER_TOUCH_INFO), ("penInfo", POINTER_PEN_INFO)]


class POINTER_TYPE_INFO(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_uint32), ("u", _U)]


user32.CreateSyntheticPointerDevice.restype = w.HANDLE
user32.CreateSyntheticPointerDevice.argtypes = [ctypes.c_uint32, ctypes.c_ulong, ctypes.c_uint32]
user32.InjectSyntheticPointerInput.restype = w.BOOL
user32.InjectSyntheticPointerInput.argtypes = [w.HANDLE, ctypes.POINTER(POINTER_TYPE_INFO), ctypes.c_uint32]


def make_dpi_aware():
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2
    except Exception:
        pass


def foreground_client_rect():
    """Screen rect (left, top, width, height) of the foreground window's client area."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    rc = w.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rc))
    pt = w.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    if rc.right < 50 or rc.bottom < 50:
        return None
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return (pt.x, pt.y, rc.right, rc.bottom), buf.value


class Pen:
    def __init__(self):
        self.dev = user32.CreateSyntheticPointerDevice(PT_PEN, 1, POINTER_FEEDBACK_DEFAULT)
        if not self.dev:
            raise OSError(ctypes.get_last_error(), "CreateSyntheticPointerDevice failed")
        self.in_range = False
        self.in_contact = False
        self.last = (0, 0)
        self._warned = False

    def _send(self, x, y, flags, pressure=0, tx=0, ty=0, pen_flags=0, change=CHANGE_NONE):
        info = POINTER_TYPE_INFO()
        info.type = PT_PEN
        p = info.penInfo
        pi = p.pointerInfo
        pi.pointerType = PT_PEN
        pi.pointerId = 0
        pi.pointerFlags = flags
        pi.ptPixelLocation = w.POINT(int(x), int(y))
        pi.ButtonChangeType = change
        p.penFlags = pen_flags
        p.penMask = PEN_MASK_PRESSURE | PEN_MASK_TILT_X | PEN_MASK_TILT_Y
        p.pressure = max(0, min(1024, int(pressure * 1024)))
        p.tiltX = max(-90, min(90, int(tx)))
        p.tiltY = max(-90, min(90, int(ty)))
        if not user32.InjectSyntheticPointerInput(self.dev, ctypes.byref(info), 1):
            if not self._warned:
                self._warned = True
                print(f"[pen] inject failed, error {ctypes.get_last_error()}")
        self.last = (x, y)

    def hover(self, x, y, tx=0, ty=0, pen_flags=0):
        if self.in_contact:
            self.up(x, y, pen_flags)
        flags = F_INRANGE | F_UPDATE | F_PRIMARY | F_CONFIDENCE
        if not self.in_range:
            flags |= F_NEW
        self.in_range = True
        self._send(x, y, flags, 0, tx, ty, pen_flags)

    def down(self, x, y, pressure, tx=0, ty=0, pen_flags=0):
        if not self.in_range:
            self.hover(x, y, tx, ty, pen_flags)
        if self.in_contact:
            return self.move(x, y, pressure, tx, ty, pen_flags)
        self.in_contact = True
        self._send(x, y, F_INRANGE | F_INCONTACT | F_FIRSTBUTTON | F_DOWN | F_PRIMARY | F_CONFIDENCE,
                   pressure, tx, ty, pen_flags, CHANGE_FIRST_DOWN)

    def move(self, x, y, pressure, tx=0, ty=0, pen_flags=0):
        if not self.in_contact:
            return self.down(x, y, pressure, tx, ty, pen_flags)
        self._send(x, y, F_INRANGE | F_INCONTACT | F_FIRSTBUTTON | F_UPDATE | F_PRIMARY | F_CONFIDENCE,
                   pressure, tx, ty, pen_flags)

    def up(self, x=None, y=None, pen_flags=0):
        if not self.in_contact:
            return
        x, y = (x, y) if x is not None else self.last
        self.in_contact = False
        self._send(x, y, F_INRANGE | F_UP | F_PRIMARY | F_CONFIDENCE, 0, 0, 0, pen_flags, CHANGE_FIRST_UP)

    def leave(self):
        if self.in_contact:
            self.up()
        if self.in_range:
            self.in_range = False
            self._send(*self.last, F_UPDATE | F_PRIMARY | F_CONFIDENCE)


KEYEVENTF_KEYUP = 2


def hotkey(*vks):
    for vk in vks:
        user32.keybd_event(vk, 0, 0, 0)
    for vk in reversed(vks):
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
