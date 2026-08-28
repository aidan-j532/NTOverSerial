import os
import struct
import sys
import threading
import time

import usb.core
from aoa.core import find_accessory, find_device, toggle_accessory_mode

ACCESSORY_VID = 0x18D1
ACCESSORY_PIDS = (0x2D00, 0x2D01)

# Must match the Android app's res/xml/accessory_filter.xml (manufacturer/model).
MANUFACTURER = "NTOverSerial"
MODEL = "Adapter"
DESCRIPTION = "NT over USB"
VERSION = "1.0"
URI = ""
SERIAL = ""

_HEADER = struct.Struct("<I")
_backend_ready = False
_backend_lock = threading.Lock()


def _find_libusb_dll():
    candidates = [
        os.environ.get("LIBUSB_DLL_PATH"),
        os.path.join(sys.prefix, "Lib", "site-packages", "libusb", "_platform", "windows", "x86_64", "libusb-1.0.dll"),
        os.path.join(sys.prefix, "Lib", "site-packages", "libusb", "_platform", "windows", "arm64", "libusb-1.0.dll"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def _ensure_backend():
    global _backend_ready
    if _backend_ready:
        return
    with _backend_lock:
        if _backend_ready:
            return
        dll = _find_libusb_dll()
        if dll:
            dll_dir = os.path.dirname(dll)
            os.environ["PATH"] = dll_dir + os.pathsep + os.environ.get("PATH", "")
            if hasattr(os, "add_dll_directory"):
                try:
                    os.add_dll_directory(dll_dir)
                except Exception:
                    pass
        try:
            usb.core.find(find_all=True)
        except usb.core.NoBackendError:
            raise RuntimeError("libusb backend could not be loaded (libusb DLL not found)")
        _backend_ready = True


def _has_vendor_interface(dev):
    try:
        for cfg in dev.configs():
            for intf in cfg.interfaces():
                if intf.bInterfaceClass == 0xFF:
                    return True
    except Exception:
        pass
    return False


def _is_candidate(dev):
    if dev.idVendor == ACCESSORY_VID and dev.idProduct in ACCESSORY_PIDS:
        return True
    try:
        man = (dev.manufacturer or "").lower()
        prod = (dev.product or "").lower()
    except Exception:
        man = prod = ""
    if "essential" in man or "essential" in prod or "android" in man or "android" in prod:
        return True
    return _has_vendor_interface(dev)


def find_devices():
    _ensure_backend()
    devices = []
    try:
        for dev in usb.core.find(find_all=True):
            if _is_candidate(dev):
                devices.append(dev)
    except Exception:
        pass
    return devices


def device_label(dev):
    man = prod = ""
    try:
        man = dev.manufacturer or ""
    except Exception:
        pass
    try:
        prod = dev.product or ""
    except Exception:
        pass
    name = (man + " " + prod).strip()
    return f"{dev.idVendor:04x}:{dev.idProduct:04x} {name}".strip()


class AndroidAccessory:
    def __init__(self, vidpid=None):
        self._vidpid = vidpid
        self._dev = None
        self._in_ep = None
        self._out_ep = None
        self._rlock = threading.Lock()
        self._wlock = threading.Lock()
        self.connected = False

    @property
    def device(self):
        return self._dev

    def connect(self, max_wait=5.0):
        _ensure_backend()
        dev = self._find_accessory_now()
        if dev is None:
            dev = self._find_normal_now()
            if dev is not None:
                self._toggle_mode(dev)
            dev = self._wait_for_accessory(max_wait)
        if dev is None:
            raise RuntimeError("Could not enter Android accessory mode. Is the phone plugged in and unlocked?")
        self._dev = dev
        self._discover_endpoints()
        self.connected = True

    def close(self):
        self.connected = False
        self._in_ep = None
        self._out_ep = None
        self._dev = None

    def send(self, data):
        frame = _HEADER.pack(len(data)) + bytes(data)
        with self._wlock:
            self._write_all(frame)

    def receive(self, timeout=0.2):
        with self._rlock:
            header = self._read_exact(4, timeout, allow_idle=True)
            if header is None:
                return None
            (length,) = _HEADER.unpack(header)
            if length == 0:
                return b""
            return self._read_exact(length, timeout)

    def _find_accessory_now(self):
        try:
            return find_accessory()
        except Exception:
            return None

    def _find_normal_now(self):
        if self._vidpid:
            try:
                return find_device([self._vidpid])
            except Exception:
                pass
        return None

    def _toggle_mode(self, dev):
        try:
            toggle_accessory_mode(dev, MANUFACTURER, MODEL, DESCRIPTION, VERSION, URI, SERIAL)
        except TypeError:
            toggle_accessory_mode(dev, MANUFACTURER.encode(), MODEL.encode(), DESCRIPTION.encode(), VERSION.encode(), URI.encode(), SERIAL.encode())

    def _wait_for_accessory(self, max_wait):
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            dev = self._find_accessory_now()
            if dev is not None:
                return dev
            time.sleep(0.1)
        return None

    def _discover_endpoints(self):
        cfg = None
        try:
            cfg = self._dev.get_active_configuration()
        except Exception:
            pass
        if cfg is None:
            try:
                cfg = self._dev.configs()[0]
            except Exception:
                pass
        if cfg is None:
            raise RuntimeError("Could not read accessory configuration")
        in_ep = out_ep = None
        for intf in cfg.interfaces():
            try:
                endpoints = list(intf.endpoints())
            except Exception:
                endpoints = []
            for ep in endpoints:
                if ep.bmAttributes != 0x02:
                    continue
                if ep.bEndpointAddress & 0x80:
                    in_ep = in_ep or ep
                else:
                    out_ep = out_ep or ep
        if in_ep is None or out_ep is None:
            raise RuntimeError(
                "Could not find bulk endpoints (WinUSB driver not bound to 18D1:2D00). "
                "Run: powershell -ExecutionPolicy Bypass -File setup_accessory_driver.ps1"
            )
        self._in_ep = in_ep
        self._out_ep = out_ep

    def _write_all(self, data):
        offset = 0
        while offset < len(data):
            offset += self._out_ep.write(data[offset:])

    def _read_exact(self, length, timeout, allow_idle=False):
        buf = bytearray()
        while len(buf) < length:
            need = length - len(buf)
            try:
                chunk = self._in_ep.read(need, timeout=int(timeout * 1000))
            except usb.core.USBTimeoutError:
                if allow_idle and len(buf) == 0:
                    return None
                raise
            buf += bytes(chunk)
        return bytes(buf)