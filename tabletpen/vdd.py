"""Virtual monitor through the Parsec Virtual Display Driver (installed once, signed by Parsec).

TabletPen plugs a virtual screen in only while the tablet's "media screen" is in use and unplugs it
afterwards, so windows never get stranded on a phantom monitor. The driver drops displays that stop
receiving keep-alive pings, so if TabletPen quits or crashes the screen disappears by itself.
"""
import ctypes
import threading
import time
from ctypes import wintypes

GUID_VDD = "{00b41627-04c4-429e-a26e-0265cf50c8fa}"
IOCTL_ADD, IOCTL_REMOVE, IOCTL_UPDATE, IOCTL_VERSION = 0x0022E004, 0x0022A008, 0x0022A00C, 0x0022E010

setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.windll.user32


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, s):
        s = s.strip("{}").replace("-", "")
        b = bytes.fromhex(s[16:])
        return cls(int(s[:8], 16), int(s[8:12], 16), int(s[12:16], 16), (ctypes.c_ubyte * 8)(*b))


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("InterfaceClassGuid", GUID), ("Flags", wintypes.DWORD),
                ("Reserved", ctypes.c_size_t)]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t), ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]


setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(GUID),
                                                 wintypes.DWORD, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [wintypes.HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
                                                      ctypes.c_void_p, wintypes.DWORD,
                                                      ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                            wintypes.DWORD, wintypes.HANDLE]
k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED)]
k32.GetOverlappedResultEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(wintypes.DWORD),
                                      wintypes.DWORD, wintypes.BOOL]
k32.CreateEventW.restype = wintypes.HANDLE
k32.CloseHandle.argtypes = [wintypes.HANDLE]
INVALID = wintypes.HANDLE(-1).value


def _device_path():
    guid = GUID.parse(GUID_VDD)
    h = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, 0x12)   # PRESENT | DEVICEINTERFACE
    if not h or h == INVALID:
        return None
    try:
        d = SP_DEVICE_INTERFACE_DATA()
        d.cbSize = ctypes.sizeof(d)
        if not setupapi.SetupDiEnumDeviceInterfaces(h, None, ctypes.byref(guid), 0, ctypes.byref(d)):
            return None
        need = wintypes.DWORD()
        setupapi.SetupDiGetDeviceInterfaceDetailW(h, ctypes.byref(d), None, 0, ctypes.byref(need), None)
        buf = ctypes.create_string_buffer(need.value)
        ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0] = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
        if not setupapi.SetupDiGetDeviceInterfaceDetailW(h, ctypes.byref(d), buf, need, None, None):
            return None
        return ctypes.wstring_at(ctypes.addressof(buf) + 4)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(h)


class Vdd:
    def __init__(self):
        self.h = None
        self.lock = threading.Lock()
        self.index = None
        self._ping = None

    @staticmethod
    def installed():
        return _device_path() is not None

    def _open(self):
        if self.h:
            return True
        path = _device_path()
        if not path:
            return False
        h = k32.CreateFileW(path, 0xC0000000, 3, None, 3, 0x80 | 0x20000000 | 0x40000000 | 0x80000000, None)
        if not h or h == INVALID:
            return False
        self.h = h
        return True

    def _ioctl(self, code, data=b""):
        inbuf = ctypes.create_string_buffer(data.ljust(32, b"\0"), 32)
        out = wintypes.DWORD(0)
        ov = OVERLAPPED()
        ov.hEvent = k32.CreateEventW(None, True, False, None)
        try:
            k32.DeviceIoControl(self.h, code, inbuf, 32, ctypes.byref(out), 4, None, ctypes.byref(ov))
            n = wintypes.DWORD()
            if not k32.GetOverlappedResultEx(self.h, ctypes.byref(ov), ctypes.byref(n), 5000, False):
                raise OSError(ctypes.get_last_error(), "virtual display driver didn't answer")
            return out.value
        finally:
            k32.CloseHandle(ov.hEvent)

    def version(self):
        with self.lock:
            return self._ioctl(IOCTL_VERSION) if self._open() else None

    def plug(self):
        """Adds the virtual monitor (if not already there) and keeps it alive. Returns True on success."""
        with self.lock:
            if self.index is not None:
                return True
            if not self._open():
                return False
            self.index = self._ioctl(IOCTL_ADD)
            self._ioctl(IOCTL_UPDATE)
        if not self._ping:
            self._ping = threading.Thread(target=self._keepalive, daemon=True)
            self._ping.start()
        return True

    def unplug(self):
        with self.lock:
            if self.index is None or not self.h:
                return
            i = self.index
            self._ioctl(IOCTL_REMOVE, bytes([(i >> 8) & 0xFF, i & 0xFF]))     # big-endian u16
            self._ioctl(IOCTL_UPDATE)
            self.index = None

    def _keepalive(self):
        while True:
            time.sleep(0.1)
            with self.lock:
                if self.h and self.index is not None:
                    try:
                        self._ioctl(IOCTL_UPDATE)
                    except OSError:
                        pass


