#!/usr/bin/env python3
# =============================================================================
# PiBeam Universal Remote - Host GUI
# -----------------------------------------------------------------------------
# Lightweight Tkinter app for Lubuntu (or any Linux) that pairs with the
# PiBeam firmware (firmware_main.py flashed to the device as main.py).
#
# Features
#   * Auto-detects the PiBeam over USB serial (RP2040 VID 0x2E8A), with
#     status indicator and auto-reconnect.
#   * Devices ("remotes") shown side-by-side in a horizontally scrollable
#     strip; each panel collapsible via its header.
#   * Grid layout per remote: rows of 0-5 evenly spaced slots.
#   * Edit-layout mode (pencil icon): add/remove rows, set slots per row,
#     drag buttons between cells.
#   * Buttons: text glyph (auto-scaled) or transparent PNG.
#     Left-click = transmit. Right-click = context menu (Learn/Overwrite,
#     Clear Stored Code, Update Button, Delete Button).
#   * Learn dialog with capture confirmation, Test-before-save, Cancel.
#   * Config persisted to ~/.config/pibeam_remote/config.json
#     (PNG images embedded as base64 so a single file clones a whole site).
#     File menu: Import / Export / Backup.
#
# Dependencies:  python3-tk  pillow  pyserial
# =============================================================================

import base64
import io
import json
import os
import queue
import shutil
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

try:
    from PIL import Image, ImageTk
except ImportError:
    sys.exit("Missing dependency: pillow  (pip install pillow)")

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("Missing dependency: pyserial  (pip install pyserial)")

APP_NAME = "PiBeam Remote"
CONFIG_DIR = os.path.expanduser("~/.config/pibeam_remote")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
RP2040_VID = 0x2E8A            # Raspberry Pi (MicroPython CDC)
BAUD = 115200
LEARN_TIMEOUT_S = 16           # slightly above firmware's 15 s
MAX_SLOTS = 5


# ----------------------------------------------------------------------------
# Config helpers
# ----------------------------------------------------------------------------
def default_config():
    return {"remotes": []}


def load_config():
    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default_config()


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=1)
    os.replace(tmp, CONFIG_PATH)


def png_to_b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def b64_to_image(b64):
    return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGBA")


# ----------------------------------------------------------------------------
# Serial link (background thread)
# ----------------------------------------------------------------------------
class SerialLink:
    """Owns the serial port. Reader thread pushes parsed JSON events onto
    self.events; GUI polls that queue via root.after()."""

    def __init__(self):
        self.ser = None
        self.events = queue.Queue()
        self.status = "disconnected"     # disconnected | connected
        self._stop = threading.Event()
        self._lock = threading.Lock()
        threading.Thread(target=self._worker, daemon=True).start()

    # ---- public API ----
    def send(self, obj):
        with self._lock:
            if not self.ser:
                return False
            try:
                self.ser.write((json.dumps(obj) + "\n").encode())
                return True
            except (serial.SerialException, OSError):
                self._drop()
                return False

    def close(self):
        self._stop.set()

    # ---- internals ----
    def _find_port(self):
        for p in list_ports.comports():
            if p.vid == RP2040_VID:
                return p.device
        return None

    def _drop(self):
        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass
        self.ser = None
        if self.status != "disconnected":
            self.status = "disconnected"
            self.events.put({"evt": "_status", "status": "disconnected"})

    def _worker(self):
        buf = b""
        while not self._stop.is_set():
            if self.ser is None:
                port = self._find_port()
                if port:
                    try:
                        with self._lock:
                            self.ser = serial.Serial(port, BAUD, timeout=0.2)
                        # handshake
                        self.send({"cmd": "ping"})
                    except (serial.SerialException, OSError):
                        self._drop()
                        time.sleep(1.5)
                        continue
                else:
                    time.sleep(1.5)
                    continue
            try:
                chunk = self.ser.read(256)
            except (serial.SerialException, OSError):
                self._drop()
                continue
            if chunk:
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line.decode("utf-8", "replace"))
                    except ValueError:
                        continue  # REPL noise etc.
                    if msg.get("evt") == "pong" and self.status != "connected":
                        self.status = "connected"
                        self.events.put({"evt": "_status", "status": "connected"})
                    self.events.put(msg)


