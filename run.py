import base64
import os
import json
import struct
import sys
import time
import threading

import usb.core
import usb.util
import ntcore
from aoa import find_device, find_accessory, toggle_accessory_mode
from StructDataStuff import SchemaRegistry
import tkinter as tk
from tkinter import ttk, messagebox

ACCESSORY_VID = 0x18D1
ACCESSORY_PIDS = (0x2D00, 0x2D01)
MANUFACTURER = "NTOverSerial"
MODEL = "Adapter"
DESCRIPTION = "NT over USB"
VERSION = "1.0"
URI = ""
SERIAL = ""
TOPIC_RESEND_INTERVAL = 2.0

_registry = SchemaRegistry()
_EP_CACHE = {}


def _find_libusb_dll():
    try:
        from libusb._platform.windows import DLL_PATH
        path = str(DLL_PATH)
        if os.path.isfile(path):
            return path
    except Exception:
        pass
    try:
        import site
        roots = [sys.prefix]
        roots += [s for s in site.getsitepackages() if os.path.isdir(s)]
        for root in roots:
            for dirpath, _dirs, files in os.walk(root):
                if "libusb-1.0.dll" in files:
                    return os.path.join(dirpath, "libusb-1.0.dll")
    except Exception:
        pass
    return None


def init_backend():
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
        raise RuntimeError("libusb DLL not found (run: pip install libusb)")


def value_to_json(v: ntcore.Value):
    t = v.type()
    if t == ntcore.NetworkTableType.kRaw:
        return base64.b64encode(v.value()).decode("ascii")
    return v.value()


def _is_subscribed(key, subscribed):
    if subscribed is None:
        return False
    return key in subscribed


def build_message(event: ntcore.Event, table_prefix: str = "", registry: SchemaRegistry = None, subscribed=None):
    if registry is None:
        registry = _registry
    data = event.data
    key = data.topic.getName()
    if table_prefix and not key.startswith(table_prefix):
        return None
    if not _is_subscribed(key, subscribed):
        return None
    type_str = data.topic.getTypeString()
    msg = {
        "key": key,
        "type": type_str,
        "time": data.value.time() / 1_000_000.0,
    }
    if registry.has_type(type_str):
        try:
            msg["schema"] = registry.get_schema(type_str)
            msg["value"] = registry.decode_type(type_str, data.value.value())
        except Exception:
            msg["value"] = value_to_json(data.value)
    else:
        msg["value"] = value_to_json(data.value)
    return json.dumps(msg) + "\n"


def handle_event(event: ntcore.Event, table_prefix: str = "", registry: SchemaRegistry = None, subscribed=None):
    if registry is None:
        registry = _registry
    key = event.data.topic.getName()
    marker = "/.schema/"
    if marker in key:
        name = key.split(marker, 1)[1]
        try:
            registry.register(name, event.data.value.value().decode("utf-8", "replace"))
        except Exception:
            pass
        return None
    return build_message(event, table_prefix, registry, subscribed)


def _device_name(dev):
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


def _has_vendor_interface(dev):
    try:
        for cfg in dev.configs():
            for intf in cfg.interfaces():
                if intf.bInterfaceClass == 0xFF:
                    return True
    except Exception:
        pass
    return False


def _is_phone_like(dev, name):
    if dev.idVendor == ACCESSORY_VID and dev.idProduct in ACCESSORY_PIDS:
        return True
    low = name.lower()
    if any(w in low for w in ("android", "essential", "ph-1", "mata", "qualcomm", "google")):
        return True
    return _has_vendor_interface(dev)


def find_candidates():
    init_backend()
    phone = []
    others = []
    try:
        for dev in usb.core.find(find_all=True):
            try:
                name = _device_name(dev)
            except Exception:
                name = ""
            entry = (dev.idVendor, dev.idProduct, name or f"{dev.idVendor:04x}:{dev.idProduct:04x}")
            if _is_phone_like(dev, name):
                phone.append(entry)
            else:
                others.append(entry)
    except Exception as e:
        raise RuntimeError(f"USB enumeration failed: {e}")
    return phone if phone else others


def connect_accessory(vidpid, max_wait=5.0):
    init_backend()
    dev = find_accessory()
    if dev is None:
        dev = find_device([vidpid]) if vidpid else None
        if dev is not None:
            toggle_accessory_mode(dev, MANUFACTURER, MODEL, DESCRIPTION, VERSION, URI, SERIAL)
            deadline = time.monotonic() + max_wait
            dev = None
            while time.monotonic() < deadline:
                dev = find_accessory()
                if dev is not None:
                    break
                time.sleep(0.1)
    if dev is None:
        raise RuntimeError("Could not enter Android accessory mode. Is the phone plugged in, unlocked, and USB debugging on?")
    return dev


