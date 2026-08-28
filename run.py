import base64
import os
import json
import struct
import sys
import time
import threading

import usb.core
import ntcore
from aoa import find_device, find_accessory, toggle_accessory_mode, read, write
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

_registry = SchemaRegistry()


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
        return True
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


def find_candidates():
    init_backend()
    out = []
    try:
        for dev in usb.core.find(find_all=True):
            if dev.idVendor == ACCESSORY_VID and dev.idProduct in ACCESSORY_PIDS:
                out.append((dev.idVendor, dev.idProduct, _device_name(dev)))
            elif _has_vendor_interface(dev) or "android" in _device_name(dev).lower():
                out.append((dev.idVendor, dev.idProduct, _device_name(dev)))
    except Exception:
        pass
    return out


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


def send_frame(dev, payload):
    write(dev, struct.pack("<I", len(payload)) + bytes(payload))


def receive_frame(dev, timeout=0.2):
    buf = bytearray()
    try:
        while len(buf) < 4:
            buf += bytes(read(dev, 4 - len(buf), int(timeout * 1000)))
    except usb.core.USBTimeoutError:
        if len(buf) == 0:
            return None
        raise
    (length,) = struct.unpack("<I", bytes(buf))
    if length == 0:
        return b""
    payload = bytearray()
    while len(payload) < length:
        payload += bytes(read(dev, length - len(payload), int(timeout * 1000)))
    return bytes(payload)


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

    def _send_topic_listing(self):
        try:
            topics = [t.getName() for t in self._inst.getTopics()]
            with self._sub_lock:
                self._subscribed = None
            msg = json.dumps({"topics": topics}) + "\n"
            send_frame(self._dev, msg.encode("utf-8"))
            self.root.after(0, self._log, f"Sent {len(topics)} topic names")
        except Exception as e:
            self.root.after(0, self._log, f"Topic listing error: {e}")

    def _usb_to_nt(self):
        while not self._stop.is_set():
            if self._dev is None:
                time.sleep(0.05)
                continue
            try:
                raw = receive_frame(self._dev)
            except Exception as e:
                self.root.after(0, self._log, f"USB read error: {e}")
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

            while not self._stop.is_set():
                events = poller.readQueue()
                with self._sub_lock:
                    subscribed = self._subscribed
                for ev in events:
                    msg = handle_event(ev, subscribed=subscribed)
                    if msg:
                        try:
                            send_frame(self._dev, msg.encode("utf-8"))
                        except Exception as e:
                            self.root.after(0, self._log, f"USB write error: {e}")
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
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = TKApp(root)
    root.mainloop()