# ----------------------------------------------------------------------------
# Learn dialog
# ----------------------------------------------------------------------------
class LearnDialog(tk.Toplevel):
    """Modal: waits for a capture event, then offers Test / Save / Cancel."""

    def __init__(self, app, on_save):
        super().__init__(app)
        self.app = app
        self.on_save = on_save
        self.captured = None
        self.title("Learn IR Code")
        self.transient(app)
        self.grab_set()
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.cancel)

        self.lbl = ttk.Label(self, text="Point the original remote at the\n"
                                        "PiBeam and press the button now...",
                             justify="center", padding=16)
        self.lbl.pack()
        btns = ttk.Frame(self, padding=(10, 0, 10, 10))
        btns.pack(fill="x")
        self.test_btn = ttk.Button(btns, text="Test", state="disabled",
                                   command=self.test)
        self.save_btn = ttk.Button(btns, text="Save", state="disabled",
                                   command=self.save)
        cancel_btn = ttk.Button(btns, text="Cancel", command=self.cancel)
        self.test_btn.pack(side="left", expand=True, fill="x", padx=2)
        self.save_btn.pack(side="left", expand=True, fill="x", padx=2)
        cancel_btn.pack(side="left", expand=True, fill="x", padx=2)

        self.deadline = time.time() + LEARN_TIMEOUT_S
        self.app.link.send({"cmd": "learn"})
        self.after(200, self._tick)

    def _tick(self):
        if self.captured is None and time.time() > self.deadline:
            self.lbl.config(text="No signal detected (timed out).\n"
                                 "Close and try again.")
            return
        self.after(200, self._tick)

    # called by App when firmware events arrive while dialog open
    def handle_event(self, msg):
        if msg.get("evt") == "captured":
            self.captured = msg.get("data")
            self.lbl.config(text="Code detected and read!  ({} edges)\n"
                                 "You may Test it before saving."
                            .format(len(self.captured)))
            self.test_btn.config(state="normal")
            self.save_btn.config(state="normal")
        elif msg.get("evt") == "learn_timeout":
            if self.captured is None:
                self.lbl.config(text="No signal detected (timed out).\n"
                                     "Close and try again.")

    def test(self):
        self.app.link.send({"cmd": "test"})

    def save(self):
        if self.captured:
            self.on_save(self.captured)
        self.destroy()
        self.app.learn_dialog = None

    def cancel(self):
        self.destroy()
        self.app.learn_dialog = None


# ----------------------------------------------------------------------------
# Button-editor dialog (create / update)
# ----------------------------------------------------------------------------
class ButtonEditor(tk.Toplevel):
    def __init__(self, app, initial=None, on_done=None):
        super().__init__(app)
        self.app = app
        self.on_done = on_done
        self.png_b64 = (initial or {}).get("image")
        self.title("Button Appearance")
        self.transient(app)
        self.grab_set()
        self.resizable(False, False)

        frm = ttk.Frame(self, padding=12)
        frm.pack()
        ttk.Label(frm, text="Label / glyph text:").grid(row=0, column=0,
                                                        sticky="w")
        self.txt = ttk.Entry(frm, width=18)
        self.txt.insert(0, (initial or {}).get("label", ""))
        self.txt.grid(row=0, column=1, padx=6, pady=4)

        self.png_lbl = ttk.Label(frm, text=self._png_state())
        self.png_lbl.grid(row=1, column=0, sticky="w")
        pf = ttk.Frame(frm)
        pf.grid(row=1, column=1, sticky="w")
        ttk.Button(pf, text="Choose PNG...",
                   command=self.pick_png).pack(side="left")
        ttk.Button(pf, text="Remove PNG",
                   command=self.clear_png).pack(side="left", padx=4)

        bf = ttk.Frame(frm)
        bf.grid(row=2, column=0, columnspan=2, pady=(10, 0), sticky="ew")
        ttk.Button(bf, text="OK", command=self.ok).pack(side="left",
                                                        expand=True, fill="x")
        ttk.Button(bf, text="Cancel", command=self.destroy).pack(
            side="left", expand=True, fill="x", padx=4)

    def _png_state(self):
        return "PNG: set" if self.png_b64 else "PNG: none (text used)"

    def pick_png(self):
        path = filedialog.askopenfilename(
            parent=self, title="Transparent PNG",
            filetypes=[("PNG images", "*.png")])
        if path:
            try:
                self.png_b64 = png_to_b64(path)
                self.png_lbl.config(text=self._png_state())
            except OSError as e:
                messagebox.showerror(APP_NAME, f"Could not read PNG:\n{e}",
                                     parent=self)

    def clear_png(self):
        self.png_b64 = None
        self.png_lbl.config(text=self._png_state())

    def ok(self):
        label = self.txt.get().strip()
        if not label and not self.png_b64:
            messagebox.showwarning(APP_NAME,
                                   "Provide a text glyph or a PNG.",
                                   parent=self)
            return
        if self.on_done:
            self.on_done({"label": label, "image": self.png_b64})
        self.destroy()