def _bulk_endpoints(dev):
    key = (dev.bus, dev.address)
    if key in _EP_CACHE:
        return _EP_CACHE[key]
    ep_in = ep_out = None
    try:
        # The phone re-enumerates in accessory mode as a COMPOSITE device:
        # the real Android Accessory Interface (vendor-specific, class 0xFF)
        # plus other interfaces (e.g. ADB) that also expose bulk endpoints.
        # We must only look at endpoints on the accessory interface itself,
        # or we can silently latch onto e.g. the ADB pipe instead -- writes
        # and reads "succeed" but nothing is on the other end, so nothing
        # ever arrives and nothing ever errors.
        accessory_intf = None
        for intf in dev.get_active_configuration().interfaces():
            if intf.bInterfaceClass == 0xFF:
                accessory_intf = intf
                break
        if accessory_intf is None:
            raise RuntimeError("No vendor-specific (accessory) interface found on device")
        for ep in accessory_intf.endpoints():
            if ep.bmAttributes != usb.util.ENDPOINT_TYPE_BULK:
                continue
            ep_addr = ep.bEndpointAddress
            if usb.util.endpoint_direction(ep_addr) == usb.util.ENDPOINT_IN:
                ep_in = ep
            else:
                ep_out = ep
    except Exception:
        pass
    if ep_in is None or ep_out is None:
        raise RuntimeError("Accessory bulk endpoints not found")
    _EP_CACHE[key] = (ep_in, ep_out)
    return ep_in, ep_out


def send_frame(dev, payload):
    ep_out = _bulk_endpoints(dev)[1]
    ep_out.write(struct.pack("<I", len(payload)) + bytes(payload), timeout=3000)


def send_messages(dev, msgs):
    if not msgs:
        return
    ep_out = _bulk_endpoints(dev)[1]
    buf = b"".join(struct.pack("<I", len(m)) + m for m in msgs if m)
    if buf:
        ep_out.write(buf, timeout=3000)


def receive_frame(dev, timeout=0.2, max_wait=2.0):
    # Reads a 4-byte little-endian length prefix followed by that many
    # payload bytes. The phone side may write the header and payload as
    # separate USB transactions, so a single 200ms slice timing out with a
    # partially-read header/payload is NOT fatal -- keep retrying within a
    # longer overall deadline. Only raise (a real fault) if we've made no
    # progress for the whole max_wait window.
    ep_in = _bulk_endpoints(dev)[0]
    buf = bytearray()
    deadline = time.monotonic() + max_wait
    while len(buf) < 4:
        try:
            buf += bytes(ep_in.read(4 - len(buf), int(timeout * 1000)))
            deadline = time.monotonic() + max_wait  # progress made, extend deadline
        except usb.core.USBTimeoutError:
            if len(buf) == 0:
                return None  # nothing at all waiting - totally normal, not an error
            if time.monotonic() > deadline:
                raise  # stuck mid-header for too long - something's actually wrong
    (length,) = struct.unpack("<I", bytes(buf))
    if length == 0:
        return b""
    payload = bytearray()
    deadline = time.monotonic() + max_wait
    while len(payload) < length:
        try:
            payload += bytes(ep_in.read(length - len(payload), int(timeout * 1000)))
            deadline = time.monotonic() + max_wait
        except usb.core.USBTimeoutError:
            if time.monotonic() > deadline:
                raise
    return bytes(payload)


def _usb_error_hint(e):
    s = str(e)
    low = s.lower()
    if any(t in low for t in ("not supported", "unimplemented", "access denied", "insufficient permission", "pipe error")):
        return (s
                + " - WinUSB driver not bound to accessory-mode 18D1:2D00. "
                  "Run: powershell -ExecutionPolicy Bypass -File setup_accessory_driver.ps1")
    return s