# ---------------------------------------------------------------- display configuration (CCD API)
# When a monitor appears, Windows applies its own idea of a layout (e.g. "second screen only"), which
# could blank the real screen. So: remember the real layout before plugging, then apply exactly that
# layout plus the virtual monitor, extended to the right.
class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class PATH_SOURCE(ctypes.Structure):
    _fields_ = [("adapterId", LUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32),
                ("statusFlags", ctypes.c_uint32)]


class PATH_TARGET(ctypes.Structure):
    _fields_ = [("adapterId", LUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32),
                ("outputTechnology", ctypes.c_uint32), ("rotation", ctypes.c_uint32),
                ("scaling", ctypes.c_uint32), ("refreshNum", ctypes.c_uint32), ("refreshDen", ctypes.c_uint32),
                ("scanLineOrdering", ctypes.c_uint32), ("targetAvailable", wintypes.BOOL),
                ("statusFlags", ctypes.c_uint32)]


class PATH_INFO(ctypes.Structure):
    _fields_ = [("sourceInfo", PATH_SOURCE), ("targetInfo", PATH_TARGET), ("flags", ctypes.c_uint32)]


class SOURCE_MODE(ctypes.Structure):
    _fields_ = [("width", ctypes.c_uint32), ("height", ctypes.c_uint32), ("pixelFormat", ctypes.c_uint32),
                ("x", ctypes.c_long), ("y", ctypes.c_long)]


class MODE_UNION(ctypes.Union):
    _fields_ = [("sourceMode", SOURCE_MODE), ("raw", ctypes.c_byte * 48)]


class MODE_INFO(ctypes.Structure):
    _fields_ = [("infoType", ctypes.c_uint32), ("id", ctypes.c_uint32), ("adapterId", LUID), ("u", MODE_UNION)]


class TARGET_NAME(ctypes.Structure):        # DISPLAYCONFIG_TARGET_DEVICE_NAME
    _fields_ = [("type", ctypes.c_uint32), ("size", ctypes.c_uint32), ("adapterId", LUID), ("id", ctypes.c_uint32),
                ("flags", ctypes.c_uint32), ("outputTechnology", ctypes.c_uint32),
                ("edidManufactureId", ctypes.c_uint16), ("edidProductCodeId", ctypes.c_uint16),
                ("connectorInstance", ctypes.c_uint32), ("monitorFriendlyDeviceName", wintypes.WCHAR * 64),
                ("monitorDevicePath", wintypes.WCHAR * 128)]


QDC_ALL_PATHS, QDC_ONLY_ACTIVE_PATHS = 1, 2
SDC_APPLY, SDC_USE_SUPPLIED, SDC_SAVE, SDC_ALLOW_CHANGES = 0x80, 0x20, 0x200, 0x400
PATH_ACTIVE, MODE_SOURCE, MODE_TARGET, INVALID_IDX = 1, 1, 2, 0xFFFFFFFF


def query(flags):
    np_, nm = ctypes.c_uint32(), ctypes.c_uint32()
    for _ in range(5):
        if user32.GetDisplayConfigBufferSizes(flags, ctypes.byref(np_), ctypes.byref(nm)):
            return [], []
        paths, modes_ = (PATH_INFO * np_.value)(), (MODE_INFO * nm.value)()
        r = user32.QueryDisplayConfig(flags, ctypes.byref(np_), paths, ctypes.byref(nm), modes_, None)
        if r == 0:
            return list(paths[:np_.value]), list(modes_[:nm.value])
        if r != 122:                                                      # ERROR_INSUFFICIENT_BUFFER
            return [], []
    return [], []


def target_path(t):
    n = TARGET_NAME()
    n.type, n.size, n.adapterId, n.id = 2, ctypes.sizeof(n), t.adapterId, t.id
    user32.DisplayConfigGetDeviceInfo(ctypes.byref(n))
    return n.monitorDevicePath.lower()


def is_virtual(path):
    return "psccdd" in target_path(path.targetInfo)


def real_layout():
    """The active layout of the real (non-virtual) screens: (paths, modes) ready to re-apply."""
    paths, modes_ = query(QDC_ONLY_ACTIVE_PATHS)
    return [p for p in paths if not is_virtual(p)], modes_