# ----------------------------------------------------------------------------
# Remote panel
# ----------------------------------------------------------------------------
class RemotePanel(ttk.Frame):
    PANEL_W = 260

    def __init__(self, app, remote):
        super().__init__(app.strip, relief="groove", borderwidth=2)
        self.app = app
        self.remote = remote           # dict backing this panel in config
        self.edit_mode = False
        self.drag_src = None           # (row_idx, slot_idx) during drag
        self._imgs = {}                # keep PhotoImage refs alive

        # ---- header ----
        hdr = ttk.Frame(self)
        hdr.pack(fill="x")
        self.collapse_btn = ttk.Button(hdr, width=2, text=self._carat(),
                                       command=self.toggle_collapse)
        self.collapse_btn.pack(side="left")
        self.name_lbl = ttk.Label(hdr, text=remote["name"],
                                  font=("TkDefaultFont", 11, "bold"))
        self.name_lbl.pack(side="left", padx=4)
        self.edit_btn = ttk.Button(hdr, width=3, text="\u270e\u25a6",
                                   command=self.toggle_edit)
        self.edit_btn.pack(side="right")
        menu_btn = ttk.Button(hdr, width=2, text="\u22ee",
                              command=self.header_menu)
        menu_btn.pack(side="right")

        # ---- body ----
        self.body = ttk.Frame(self)
        if not remote.get("collapsed"):
            self.body.pack(fill="both", expand=True)
        self.rebuild()

    # ------------- header actions -------------
    def _carat(self):
        return "\u25b8" if self.remote.get("collapsed") else "\u25be"

    def toggle_collapse(self):
        self.remote["collapsed"] = not self.remote.get("collapsed", False)
        if self.remote["collapsed"]:
            self.body.forget()
        else:
            self.body.pack(fill="both", expand=True)
        self.collapse_btn.config(text=self._carat())
        self.app.save()

    def header_menu(self):
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="Rename Device", command=self.rename)
        m.add_command(label="Add New Button", command=self.add_button)
        m.add_separator()
        m.add_command(label="Delete Device", command=self.delete_device)
        m.tk_popup(*self.winfo_pointerxy())

    def rename(self):
        name = simpledialog.askstring(APP_NAME, "Device name:",
                                      initialvalue=self.remote["name"],
                                      parent=self.app)
        if name:
            self.remote["name"] = name
            self.name_lbl.config(text=name)
            self.app.save()

    def delete_device(self):
        if messagebox.askyesno(APP_NAME,
                               f"Delete device '{self.remote['name']}' and "
                               "all its buttons?", parent=self.app):
            self.app.cfg["remotes"].remove(self.remote)
            self.app.save()
            self.app.rebuild_strip()

    def toggle_edit(self):
        self.edit_mode = not self.edit_mode
        self.drag_src = None
        self.rebuild()

    # ------------- layout -------------
    def rebuild(self):
        for w in self.body.winfo_children():
            w.destroy()
        self._imgs.clear()

        for r_idx, row in enumerate(self.remote["rows"]):
            rf = ttk.Frame(self.body)
            rf.pack(fill="x", pady=2)
            slots = row["slots"]
            for c in range(max(slots, 1)):
                rf.columnconfigure(c, weight=1, uniform="slots")
            for s_idx in range(slots):
                btn_cfg = row["buttons"][s_idx]
                w = self.make_cell(rf, r_idx, s_idx, btn_cfg)
                w.grid(row=0, column=s_idx, padx=3, sticky="nsew")
            if self.edit_mode:
                rc = ttk.Frame(rf)
                rc.grid(row=0, column=max(slots, 1), padx=2)
                ttk.Button(rc, width=2, text="\u2699",
                           command=lambda i=r_idx: self.row_menu(i)
                           ).pack()

        if self.edit_mode:
            ttk.Button(self.body, text="+ Add Row",
                       command=self.add_row).pack(pady=4)

    def make_cell(self, parent, r, s, cfg):
        if cfg is None:
            if self.edit_mode:
                b = tk.Button(parent, text="\u00b7", relief="ridge",
                              command=lambda: self.place_or_add(r, s))
                return b
            return ttk.Frame(parent, height=34)  # invisible spacer
        # real button
        label = cfg.get("label") or ""
        b = tk.Button(parent, text=label, wraplength=self.PANEL_W // 3)
        if cfg.get("image"):
            try:
                img = b64_to_image(cfg["image"])
                img.thumbnail((64, 40))
                ph = ImageTk.PhotoImage(img)
                self._imgs[(r, s)] = ph
                b.config(image=ph, text="", width=64, height=40)
            except Exception:
                pass
        if not cfg.get("code"):
            b.config(fg="gray40")     # visually flag "no code learned yet"
        if self.edit_mode:
            b.config(relief="ridge")
            b.bind("<ButtonPress-1>", lambda e: self.drag_start(r, s))
            b.bind("<ButtonRelease-1>", self.drag_release)
        else:
            b.config(command=lambda: self.fire(cfg))
            b.bind("<Button-3>", lambda e: self.context_menu(e, r, s))
        return b

    # ------------- edit-mode: rows -------------
    def add_row(self):
        n = simpledialog.askinteger(APP_NAME, "Slots in new row (0-5):",
                                    minvalue=0, maxvalue=MAX_SLOTS,
                                    initialvalue=3, parent=self.app)
        if n is None:
            return
        self.remote["rows"].append({"slots": n, "buttons": [None] * n})
        self.app.save()
        self.rebuild()

    def row_menu(self, r_idx):
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="Set slot count...",
                      command=lambda: self.resize_row(r_idx))
        m.add_command(label="Delete row",
                      command=lambda: self.delete_row(r_idx))
        m.tk_popup(*self.winfo_pointerxy())

    def resize_row(self, r_idx):
        row = self.remote["rows"][r_idx]
        n = simpledialog.askinteger(APP_NAME, "Slots in row (0-5):",
                                    minvalue=0, maxvalue=MAX_SLOTS,
                                    initialvalue=row["slots"],
                                    parent=self.app)
        if n is None:
            return
        kept = [b for b in row["buttons"] if b is not None][:n]
        row["buttons"] = kept + [None] * (n - len(kept))
        row["slots"] = n
        self.app.save()
        self.rebuild()

    def delete_row(self, r_idx):
        row = self.remote["rows"][r_idx]
        if any(b for b in row["buttons"]):
            if not messagebox.askyesno(APP_NAME,
                                       "Row contains buttons. Delete anyway?",
                                       parent=self.app):
                return
        del self.remote["rows"][r_idx]
        self.app.save()
        self.rebuild()

    # ------------- edit-mode: drag & drop -------------
    def drag_start(self, r, s):
        self.drag_src = (r, s)

    def drag_release(self, event):
        if self.drag_src is None:
            return
        target = self.winfo_containing(event.x_root, event.y_root)
        # walk grid of body rows to find which cell target is
        dst = self.locate_cell(target)
        src = self.drag_src
        self.drag_src = None
        if dst and dst != src:
            rows = self.remote["rows"]
            sr, ss = src
            dr, ds = dst
            rows[sr]["buttons"][ss], rows[dr]["buttons"][ds] = \
                rows[dr]["buttons"][ds], rows[sr]["buttons"][ss]
            self.app.save()
            self.rebuild()

    def locate_cell(self, widget):
        if widget is None:
            return None
        for r_idx, rf in enumerate(
                [w for w in self.body.winfo_children()
                 if isinstance(w, ttk.Frame)]):
            for child in rf.winfo_children():
                if child is widget or widget in child.winfo_children():
                    info = child.grid_info()
                    if info and int(info["column"]) < \
                            self.remote["rows"][r_idx]["slots"]:
                        return (r_idx, int(info["column"]))
        return None

    def place_or_add(self, r, s):
        """Click on an empty cell in edit mode: move pending drag here,
        or offer to create a new button in place."""
        if self.drag_src:
            src = self.drag_src
            self.drag_src = None
            sr, ss = src
            rows = self.remote["rows"]
            rows[r]["buttons"][s] = rows[sr]["buttons"][ss]
            rows[sr]["buttons"][ss] = None
            self.app.save()
            self.rebuild()
        else:
            ButtonEditor(self.app, on_done=lambda c: self.new_button_at(r, s, c))

    def new_button_at(self, r, s, cfg):
        cfg["code"] = None
        self.remote["rows"][r]["buttons"][s] = cfg
        self.app.save()
        self.rebuild()

    # ------------- button behavior -------------
    def add_button(self):
        # first empty slot; else append a row
        for r_idx, row in enumerate(self.remote["rows"]):
            for s_idx in range(row["slots"]):
                if row["buttons"][s_idx] is None:
                    ButtonEditor(self.app,
                                 on_done=lambda c, r=r_idx, s=s_idx:
                                 self.new_button_at(r, s, c))
                    return
        self.remote["rows"].append({"slots": 3, "buttons": [None] * 3})
        ButtonEditor(self.app,
                     on_done=lambda c: self.new_button_at(
                         len(self.remote["rows"]) - 1, 0, c))

    def fire(self, cfg):
        if not cfg.get("code"):
            self.app.set_status("No code stored on that button "
                                "(right-click \u2192 Learn New Code)")
            return
        label = cfg.get("label") or "button"
        if self.app.link.send({"cmd": "send", "data": cfg["code"]}):
            self.app.set_status(f"Sending: {label}...")
            self.app.await_send_result(label)
        else:
            self.app.set_status("PiBeam not connected")

    def context_menu(self, event, r, s):
        cfg = self.remote["rows"][r]["buttons"][s]
        m = tk.Menu(self, tearoff=0)
        if cfg.get("code"):
            m.add_command(label="Overwrite Stored Code",
                          command=lambda: self.learn_into(cfg))
            m.add_command(label="Clear Stored Code",
                          command=lambda: self.clear_code(cfg))
        else:
            m.add_command(label="Learn New Code",
                          command=lambda: self.learn_into(cfg))
        m.add_command(label="Update Button",
                      command=lambda: self.update_button(r, s))
        m.add_separator()
        m.add_command(label="Delete Button",
                      command=lambda: self.delete_button(r, s))
        m.tk_popup(event.x_root, event.y_root)

    def learn_into(self, cfg):
        if self.app.link.status != "connected":
            messagebox.showwarning(APP_NAME, "PiBeam is not connected.",
                                   parent=self.app)
            return
        def on_save(data):
            cfg["code"] = data
            self.app.save()
            self.rebuild()
        self.app.learn_dialog = LearnDialog(self.app, on_save)

    def clear_code(self, cfg):
        cfg["code"] = None
        self.app.save()
        self.rebuild()

    def update_button(self, r, s):
        cfg = self.remote["rows"][r]["buttons"][s]
        def done(newc):
            cfg["label"] = newc["label"]
            cfg["image"] = newc["image"]
            self.app.save()
            self.rebuild()
        ButtonEditor(self.app, initial=cfg, on_done=done)

    def delete_button(self, r, s):
        self.remote["rows"][r]["buttons"][s] = None
        self.app.save()
        self.rebuild()