# Create the TKinger app which is a nice like "old" looking UI library for python
class TKApp:
    def __init__(self, root):
        self.root = root
        self.root.title("NT over USB (Android Accessory)")
        self.root.geometry("520x420")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.ip_var = tk.StringVar(value="10.22.7.2")
        self.usb_var = tk.StringVar()
        self.connected = False
        self._stop = threading.Event()
        self._thread = None
        self._thread_io = None
        self._dev = None
        self._candidates = []
        self._inst = None
        self._poller = None
        self._subscribed = None
        self._sub_lock = threading.Lock()
        self._last_write_err = 0.0
        self._write_err_count = 0

        self._build_ui()
        self._refresh_devices()

    def _build_ui(self):
        main = ttk.Frame(self.root, padding="12")
        main.pack(fill=tk.BOTH, expand=True)

        conn = ttk.LabelFrame(main, text="Connection", padding="8")
        conn.pack(fill=tk.X, pady=(0, 8))

        row = ttk.Frame(conn)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text="Server IP:", width=12).pack(side=tk.LEFT)
        self.ip_entry = ttk.Entry(row, textvariable=self.ip_var, width=25)
        self.ip_entry.pack(side=tk.LEFT)

        row = ttk.Frame(conn)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text="USB Device:", width=12).pack(side=tk.LEFT)
        self.usb_combo = ttk.Combobox(row, textvariable=self.usb_var, width=28, state="readonly")
        self.usb_combo.pack(side=tk.LEFT)
        ttk.Button(row, text="Refresh", command=self._refresh_devices, width=8).pack(side=tk.LEFT, padx=(6, 0))

        ctrl = ttk.Frame(main)
        ctrl.pack(fill=tk.X, pady=(0, 8))
        self.connect_btn = ttk.Button(ctrl, text="Connect and Run", command=self._toggle)
        self.connect_btn.pack(side=tk.LEFT)
        self.status_label = ttk.Label(ctrl, text="  Disconnected", foreground="gray")
        self.status_label.pack(side=tk.LEFT)

        log_frame = ttk.LabelFrame(main, text="Log", padding="4")
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.log_text = tk.Text(log_frame, height=10, state=tk.DISABLED, wrap=tk.WORD, font=("Consolas", 9))
        sb = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def _refresh_devices(self):
        try:
            candidates = find_candidates()
        except Exception as e:
            self._log(f"Device scan failed: {e}")
            return
        self._candidates = candidates
        self.usb_combo["values"] = [c[2] for c in candidates]
        if candidates and not self.usb_var.get():
            self.usb_var.set(candidates[0][2])

    def _log(self, msg):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _set_status(self, text, color="gray"):
        self.status_label.config(text=f"  {text}", foreground=color)

    def _toggle(self):
        if self.connected:
            self._disconnect()
        else:
            self._connect()

    @staticmethod
    def _parse_usb_msg(line: str, inst: ntcore.NetworkTableInstance):
        data = json.loads(line)
        if "subscribe" in data:
            return ("subscribe", data.get("subscribe"))
        key = data.get("key")
        value = data.get("value")
        if key is None or value is None:
            return None
        inst.getTable("").putValue(key, value)
        return None

    def _handle_subscribe(self, subscribed):
        if not isinstance(subscribed, list):
            self.root.after(0, self._log, "Bad subscribe list")
            return
        keys = [str(s) for s in subscribed]
        with self._sub_lock:
            self._subscribed = set(keys) if keys else set()
        self.root.after(0, self._log, f"Subscribed to {len(keys)} topics")

    def _send_topic_listing(self, quiet=False):
        try:
            topics = [t.getName() for t in self._inst.getTopics()]
            with self._sub_lock:
                self._subscribed = None
            msg = json.dumps({"topics": topics}) + "\n"
            try:
                send_frame(self._dev, msg.encode("utf-8"))
            except usb.core.USBTimeoutError:
                time.sleep(0.5)
                send_frame(self._dev, msg.encode("utf-8"))
            if not quiet:
                self.root.after(0, self._log, f"Sent {len(topics)} topic names")
        except usb.core.USBTimeoutError:
            if not quiet:
                self.root.after(0, self._log, "Topic listing write timed out; retrying automatically")
        except Exception as e:
            if not quiet:
                self.root.after(0, self._log, f"Topic listing error: {e}")

    def _usb_to_nt(self):
        last_err_log = 0.0
        while not self._stop.is_set():
            dev = self._dev
            if dev is None:
                time.sleep(0.05)
                continue
            try:
                raw = receive_frame(dev)
            except usb.core.USBTimeoutError as e:
                # A stuck-mid-frame timeout is worth knowing about but isn't
                # fatal -- keep the read loop alive and just retry.
                now = time.monotonic()
                if now - last_err_log > 3:
                    self.root.after(0, self._log, f"USB read timeout (retrying): {_usb_error_hint(e)}")
                    last_err_log = now
                continue
            except Exception as e:
                self.root.after(0, self._log, f"USB read error: {_usb_error_hint(e)}")
                break
            if not raw:
                continue
            line = raw.decode("utf-8", "replace").strip()
            self.root.after(0, self._log, f"<< {line}")
            try:
                result = self._parse_usb_msg(line, self._inst)
                if result is not None and result[0] == "subscribe":
                    self._handle_subscribe(result[1])
            except (json.JSONDecodeError, ValueError) as e:
                self.root.after(0, self._log, f"Bad USB message: {e}")

    def _connect(self):
        ip = self.ip_var.get().strip()
        label = self.usb_var.get()
        if not ip:
            messagebox.showwarning("Missing", "Enter a server IP address.")
            return
        if not label or not self._candidates:
            messagebox.showwarning("Missing", "Plug in the phone and select a USB device.")
            return
        match = next((c for c in self._candidates if c[2] == label), None)
        if match is None:
            messagebox.showwarning("Missing", "Selected USB device is no longer present. Refresh.")
            return

        self._stop.clear()
        self.connected = True
        self.connect_btn.config(text="Disconnect")
        self._set_status(f"Connecting to {ip}...", "orange")
        self._log(f"Opening USB device {label}")
        self._log(f"Connecting to NT server {ip}...")

        self._thread = threading.Thread(target=self._run, args=(ip, (match[0], match[1])), daemon=True)
        self._thread.start()
        self._thread_io = threading.Thread(target=self._usb_to_nt, daemon=True)
        self._thread_io.start()

    def _disconnect(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        if self._thread_io:
            self._thread_io.join(timeout=3)
            self._thread_io = None
        self._dev = None
        if self._poller:
            try:
                self._poller.close()
            except Exception:
                pass
            self._poller = None
        if self._inst:
            try:
                self._inst.stopClient()
            except Exception:
                pass
            self._inst = None
        with self._sub_lock:
            self._subscribed = None
        self.connected = False
        self.connect_btn.config(text="Connect and Run")
        self._set_status("Disconnected", "gray")
        self._log("Disconnected.")

    def _run(self, ip, vidpid):
        try:
            try:
                self._dev = connect_accessory(vidpid)
            except Exception as e:
                self.root.after(0, self._log, f"USB connect failed: {e}")
                self.root.after(0, self._set_status, "USB connect failed", "red")
                return
            self.root.after(0, self._log, "Android accessory connected")

            inst = ntcore.NetworkTableInstance.getDefault()
            self._inst = inst
            inst.setServer(ip)
            inst.startClient4("NTOverSerial")

            poller = ntcore.NetworkTableListenerPoller(inst)
            self._poller = poller
            poller.addListener([""], ntcore.EventFlags.kValueRemote)

            timeout = 10
            start = time.monotonic()
            while not self._stop.is_set() and not inst.isConnected():
                if time.monotonic() - start > timeout:
                    self.root.after(0, self._log, f"Connection to {ip} timed out")
                    self.root.after(0, self._set_status, "Connection timed out", "red")
                    return
                time.sleep(0.2)

            self.root.after(0, self._log, f"Connected to {ip}")
            self.root.after(0, self._set_status, f"Connected to {ip}", "green")

            self._send_topic_listing()
            last_topic_send = time.monotonic()

            while not self._stop.is_set():
                dev = self._dev
                if dev is None:
                    break
                events = poller.readQueue()
                with self._sub_lock:
                    subscribed = self._subscribed
                if subscribed is None:
                    now = time.monotonic()
                    if now - last_topic_send >= TOPIC_RESEND_INTERVAL:
                        self._send_topic_listing(quiet=True)
                        last_topic_send = now
                pending = []
                for ev in events:
                    msg = handle_event(ev, subscribed=subscribed)
                    if msg:
                        pending.append(msg.encode("utf-8"))
                if pending:
                    try:
                        send_messages(dev, pending)
                    except Exception as e:
                        now = time.monotonic()
                        self._write_err_count += 1
                        if now - self._last_write_err > 3:
                            self.root.after(0, self._log, f"USB write error (x{self._write_err_count}): {_usb_error_hint(e)}")
                            self._last_write_err = now
                            self._write_err_count = 0
                time.sleep(0.02)

        except Exception as e:
            self.root.after(0, self._log, f"Error: {e}")
        finally:
            self.root.after(0, self._disconnect)

    def _on_close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._thread_io:
            self._thread_io.join(timeout=2)
        self._dev = None
        if self._poller:
            try:
                self._poller.close()
            except Exception:
                pass
        if self._inst:
            try:
                self._inst.stopClient()
            except Exception:
                pass
            self._inst = None
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = TKApp(root)
    root.mainloop()