import base64
import json
import time
import threading
import ntcore
import serial
import serial.tools.list_ports
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
        self.root.title("NT over Serial")
        self.root.geometry("520x420")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.ip_var = tk.StringVar(value="10.22.7.2")
        self.port_var = tk.StringVar()
        self.baud_var = tk.StringVar(value="115200")
        self.connected = False
        self._stop = threading.Event()
        self._thread = None
        self._thread_serial = None
        self._ser = None
        self._inst = None
        self._poller = None
        self._subscribed = None
        self._sub_lock = threading.Lock()

        self._build_ui()
        self._refresh_ports()

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
        ttk.Label(row, text="Serial Port:", width=12).pack(side=tk.LEFT)
        self.port_combo = ttk.Combobox(row, textvariable=self.port_var, width=20, state="readonly")
        self.port_combo.pack(side=tk.LEFT)
        ttk.Button(row, text="Refresh", command=self._refresh_ports, width=8).pack(side=tk.LEFT, padx=(6, 0))

        row = ttk.Frame(conn)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text="Baud Rate:", width=12).pack(side=tk.LEFT)
        bauds = ["9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600"]
        ttk.Combobox(row, textvariable=self.baud_var, values=bauds, width=20, state="readonly").pack(side=tk.LEFT)

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

    def _refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.port_combo["values"] = ports
        if ports and not self.port_var.get():
            self.port_var.set(ports[0])

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
    def _parse_serial_msg(line: str, inst: ntcore.NetworkTableInstance):
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
            self._ser.write(msg.encode("utf-8"))
            self.root.after(0, self._log, f"Sent {len(topics)} topic names")
        except Exception as e:
            self.root.after(0, self._log, f"Topic listing error: {e}")

    def _serial_to_nt(self):
        while not self._stop.is_set():
            try:
                line = self._ser.readline().decode("utf-8", "replace").strip()
                if not line:
                    continue
                self.root.after(0, self._log, f"<< {line}")
                try:
                    result = self._parse_serial_msg(line, self._inst)
                    if result is not None and result[0] == "subscribe":
                        self._handle_subscribe(result[1])
                except (json.JSONDecodeError, ValueError) as e:
                    self.root.after(0, self._log, f"Bad serial message: {e}")
            except Exception as e:
                self.root.after(0, self._log, f"Serial read error: {e}")
                break

    def _connect(self):
        ip = self.ip_var.get().strip()
        port = self.port_var.get()
        baud = self.baud_var.get()
        if not ip:
            messagebox.showwarning("Missing", "Enter a server IP address.")
            return
        if not port:
            messagebox.showwarning("Missing", "Select a serial port.")
            return
        try:
            self._ser = serial.Serial(port, int(baud), timeout=1)
        except Exception as e:
            messagebox.showerror("Serial Error", str(e))
            return

        self._stop.clear()
        self.connected = True
        self.connect_btn.config(text="Disconnect")
        self._set_status(f"Connecting to {ip}...", "orange")
        self._log(f"Opened {port} @ {baud}")
        self._log(f"Connecting to NT server {ip}...")

        self._thread = threading.Thread(target=self._run, args=(ip,), daemon=True)
        self._thread.start()
        self._thread_serial = threading.Thread(target=self._serial_to_nt, daemon=True)
        self._thread_serial.start()

    def _disconnect(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        if self._thread_serial:
            self._thread_serial.join(timeout=3)
            self._thread_serial = None
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None
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
                            self._ser.write(msg.encode("utf-8"))
                            # self.root.after(0, self._log, f">> {msg.strip()}")
                        except Exception as e:
                            self.root.after(0, self._log, f"Serial write error :) Ur cooked blud: {e}")
                time.sleep(0.02)

        except Exception as e:
            self.root.after(0, self._log, f"Error: {e}")
        finally:
            self.root.after(0, self._disconnect)

    def _on_close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._thread_serial:
            self._thread_serial.join(timeout=2)
        if self._ser:
            try:
                self._ser.close()
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