def apply_with_virtual(saved, w, h):
    """Re-applies the saved real layout and adds the virtual monitor (w x h) to the right of it."""
    rpaths, rmodes = saved
    allp, _ = query(QDC_ALL_PATHS)
    vp = next((p for p in allp if is_virtual(p) and p.targetInfo.targetAvailable), None)
    if vp is None or not rpaths:
        return False
    used = {(p.sourceInfo.adapterId.LowPart, p.sourceInfo.id) for p in rpaths}
    if (vp.sourceInfo.adapterId.LowPart, vp.sourceInfo.id) in used:      # a source already in use: pick another
        cand = [p for p in allp if is_virtual(p) and (p.sourceInfo.adapterId.LowPart, p.sourceInfo.id) not in used]
        if not cand:
            return False
        vp = cand[0]
    right = max((m.u.sourceMode.x + m.u.sourceMode.width for m in rmodes if m.infoType == MODE_SOURCE), default=0)
    modes_ = list(rmodes)
    sm = MODE_INFO()
    sm.infoType, sm.id, sm.adapterId = MODE_SOURCE, vp.sourceInfo.id, vp.sourceInfo.adapterId
    sm.u.sourceMode = SOURCE_MODE(w, h, 4, right, 0)                      # 32 bpp
    modes_.append(sm)
    v = PATH_INFO.from_buffer_copy(vp)
    v.flags = PATH_ACTIVE
    v.sourceInfo.modeInfoIdx = len(modes_) - 1
    v.targetInfo.modeInfoIdx = INVALID_IDX
    paths = list(rpaths) + [v]
    pa, ma = (PATH_INFO * len(paths))(*paths), (MODE_INFO * len(modes_))(*modes_)
    r = user32.SetDisplayConfig(len(paths), pa, len(modes_), ma, SDC_APPLY | SDC_USE_SUPPLIED | SDC_ALLOW_CHANGES | SDC_SAVE)
    return r == 0


# ---------------------------------------------------------------- finding & sizing the virtual monitor
class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32), ("DeviceString", wintypes.WCHAR * 128),
                ("StateFlags", wintypes.DWORD), ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128)]


class DEVMODEW(ctypes.Structure):
    _fields_ = [("dmDeviceName", wintypes.WCHAR * 32), ("dmSpecVersion", wintypes.WORD),
                ("dmDriverVersion", wintypes.WORD), ("dmSize", wintypes.WORD), ("dmDriverExtra", wintypes.WORD),
                ("dmFields", wintypes.DWORD), ("dmPositionX", wintypes.LONG), ("dmPositionY", wintypes.LONG),
                ("dmDisplayOrientation", wintypes.DWORD), ("dmDisplayFixedOutput", wintypes.DWORD),
                ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short), ("dmYResolution", ctypes.c_short),
                ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short), ("dmFormName", wintypes.WCHAR * 32),
                ("dmLogPixels", wintypes.WORD), ("dmBitsPerPel", wintypes.DWORD), ("dmPelsWidth", wintypes.DWORD),
                ("dmPelsHeight", wintypes.DWORD), ("dmDisplayFlags", wintypes.DWORD),
                ("dmDisplayFrequency", wintypes.DWORD), ("dmICMMethod", wintypes.DWORD),
                ("dmICMIntent", wintypes.DWORD), ("dmMediaType", wintypes.DWORD), ("dmDitherType", wintypes.DWORD),
                ("dmReserved1", wintypes.DWORD), ("dmReserved2", wintypes.DWORD),
                ("dmPanningWidth", wintypes.DWORD), ("dmPanningHeight", wintypes.DWORD)]


def virtual_display_name():
    """\\\\.\\DISPLAYn of the Parsec virtual monitor, if it's plugged in."""
    i = 0
    while True:
        d = DISPLAY_DEVICEW()
        d.cb = ctypes.sizeof(d)
        if not user32.EnumDisplayDevicesW(None, i, ctypes.byref(d), 0):
            return None
        if "parsec" in d.DeviceString.lower() and d.StateFlags & 1:      # ATTACHED_TO_DESKTOP
            return d.DeviceName
        i += 1


def display_rect(name):
    dm = DEVMODEW()
    dm.dmSize = ctypes.sizeof(dm)
    if not user32.EnumDisplaySettingsW(name, -1, ctypes.byref(dm)):           # ENUM_CURRENT_SETTINGS
        return None
    return dm.dmPositionX, dm.dmPositionY, dm.dmPelsWidth, dm.dmPelsHeight


def modes(name):
    out, i = set(), 0
    while True:
        dm = DEVMODEW()
        dm.dmSize = ctypes.sizeof(dm)
        if not user32.EnumDisplaySettingsW(name, i, ctypes.byref(dm)):
            return sorted(out)
        out.add((dm.dmPelsWidth, dm.dmPelsHeight))
        i += 1


def set_mode(name, w, h, place_right_of=None):
    """Resolution of the virtual monitor (and put it to the right of the main screen)."""
    dm = DEVMODEW()
    dm.dmSize = ctypes.sizeof(dm)
    user32.EnumDisplaySettingsW(name, -1, ctypes.byref(dm))
    dm.dmPelsWidth, dm.dmPelsHeight = w, h
    dm.dmFields = 0x80000 | 0x100000                                        # PELSWIDTH | PELSHEIGHT
    if place_right_of is not None:
        dm.dmPositionX, dm.dmPositionY = place_right_of, 0
        dm.dmFields |= 0x20                                                  # POSITION
    r = user32.ChangeDisplaySettingsExW(name, ctypes.byref(dm), None, 0x1 | 0x4, None)  # UPDATEREGISTRY|NORESET
    user32.ChangeDisplaySettingsExW(None, None, None, 0, None)               # apply
    return r
