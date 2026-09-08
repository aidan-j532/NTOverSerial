import json
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
import crossfiledialog

import usb.core

from classes.nt_handler import NTHandler
from classes.apk_installer import install_apk
from classes.usb_handler import USBHandler

TOPIC_RESEND_INTERVAL = 10.0

class TKApp:
    def __init__(self, root):
        self.root = root
        self.root.title("NTOverAOA")
        self.root.geometry("560x480")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.style = ttk.Style()
        self.style.theme_use("clam")

        self.ip_var = tk.StringVar(value="10.22.7.2")
        self.usb_var = tk.StringVar()
        self.apk_var = tk.StringVar()
        self.connected = False

        self._stop = threading.Event()
        self._thread = None
        self._thread_io = None
        self._install_thread = None
        self._driver_install_thread = None

        self.usb = USBHandler()
        self.nt = NTHandler()

        self._candidates = []
        self._subscribed = None
        self._pending_initial_push = None
        self._push_first = False
        self._next_push_retry = 0.0

        self._sub_lock = threading.Lock()
        self._sub_info = {}
        self._subs_dirty = False
        self._last_write_err = 0.0
        self._write_err_count = 0

        self._make_ui()
        self._poll_subs_after = self.root.after(250, self._poll_subs_ui)
        self._rescan_for_usb_devices()

    def _make_ui(self):
        main = ttk.Frame(self.root, padding="8")
        main.pack(fill=tk.BOTH, expand=True)

        self.notebook = ttk.Notebook(main)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        conn_tab = ttk.Frame(self.notebook, padding="8")
        self.notebook.add(conn_tab, text="Connection")

        conn = ttk.LabelFrame(conn_tab, text="Connection", padding="8")
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

        ctrl = ttk.Frame(conn_tab)
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

        log_frame = ttk.LabelFrame(conn_tab, text="Log", padding="4")
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

        setup_tab = ttk.Frame(self.notebook, padding="8")
        self.notebook.add(setup_tab, text="Setup")

        apk_frame = ttk.LabelFrame(setup_tab, text="Install APK", padding="8")
        apk_frame.pack(fill=tk.X, pady=(0, 8))

        row = ttk.Frame(apk_frame)
        row.pack(fill=tk.X, pady=2)

        ttk.Label(row, text="APK file:", width=12).pack(side=tk.LEFT)

        ttk.Entry(
            row,
            textvariable=self.apk_var,
            width=35,
            state="readonly",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        ttk.Button(
            row,
            text="Browse...",
            command=self._choose_apk,
        ).pack(side=tk.LEFT, padx=(6, 0))

        self.install_btn = ttk.Button(
            apk_frame,
            text="Install on Selected Device",
            command=self._install_apk,
        )
        self.install_btn.pack(anchor=tk.W, pady=(8, 0))

        if sys.platform == "win32":
            driver_frame = ttk.LabelFrame(
                setup_tab,
                text="Install WinUSB driver",
                padding="8",
            )
            driver_frame.pack(fill=tk.X, pady=(0, 8))

            ttk.Label(
                driver_frame,
                text="Replace the selected device driver with WinUSB.",
            ).pack(anchor=tk.W)

            self.driver_install_btn = ttk.Button(
                driver_frame,
                text="Install WinUSB on Selected Device",
                command=self._install_winusb,
            )
            self.driver_install_btn.pack(anchor=tk.W, pady=(8, 0))

            self.driver_install_status_label = ttk.Label(
                driver_frame,
                text="Select a USB device.",
                foreground="gray",
                wraplength=500,
            )
            self.driver_install_status_label.pack(fill=tk.X, pady=(6, 0))

        self.install_status_label = ttk.Label(
            setup_tab,
            text="Select an APK and a USB device.",
            foreground="gray",
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=500,
        )
        self.install_status_label.pack(fill=tk.X, anchor=tk.W)

        subs_tab = ttk.Frame(self.notebook, padding="8")
        self.notebook.add(subs_tab, text="Subscriptions")

        subs_head = ttk.Frame(subs_tab)
        subs_head.pack(fill=tk.X, pady=(0, 6))

        self.sub_count_label = ttk.Label(subs_head, text="0 subscribed")
        self.sub_count_label.pack(side=tk.LEFT)

        sub_list = ttk.Frame(subs_tab)
        sub_list.pack(fill=tk.BOTH, expand=True)

        self.sub_listbox = tk.Listbox(
            sub_list,
            height=12,
            font=("Consolas", 10),
            activestyle="none",
        )
        self.sub_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        sub_sb = ttk.Scrollbar(
            sub_list,
            orient=tk.VERTICAL,
            command=self.sub_listbox.yview,
        )
        sub_sb.pack(side=tk.RIGHT, fill=tk.Y)

        self.sub_listbox.configure(yscrollcommand=sub_sb.set)

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

    def _choose_apk(self):
        path = crossfiledialog.open_file(
            title="Select APK",
            start_dir=os.getcwd(),
            filter="*.apk"
        )   

        if path:
            self.apk_var.set(path)
            self.install_status_label.config(
                text="Ready to install on the selected device.",
                foreground="gray",
            )

    def _install_apk(self):
        apk_path = self.apk_var.get().strip()
        label = self.usb_var.get()

        if not apk_path or not os.path.isfile(apk_path):
            messagebox.showwarning("Missing", "Select an APK file first.")
            return

        if not apk_path.lower().endswith(".apk"):
            messagebox.showwarning("Invalid file", "Select an APK file.")
            return

        match = next(
            (candidate for candidate in self._candidates if candidate[2] == label),
            None,
        )

        if match is None:
            messagebox.showwarning(
                "Missing",
                "Select a connected USB device and refresh the device list if needed.",
            )
            return

        serial = match[3]

        if not serial:
            messagebox.showwarning(
                "Missing device serial",
                "The selected USB device does not expose an ADB serial number.",
            )
            return

        self.install_btn.config(state=tk.DISABLED)
        self.install_status_label.config(
            text="Installing...",
            foreground="orange",
        )

        self._install_thread = threading.Thread(
            target=self._install_apk_worker,
            args=(apk_path, serial),
            daemon=True,
        )
        self._install_thread.start()

    def _install_apk_worker(self, apk_path, serial):
        try:
            self._log(f"Installing {os.path.basename(apk_path)} on {serial}")
            install_apk(apk_path, serial)

            self.root.after(
                0,
                self._finish_apk_install,
                "APK installed successfully.",
                "green",
            )
        except Exception as e:
            self.root.after(
                0,
                self._finish_apk_install,
                f"APK install failed: {e}",
                "red",
            )

    def _finish_apk_install(self, message, color):
        self.install_btn.config(state=tk.NORMAL)
        self.install_status_label.config(text=message, foreground=color)

    def _install_winusb(self):
        label = self.usb_var.get()
        match = next(
            (candidate for candidate in self._candidates if candidate[2] == label),
            None,
        )

        if match is None:
            messagebox.showwarning(
                "Missing",
                "Select a connected USB device and refresh the device list if needed.",
            )
            return

        if not messagebox.askyesno(
            "Install WinUSB driver",
            "This replaces the selected device driver with WinUSB and may require administrator approval. Continue?",
        ):
            return

        self.driver_install_btn.config(state=tk.DISABLED)
        self.driver_install_status_label.config(
            text="Installing WinUSB driver... approve the Windows administrator prompt if shown.",
            foreground="orange",
        )
        self._driver_install_thread = threading.Thread(
            target=self._install_winusb_worker,
            args=(match[0], match[1], label),
            daemon=True,
        )
        self._driver_install_thread.start()

    def _install_winusb_worker(self, vid, pid, description):
        try:
            from classes.winusb_installer import install_winusb_driver

            self._log(f"Installing WinUSB on {description}")
            install_winusb_driver(vid, pid, description)
            self.root.after(
                0,
                self._finish_winusb_install,
                "WinUSB driver installed successfully.",
                "green",
            )
        except Exception as e:
            self.root.after(
                0,
                self._finish_winusb_install,
                f"WinUSB install failed: {e}",
                "red",
            )

    def _finish_winusb_install(self, message, color):
        self.driver_install_btn.config(state=tk.NORMAL)
        self.driver_install_status_label.config(text=message, foreground=color)

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

            for key in keys:
                if key not in self._sub_info:
                    self._sub_info[key] = {"value": "", "time": None}

            self._subs_dirty = True

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

    def _short_value(self, value):
        try:
            text = json.dumps(value)
        except Exception:
            text = str(value)

        text = text.replace("\n", " ")

        if len(text) > 80:
            text = text[:77] + "..."

        return text

    def _note_sub_value(self, key, value):
        display = self._short_value(value)

        with self._sub_lock:
            info = self._sub_info.get(key)

            if info is not None and info["value"] != display:
                info["value"] = display
                info["time"] = time.time()
                self._subs_dirty = True

    def _poll_subs_ui(self):
        dirty = False

        with self._sub_lock:
            if self._subs_dirty:
                self._subs_dirty = False
                dirty = True

        if dirty:
            self._rebuild_subs_tree()

        self._poll_subs_after = self.root.after(250, self._poll_subs_ui)

    def _rebuild_subs_tree(self):
        with self._sub_lock:
            items = sorted(self._sub_info.items())

        self.sub_listbox.delete(0, tk.END)

        for key, info in items:
            if info["time"] is not None:
                last = time.strftime("%H:%M:%S", time.localtime(info["time"]))
            else:
                last = "-"

            self.sub_listbox.insert(
                tk.END,
                f"{key} = {info['value'] or '-'} ({last})",
            )

        self.sub_count_label.config(text=f"{len(items)} subscribed")

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

            if self._sub_info:
                self._sub_info.clear()

            self._subs_dirty = True

        self._rebuild_subs_tree()

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

                        try:
                            parsed = json.loads(msg)
                            self._note_sub_value(
                                parsed.get("key", ""),
                                parsed.get("value"),
                            )
                        except (json.JSONDecodeError, ValueError):
                            pass

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

        if hasattr(self, "_poll_subs_after"):
            self.root.after_cancel(self._poll_subs_after)

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