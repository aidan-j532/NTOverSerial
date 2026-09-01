import json
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import usb.core

from classes.nt_handler import NTHandler
from classes.usb_handler import USBHandler

TOPIC_RESEND_INTERVAL = 10.0

class TKApp:
    def __init__(self, root):
        self.root = root
        self.root.title("NTOverAOA")
        self.root.geometry("520x420")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.ip_var = tk.StringVar(value="10.22.7.2")
        self.usb_var = tk.StringVar()
        self.connected = False

        self._stop = threading.Event()
        self._thread = None
        self._thread_io = None

        self.usb = USBHandler()
        self.nt = NTHandler()

        self._candidates = []
        self._subscribed = None
        self._pending_initial_push = None
        self._push_first = False
        self._next_push_retry = 0.0

        self._sub_lock = threading.Lock()
        self._last_write_err = 0.0
        self._write_err_count = 0

        self._make_ui()
        self._rescan_for_usb_devices()

    def _make_ui(self):
        main = ttk.Frame(self.root, padding="12")
        main.pack(fill=tk.BOTH, expand=True)

        conn = ttk.LabelFrame(main, text="Connection", padding="8")
        conn.pack(fill=tk.X, pady=(0, 8))

        row = ttk.Frame(conn)
        row.pack(fill=tk.X, pady=2)

        ttk.Label(row, text="Server IP:", width=12).pack(side=tk.LEFT)

        self.ip_entry = ttk.Entry(
            row,
            textvariable=self.ip_var,
            width=25,
        )
        self.ip_entry.pack(side=tk.LEFT)

        row = ttk.Frame(conn)
        row.pack(fill=tk.X, pady=2)

        ttk.Label(row, text="USB Device:", width=12).pack(side=tk.LEFT)

        self.usb_combo = ttk.Combobox(
            row,
            textvariable=self.usb_var,
            width=28,
            state="readonly",
        )
        self.usb_combo.pack(side=tk.LEFT)

        ttk.Button(
            row,
            text="Refresh",
            command=self._rescan_for_usb_devices,
            width=8,
        ).pack(side=tk.LEFT, padx=(6, 0))

        ctrl = ttk.Frame(main)
        ctrl.pack(fill=tk.X, pady=(0, 8))

        self.connect_btn = ttk.Button(
            ctrl,
            text="Connect and Run",
            command=self._toggle,
        )
        self.connect_btn.pack(side=tk.LEFT)

        self.status_label = ttk.Label(
            ctrl,
            text="  Disconnected",
            foreground="gray",
        )
        self.status_label.pack(side=tk.LEFT)

        log_frame = ttk.LabelFrame(main, text="Log", padding="4")
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = tk.Text(
            log_frame,
            height=10,
            state=tk.DISABLED,
            wrap=tk.WORD,
            font=("Consolas", 9),
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

        sb = ttk.Scrollbar(
            log_frame,
            orient=tk.VERTICAL,
            command=self.log_text.yview,
        )
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        self.log_text.configure(yscrollcommand=sb.set)

    def _rescan_for_usb_devices(self):
        try:
            candidates = self.usb.find_options()
        except Exception as e:
            self._log(f"Device scan failed: {e}")
            return

        self._candidates = candidates
        self.usb_combo["values"] = [candidate[2] for candidate in candidates]

        if candidates and not self.usb_var.get():
            self.usb_var.set(candidates[0][2])

    def _log(self, msg):
        if threading.current_thread() is not threading.main_thread():
            self.root.after(0, self._log, msg)
            return

        self._append_log(msg)

    def _append_log(self, msg):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, str(msg) + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _set_status(self, text, color="gray"):
        self.status_label.config(
            text=f"  {text}",
            foreground=color,
        )

    def _toggle(self):
        if self.connected:
            self._disconnect()
        else:
            self._connect()

    def _read_usb_msg(self, line):
        return self.usb.parse_line(line)

    def _normalize_key(self, key):
        key = key.strip()

        if not key:
            return ""

        key = "/".join(part for part in key.split("/") if part)

        return "/" + key

    def _handle_subscribe(self, subscribed):
        if not isinstance(subscribed, list):
            self._log("Bad subscribe list")
            return

        keys = [self._normalize_key(str(value)) for value in subscribed]

        keys = [key for key in keys if key]

        with self._sub_lock:
            current = self._subscribed or set()
            new_keys = [key for key in keys if key not in current]

            self._subscribed = current | set(keys)

            pending = self._pending_initial_push or []
            self._pending_initial_push = pending + new_keys
            self._push_first = True

        try:
            info = []

            for key in keys:
                topic_exists = self.nt.topic_exists(key)

                if not topic_exists:
                    info.append(f"failed to subscribe to {key}: it doesn't exist!")

            # self._log(f"Subscribed topics: {'; '.join(info)}")

        except Exception as e:
            self._log(f"Topic analysis failed: {e}")

        shown = ", ".join(repr(key) for key in keys[:10])

        if len(keys) > 10:
            shown += " ..."

        self._log(f"Subscribed to {len(keys)} topics: {shown}")

    def _build_current_value_messages(self, keys):
        return self.nt.build_current_value_messages(keys)

    def _send_topic_listing(self, quiet=False):
        try:
            topics = self.nt.get_topics()

            with self._sub_lock:
                self._subscribed = None

            msg = json.dumps({"topics": topics}) + "\n"

            try:
                self.usb.send_frame(msg.encode("utf-8"))
            except usb.core.USBTimeoutError:
                time.sleep(0.5)
                self.usb.send_frame(msg.encode("utf-8"))

            if not quiet:
                self._log(f"Sent {len(topics)} topic names")

        except usb.core.USBTimeoutError:
            if not quiet:
                self._log("Topic listing write timed out; retrying...")

        except Exception as e:
            if not quiet:
                self._log(f"Topic listing error: {e}")

    def _usb_to_nt(self):
        last_err_log = 0.0

        while not self._stop.is_set():
            if not self.usb.is_connected():
                time.sleep(0.05)
                continue

            try:
                raw = self.usb.receive_line()

            except usb.core.USBTimeoutError as e:
                now = time.monotonic()

                if now - last_err_log > 3:
                    self._log(f"USB read timeout (retrying): {self.usb.error_hint(e)}")
                    last_err_log = now

                continue

            except Exception as e:
                self._log(f"USB read error: {self.usb.error_hint(e)}")
                break

            if not raw:
                continue

            line = raw.decode(
                "utf-8",
                "replace",
            ).strip()

            self._log(f"<< {line}")

            try:
                result = self._read_usb_msg(line)

                if result is None:
                    continue

                kind, payload = result

                if kind == "subscribe":
                    self._handle_subscribe(payload)

                elif kind == "put":
                    self.nt.put_message(payload)

            except (json.JSONDecodeError, ValueError) as e:
                self._log(f"Bad USB message: {e}")

    def _connect(self):
        ip = self.ip_var.get().strip()
        label = self.usb_var.get()

        if not ip:
            messagebox.showwarning(
                "Missing",
                "Enter a server IP address.",
            )
            return

        if not label or not self._candidates:
            messagebox.showwarning(
                "Missing",
                "Plug in the device and select a the USB device.",
            )
            return

        match = next(
            (candidate for candidate in self._candidates if candidate[2] == label),
            None,
        )

        if match is None:
            messagebox.showwarning(
                "Missing",
                "Selected USB device must've been unplugged. Refreshing.",
            )
            self._rescan_for_usb_devices()
            return

        self._stop.clear()
        self.connected = False

        self.connect_btn.config(text="Disconnect")
        self._set_status(
            f"Connecting to {ip}...",
            "orange",
        )

        self._log(f"Opening USB device {label}")
        self._log(f"Connecting to NT server {ip}...")

        self._thread = threading.Thread(
            target=self._run,
            args=(ip, (match[0], match[1])),
            daemon=True,
        )
        self._thread.start()

        self._thread_io = threading.Thread(
            target=self._usb_to_nt,
            daemon=True,
        )
        self._thread_io.start()

    def _disconnect(self):
        self._stop.set()

        self._thread = None
        self._thread_io = None

        self.usb.disconnect()
        self.nt.disconnect()

        with self._sub_lock:
            self._subscribed = None
            self._pending_initial_push = None

        self.connected = False

        self.connect_btn.config(text="Connect and Run")
        self._set_status("Disconnected", "gray")
        self._log("Disconnected.")

    def _run(self, ip, vidpid):
        try:
            try:
                self.usb.connect(vidpid)
            except Exception as e:
                self._log(f"USB connect failed: {e}")
                self.root.after(
                    0,
                    self._set_status,
                    "USB connect failed",
                    "red",
                )
                return

            self._log("Android accessory connected")

            try:
                self.nt.connect(
                    ip,
                    timeout=10,
                )
            except TimeoutError as e:
                self._log(str(e))
                self.root.after(
                    0,
                    self._set_status,
                    "Connection timed out",
                    "red",
                )
                return

            self._log(f"Connected to {ip}")

            self.root.after(
                0,
                self._set_status,
                f"Connected to {ip}",
                "green",
            )
            
            if self.usb.is_connected() and self.nt.is_connected():
                self.connected = True

            self._send_topic_listing()
            last_topic_send = time.monotonic()

            while not self._stop.is_set():
                if not self.usb.is_connected():
                    break

                events = self.nt.read_events()
                now = time.monotonic()

                with self._sub_lock:
                    subscribed = self._subscribed

                    if now >= self._next_push_retry:
                        push_keys = self._pending_initial_push
                    else:
                        push_keys = None

                    if push_keys is not None:
                        self._pending_initial_push = None
                        self._next_push_retry = now + 0.5

                    push_first = self._push_first
                    self._push_first = False

                if (subscribed is None) and (now - last_topic_send >= TOPIC_RESEND_INTERVAL):
                        self._send_topic_listing()
                        last_topic_send = now

                pending = []

                if push_keys:
                    msgs, still_waiting = self._build_current_value_messages(push_keys)

                    pending.extend(msgs)

                    if msgs:
                        self._log(f"Sent current value for {len(msgs)} topic(s)")

                    if still_waiting:
                        with self._sub_lock:
                            current = self._pending_initial_push or []

                            self._pending_initial_push = current + still_waiting

                        if push_first:
                            self._log(
                                "No current value available yet for "
                                "some topics; will keep trying"
                            )

                for event in events:
                    msg = self.nt.handle_event(
                        event,
                        subscribed=subscribed,
                    )

                    if msg:
                        pending.append(msg.encode("utf-8"))

                if pending:
                    try:
                        self.usb.send_messages(pending)

                    except Exception as e:
                        now = time.monotonic()
                        self._write_err_count += 1

                        if now - self._last_write_err > 3:
                            self._log(
                                f"USB write error "
                                f"(x{self._write_err_count}): "
                                f"{self.usb.error_hint(e)}"
                            )

                            self._last_write_err = now
                            self._write_err_count = 0

                time.sleep(0.02)

        except Exception as e:
            self._log(f"Error: {e}")

        finally:
            self.connected = False

    def _on_close(self):
        self._stop.set()

        if self._thread:
            self._thread.join(timeout=2)

        if self._thread_io:
            self._thread_io.join(timeout=2)

        self.usb.disconnect()
        self.nt.disconnect()
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = TKApp(root)
    root.mainloop()