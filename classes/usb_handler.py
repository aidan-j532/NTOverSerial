import json
import os
import site
import sys
import time

import usb.core
import usb.util

if sys.platform == "win32":
    from libusb._platform.windows import DLL_PATH
else:
    DLL_PATH = None

from aoa import find_accessory, find_device, toggle_accessory_mode

# AOA vendor and product id stuff
ACCESSORY_VID = 0x18D1
ACCESSORY_PIDS = (0x2D00, 0x2D01)

MANUFACTURER = "NTOverAOA"
MODEL = "Adapter"
DESCRIPTION = "Sends NetworkTables Data to a Android Device With AOA"
VERSION = "1.2"  # :)
URI = ""
SERIAL = ""

WRITE_TIMEOUT = 3000


class USBHandler:
    def __init__(
        self,
        manufacturer=MANUFACTURER,
        model=MODEL,
        description=DESCRIPTION,
        version=VERSION,
        uri=URI,
        serial=SERIAL,
    ):
        self.manufacturer = manufacturer
        self.model = model
        self.description = description
        self.version = version
        self.uri = uri
        self.serial = serial

        self.device = None
        self._ep_in = None
        self._ep_out = None
        self._recv_buf = bytearray()

    def find_libusb_dll(self):
        if sys.platform != "win32":
            return None

        path = str(DLL_PATH)

        if os.path.isfile(path):
            return path

        roots = [sys.prefix]

        roots += [path for path in site.getsitepackages() if os.path.isdir(path)]

        for root in roots:
            for dirpath, _dirs, files in os.walk(root):
                if "libusb-1.0.dll" in files:
                    return os.path.join(dirpath, "libusb-1.0.dll")

        return None

    def init_backend(self):
        dll = self.find_libusb_dll()

        if dll:
            dll_dir = os.path.dirname(dll)

            os.environ["PATH"] = dll_dir + os.pathsep + os.environ.get("PATH", "")

            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(dll_dir)

        try:
            usb.core.find(find_all=True)
        except usb.core.NoBackendError:
            raise RuntimeError("libusb DLL not found (run: pip install libusb)")

    def device_name(self, dev):
        manufacturer = ""
        product = ""

        manufacturer = dev.manufacturer or ""

        product = dev.product or ""

        name = (manufacturer + " " + product).strip()

        return f"{dev.idVendor:04x}:{dev.idProduct:04x} {name}".strip()

    def has_vendor_interface(self, dev):
        try:
            configs = dev.configs()
        except Exception:
            return False

        for config in configs:
            for interface in config.interfaces():
                if interface.bInterfaceClass == 0xFF:
                    return True

        return False

    def is_phone_like(self, dev, name):
        if dev.idVendor == ACCESSORY_VID and dev.idProduct in ACCESSORY_PIDS:
            return True

        low = name.lower()

        phone_names = (
            "android",
            "essential",
            "ph-1",
            "mata",
            "qualcomm",
            "google",
        )

        if any(word in low for word in phone_names):
            return True

        return self.has_vendor_interface(dev)

    def find_options(self):
        self.init_backend()

        phone = []
        others = []

        try:
            for dev in usb.core.find(find_all=True):
                try:
                    name = self.device_name(dev)
                except Exception:
                    name = ""

                if name:
                    display_name = name
                else:
                    display_name = f"{dev.idVendor:04x}:{dev.idProduct:04x}"

                entry = (
                    dev.idVendor,
                    dev.idProduct,
                    display_name,
                )

                if self.is_phone_like(dev, name):
                    phone.append(entry)
                else:
                    others.append(entry)

        except Exception as e:
            raise RuntimeError(f"USB enumeration failed: {e}")

        if phone:
            return phone

        return others

    def connect(self, vidpid, max_wait=5.0):
        self.init_backend()

        dev = find_accessory()

        if dev is None:
            if vidpid:
                dev = find_device([vidpid])
            else:
                dev = None

            if dev is not None:
                toggle_accessory_mode(
                    dev,
                    self.manufacturer,
                    self.model,
                    self.description,
                    self.version,
                    self.uri,
                    self.serial,
                )

                deadline = time.monotonic() + max_wait
                dev = None

                while time.monotonic() < deadline:
                    dev = find_accessory()

                    if dev is not None:
                        break

                    time.sleep(0.1)

        if dev is None:
            raise RuntimeError(
                "Could not enter Android accessory mode. "
                "Is the phone plugged in, unlocked, and USB debugging on?"
            )

        self.device = dev
        self._ep_in, self._ep_out = self.bulk_endpoints(dev)
        self._recv_buf.clear()

        return dev

    def is_connected(self):
        return self.device is not None

    def disconnect(self):
        self.device = None
        self._ep_in = None
        self._ep_out = None
        self._recv_buf.clear()

    def bulk_endpoints(self, dev):
        accessory_intf = None

        for interface in dev.get_active_configuration().interfaces():
            if interface.bInterfaceClass == 0xFF:
                accessory_intf = interface
                break

        if accessory_intf is None:
            raise RuntimeError(
                "No vendor-specific (accessory) interface found on device"
            )

        ep_in = None
        ep_out = None

        for endpoint in accessory_intf.endpoints():
            if endpoint.bmAttributes != usb.util.ENDPOINT_TYPE_BULK:
                continue

            address = endpoint.bEndpointAddress

            if usb.util.endpoint_direction(address) == usb.util.ENDPOINT_IN:
                ep_in = endpoint
            else:
                ep_out = endpoint

        if ep_in is None or ep_out is None:
            raise RuntimeError("Accessory bulk endpoints not found")

        return ep_in, ep_out

    def send_frame(self, payload):
        self._ep_out.write(
            bytes(payload),
            timeout=WRITE_TIMEOUT,
        )

    def send_messages(self, msgs):
        if not msgs:
            return

        buf = b"".join(message for message in msgs if message)

        if buf:
            self._ep_out.write(
                buf,
                timeout=WRITE_TIMEOUT,
            )

    def parse_line(self, line):
        data = json.loads(line)

        if "subscribe" in data:
            return ("subscribe", data.get("subscribe"))

        key = data.get("key")
        value = data.get("value")

        if key is None or value is None:
            return None

        return ("put", {"key": key, "value": value})

    def receive_line(self, timeout=0.2, max_read=65536):
        idx = self._recv_buf.find(b"\n")

        if idx != -1:
            line = bytes(self._recv_buf[:idx])
            del self._recv_buf[: idx + 1]

            return line

        try:
            chunk = self._ep_in.read(
                max_read,
                int(timeout * 1000),
            )

            self._recv_buf += bytes(chunk)

        except usb.core.USBTimeoutError:
            return None

        idx = self._recv_buf.find(b"\n")

        if idx != -1:
            line = bytes(self._recv_buf[:idx])
            del self._recv_buf[: idx + 1]

            return line

        return None

    def error_hint(self, e):
        message = str(e)
        low = message.lower()

        keywords = (
            "not supported",
            "unimplemented",
            "access denied",
            "insufficient permission",
            "pipe error",
        )

        if any(word in low for word in keywords):
            return (
                message + " - WinUSB driver not bound to accessory-mode "
                "18D1:2D00. "
                "Run: powershell -ExecutionPolicy Bypass -File "
                "setup_accessory_driver.ps1"
            )

        return message