# ----------------------------------------------------------------------------
# Main application window
# ----------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("860x520")
        self.minsize(420, 300)
        self.cfg = load_config()
        self.link = SerialLink()
        self.learn_dialog = None

        # menu
        mb = tk.Menu(self)
        fm = tk.Menu(mb, tearoff=0)
        fm.add_command(label="Add New Device", command=self.add_device)
        fm.add_separator()
        fm.add_command(label="Export Config...", command=self.export_cfg)
        fm.add_command(label="Import Config...", command=self.import_cfg)
        fm.add_command(label="Backup Config", command=self.backup_cfg)
        fm.add_separator()
        fm.add_command(label="Quit", command=self.destroy)
        mb.add_cascade(label="File", menu=fm)
        self.config(menu=mb)

        # horizontally scrollable strip of panels
        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(outer, highlightthickness=0)
        hbar = ttk.Scrollbar(outer, orient="horizontal",
                             command=self.canvas.xview)
        self.canvas.configure(xscrollcommand=hbar.set)
        hbar.pack(side="bottom", fill="x")
        self.canvas.pack(side="top", fill="both", expand=True)
        self.strip = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.strip,
                                              anchor="nw")
        self.strip.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._stretch_height)

        # status bar
        sb = ttk.Frame(self)
        sb.pack(fill="x", side="bottom")
        self.conn_lbl = tk.Label(sb, text="\u25cf PiBeam: searching...",
                                 fg="#b58900", anchor="w")
        self.conn_lbl.pack(side="left", padx=6)
        self.status_lbl = ttk.Label(sb, text="", anchor="e")
        self.status_lbl.pack(side="right", padx=6)
        self._pending_send = None   # (label, deadline_monotonic) or None

        self.rebuild_strip()
        self.after(100, self.poll_events)

    def _stretch_height(self, event):
        self.canvas.itemconfigure(self._win, height=event.height)

    # ------------- devices strip -------------
    def rebuild_strip(self):
        for w in self.strip.winfo_children():
            w.destroy()
        for remote in self.cfg["remotes"]:
            p = RemotePanel(self, remote)
            p.pack(side="left", fill="y", padx=6, pady=6)

    def add_device(self):
        name = simpledialog.askstring(APP_NAME, "New device name:",
                                      parent=self)
        if not name:
            return
        self.cfg["remotes"].append(
            {"name": name, "collapsed": False,
             "rows": [{"slots": 3, "buttons": [None, None, None]}]})
        self.save()
        self.rebuild_strip()

    # ------------- persistence -------------
    def save(self):
        save_config(self.cfg)

    def export_cfg(self):
        path = filedialog.asksaveasfilename(
            parent=self, defaultextension=".json",
            initialfile="pibeam_config.json",
            filetypes=[("JSON", "*.json")])
        if path:
            with open(path, "w") as f:
                json.dump(self.cfg, f, indent=1)
            self.set_status(f"Exported to {os.path.basename(path)}")

    def import_cfg(self):
        path = filedialog.askopenfilename(
            parent=self, filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            with open(path) as f:
                new = json.load(f)
            assert isinstance(new.get("remotes"), list)
        except Exception:
            messagebox.showerror(APP_NAME, "Not a valid config file.",
                                 parent=self)
            return
        if messagebox.askyesno(APP_NAME, "Replace the current configuration "
                               "with the imported one?", parent=self):
            self.cfg = new
            self.save()
            self.rebuild_strip()

    def backup_cfg(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dst = os.path.join(CONFIG_DIR, f"config-backup-{stamp}.json")
        self.save()
        shutil.copyfile(CONFIG_PATH, dst)
        self.set_status(f"Backup saved: {os.path.basename(dst)}")

    # ------------- events / status -------------
    def await_send_result(self, label, timeout_s=3.0):
        self._pending_send = (label, time.time() + timeout_s)

    def poll_events(self):
        try:
            while True:
                msg = self.link.events.get_nowait()
                if msg.get("evt") == "_status":
                    self.update_conn(msg["status"])
                elif self.learn_dialog is not None and \
                        self.learn_dialog.winfo_exists():
                    self.learn_dialog.handle_event(msg)
                elif msg.get("evt") in ("sent", "error") and self._pending_send:
                    label, _ = self._pending_send
                    self._pending_send = None
                    if msg.get("evt") == "sent":
                        self.set_status(f"Sent: {label}")
                    else:
                        self.set_status(f"Failed to send {label}: "
                                        f"{msg.get('msg', 'unknown error')}")
        except queue.Empty:
            pass
        # If a send confirmation never arrives (device unplugged mid-send,
        # firmware crashed, etc.), don't leave the status bar claiming
        # "Sending..." forever.
        if self._pending_send and time.time() > self._pending_send[1]:
            label = self._pending_send[0]
            self._pending_send = None
            self.set_status(f"No response from PiBeam for: {label}")
        self.after(100, self.poll_events)

    def update_conn(self, status):
        if status == "connected":
            self.conn_lbl.config(text="\u25cf PiBeam: connected",
                                 fg="#2aa22a")
        else:
            self.conn_lbl.config(text="\u25cf PiBeam: disconnected "
                                      "(searching...)", fg="#cc3333")

    def set_status(self, text):
        self.status_lbl.config(text=text)
        self.after(4000, lambda: self.status_lbl.config(text=""))


if __name__ == "__main__":
    App().mainloop()
