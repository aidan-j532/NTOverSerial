import base64
import json
import time
import threading
import ntcore
from AndroidAccessory import AndroidAccessory, find_devices, device_label
from StructDataStuff import SchemaRegistry
import tkinter as tk
from tkinter import ttk, messagebox

_registry = SchemaRegistry()

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
        self._axc = None
        self._devices = []
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
            devices = find_devices()
        except Exception as e:
            self._log(f"Device scan failed: {e}")
            return
        self._devices = devices
        self.usb_combo["values"] = [device_label(d) for d in devices]
        if devices and not self.usb_var.get():
            self.usb_var.set(device_label(devices[0]))

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
            self._axc.send(msg.encode("utf-8"))
            self.root.after(0, self._log, f"Sent {len(topics)} topic names")
        except Exception as e:
            self.root.after(0, self._log, f"Topic listing error: {e}")

    def _usb_to_nt(self):
        while not self._stop.is_set():
            axc = self._axc
            if axc is None or not axc.connected:
                time.sleep(0.05)
                continue
            try:
                raw = axc.receive()
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
        if not label or not self._devices:
            messagebox.showwarning("Missing", "Plug in the phone and select a USB device.")
            return
        dev = next((d for d in self._devices if device_label(d) == label), None)
        if dev is None:
            messagebox.showwarning("Missing", "Selected USB device is no longer present. Refresh.")
            return

        self._axc = AndroidAccessory(vidpid=(dev.idVendor, dev.idProduct))
        self._stop.clear()
        self.connected = True
        self.connect_btn.config(text="Disconnect")
        self._set_status(f"Connecting to {ip}...", "orange")
        self._log(f"Opening USB device {device_label(dev)}")
        self._log(f"Connecting to NT server {ip}...")

        self._thread = threading.Thread(target=self._run, args=(ip,), daemon=True)
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
        if self._axc:
            try:
                self._axc.close()
            except Exception:
                pass
            self._axc = None
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

    def _run(self, ip):
        try:
            try:
                self._axc.connect(max_wait=5.0)
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
                            self._axc.send(msg.encode("utf-8"))
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
        if self._axc:
            try:
                self._axc.close()
            except Exception:
                pass
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