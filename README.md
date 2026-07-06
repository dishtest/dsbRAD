# PiBeam Universal Remote

A learning-remote system for the SB Components **PiBeam** USB IR transceiver.
Two components:

| File | Runs on | Purpose |
|---|---|---|
| `firmware_main.py` | PiBeam (RP2040, MicroPython) | Serial protocol: learn / send / test |
| `pibeam_remote.py` | Lubuntu host | GUI: devices, buttons, learning, config |

---

## 1. Flash the PiBeam (one time)

1. Download the MicroPython UF2 from the official repo:
   `github.com/sbcshop/PiBeam_Software` (PiBeam_firmware file).
2. Hold the **BOOT** button while plugging the PiBeam into USB; release once
   it mounts as a drive called `RPI-RP2`.
3. Drag the UF2 onto that drive. The device reboots into MicroPython.
4. The GPIO pins are already correct in `firmware_main.py` (GP0 = IR TX,
   GP1 = IR RX — confirmed from the official sbcshop pinout table), so no
   editing is needed here.
5. Copy `firmware_main.py` onto the PiBeam **renamed to `main.py`** so it
   auto-runs at power-up. Easiest options:
   - Thonny: open the file, *File → Save as → MicroPython device → main.py*
   - or `mpremote cp firmware_main.py :main.py`
6. Unplug/replug. The firmware is now listening on the USB serial port.

## 2. Host setup (Lubuntu)

```bash
sudo apt install python3-tk python3-serial
# (or: pip install pyserial)
sudo usermod -a -G dialout $USER    # serial-port permission; re-log after
python3 pibeam_remote.py
```

The status bar shows a green dot when the PiBeam is detected (auto-reconnects
if unplugged; RP2040 devices are matched by USB vendor ID), plus the running
firmware version.

## 3. Using the app

- **File → Add New Device** creates a remote panel. Panels sit side-by-side;
  scroll horizontally for more.
- **View → Adjust Scaling** resizes the whole UI (75%–200%) — useful when a
  remote-desktop session lands in an awkward resolution.
- **View menu** also lists every configured remote with a checkbox: uncheck
  to hide a remote entirely (it takes no screen space until re-checked).
  Visibility and scale both persist across restarts.
- **Header ⋮ menu**: rename device, add button, **Edit Layout**, delete
  device.
- **Edit Layout mode** (a **Save Layout** button appears in the header;
  click it to finish — changes save as you make them):
  - `+ Add Row`, per-row ⚙ menu to set slot count (0–5) or delete the row
  - drag buttons between cells (press, drag, release on target)
  - click an empty `·` cell to create a new button in place
- **Left-click** a button: transmit its stored code.
  Buttons with no stored code appear grayed.
- **Right-click** a button: Learn New Code / Overwrite Stored Code,
  Clear Stored Code, Self-Test (loopback), Update Button (rename),
  Delete Button.
- **Learning**: dialog confirms when a code is captured, lets you **Test**
  (replays the captured code out the IR transmitter) before **Save**.
  Cancel any time; times out after ~15 s of no signal.

## 4. Config / site cloning

Everything (devices, layout, codes, UI scale, visibility) lives in one file:

```
~/.config/pibeam_remote/config.json
```

**File → Export Config** produces a single JSON you can carry to another
site; **Import Config** applies it there. **Backup Config** drops a
timestamped copy alongside the live config. Codes are stored as raw IR
timings, so any protocol the receiver can see can be replayed.

## Notes / limits

- Click-per-press only (no hold-to-repeat) by design.
- One program should own the serial port at a time.
- If the device shows disconnected but is plugged in, confirm your user is
  in the `dialout` group and no other program (e.g., Thonny) has the port.
