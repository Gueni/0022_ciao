"""Bluetooth LE link. The tablet advertises the TabletPen GATT service; the PC (central) connects to it.

  INPUT  (notify)                      tablet -> PC packets
  EVENTS (write / write-without-resp.) PC -> tablet packets (preview frames are split into 0x20 chunks)
"""
import asyncio

from bleak import BleakClient, BleakScanner

from core import Link, log

SVC_UUID = "7a1b0001-5e2d-4c0f-9b7a-7ab1e7be0001"
INPUT_UUID = "7a1b0002-5e2d-4c0f-9b7a-7ab1e7be0001"
EVENT_UUID = "7a1b0003-5e2d-4c0f-9b7a-7ab1e7be0001"


class BleLink(Link):
    kind = "ble"
    # small grayscale frames while the screen changes (Bluetooth is slow), sharp color once it's still
    preview_spec = (720, 450, 40, True, 0.15)

    def __init__(self, core, client):
        super().__init__()
        self.core = core
        self.client = client
        self.gone = asyncio.Event()
        self._lock = asyncio.Lock()
        self._fid = 0
        self._q = asyncio.Queue()      # keeps notifications in order

    @property
    def closed(self):
        return self.gone.is_set() or not self.client.is_connected

    async def send(self, data: bytes):
        if self.closed:
            return
        async with self._lock:
            try:
                await asyncio.wait_for(self.client.write_gatt_char(EVENT_UUID, data, response=False), 3)
            except Exception as e:
                log(f"[ble] link lost ({type(e).__name__})")
                self.gone.set()

    async def send_frame(self, jpg: bytes, abort=None):
        n = max(20, min(self.client.mtu_size - 3, 509)) - 6
        total = (len(jpg) + n - 1) // n
        self._fid = (self._fid + 1) & 0xFF
        for i in range(total):
            if self.closed:
                return False
            if abort and i and i % 40 == 0 and await abort():   # screen changed: don't finish a stale frame
                return False
            hdr = bytes([0x20, self._fid]) + i.to_bytes(2, "little") + total.to_bytes(2, "little")
            await self.send(hdr + jpg[i * n:(i + 1) * n])
        return True

    def close(self):
        self.gone.set()

    def on_notify(self, _char, data: bytearray):
        self._q.put_nowait(bytes(data))

    async def pump(self):
        while True:
            await self.core.packet(self, await self._q.get())


def _fast_link(client):
    """Ask Windows for a short connection interval (Windows 11+) for lower pen latency."""
    try:
        from winrt.windows.devices.bluetooth import BluetoothLEPreferredConnectionParameters as P
        return client._backend._requester.request_preferred_connection_parameters(P.throughput_optimized)
    except Exception:
        return None


async def ble_loop(core):
    try:
        from winrt.windows.devices.bluetooth import BluetoothAdapter
        from winrt.windows.devices.radios import RadioState
        ad = await BluetoothAdapter.get_default_async()
        if ad is None or not ad.is_low_energy_supported:
            core.diag["ble"] = "this PC has no Bluetooth LE"
            log("[ble] no Bluetooth LE on this PC")
            return
    except Exception as e:
        core.diag["ble"] = f"unavailable ({type(e).__name__})"
        log(f"[ble] unavailable: {e}")
        return
    errors = 0
    while True:
        try:
            try:
                radio = await ad.get_radio_async()
                if radio is not None and radio.state != RadioState.ON:
                    core.diag["ble"] = "Bluetooth is turned off on this PC"
                    core.status()
                    await asyncio.sleep(3)
                    continue
            except Exception:
                pass
            core.diag["ble"] = "searching for the tablet"
            dev = await BleakScanner.find_device_by_filter(
                lambda d, adv: SVC_UUID in [u.lower() for u in adv.service_uuids], timeout=8)
            if dev is None:
                continue
            core.diag["ble"] = "found the tablet, connecting"
            gone = asyncio.Event()
            client = BleakClient(dev, disconnected_callback=lambda c: gone.set(), timeout=15)
            link = BleLink(core, client)
            link.gone = gone
            await client.connect()
            req = _fast_link(client)  # noqa: F841  (must stay referenced while connected)
            log(f"[ble] linked to tablet {dev.address} (mtu {client.mtu_size})")
            core.diag["ble"] = "connected"
            errors = 0
            core.link_up(link)
            pump = asyncio.get_running_loop().create_task(link.pump())
            watch = asyncio.get_running_loop().create_task(_watch(client, link))
            try:
                await client.start_notify(INPUT_UUID, link.on_notify)
                await link.gone.wait()
            finally:
                pump.cancel()
                watch.cancel()
                core.link_down(link)
                try:
                    await client.disconnect()
                except Exception:
                    pass
        except Exception as e:
            errors += 1
            core.diag["ble"] = f"error: {type(e).__name__}: {str(e)[:60]}"
            log(f"[ble] {type(e).__name__}: {e}")
            await asyncio.sleep(min(30, 2 * errors))


async def _watch(client, link):
    while client.is_connected and not link.gone.is_set():
        await asyncio.sleep(1)
    link.gone.set()
