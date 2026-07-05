# =============================================================================
# PiBeam Universal Remote Firmware  (save to the PiBeam as: main.py)
# -----------------------------------------------------------------------------
# Runs on the PiBeam's RP2040 under MicroPython. Listens on USB CDC serial
# (the same port MicroPython's REPL uses) for newline-delimited JSON commands
# from the host GUI, drives the IR transmitter, and captures raw IR timings
# from the IR receiver.
#
# PROTOCOL (host -> device), one JSON object per line:
#   {"cmd": "ping"}                     -> {"evt":"pong","fw":"2.3"}
#   {"cmd": "learn"}                    -> {"evt":"learn_start"} then either
#                                          {"evt":"captured","data":[...]} or
#                                          {"evt":"learn_timeout"}
#   {"cmd": "send", "data": [t0,t1..]}  -> {"evt":"sent"} or {"evt":"error",...}
#   {"cmd": "test"}                     -> retransmits the last captured code
#                                          -> {"evt":"sent"} or {"evt":"error",...}
#
# Timing data is a flat list of microsecond durations alternating
# MARK (IR on), SPACE (IR off), always beginning with a MARK.
#
# PIN ASSIGNMENTS: confirmed from the official sbcshop PiBeam interfacing
# table (github.com/sbcshop/PiBeam_Software) - GP0 is the IR transmitter,
# GP1 is the IR receiver.
#
# -----------------------------------------------------------------------------
# WHY THIS VERSION IS DIFFERENT (v2):
# v1 used a plain Python "for" loop with sleep_us() to bit-bang the carrier,
# and a polling loop to capture the receiver pin. Both are subject to
# MicroPython interpreter overhead (a few microseconds of call/loop overhead
# per edge), which is enough jitter to make IR protocols fail to decode on
# real hardware even though a capture "looked" roughly right.
#
# v2 fixes the root cause by using the RP2040's PIO (Programmable I/O)
# hardware block for transmit - the same technique sbcshop's own PiBeam.py
# library uses - which generates the carrier on/off timing with ~1us
# hardware precision, completely independent of interpreter speed. Capture
# is switched to an interrupt-driven edge timestamp (Pin.irq), which
# timestamps each edge the instant it happens in a hardware interrupt,
# rather than polling a pin in a software loop and possibly missing or
# mistiming short pulses between checks.
#
# The RP2_RMT class below is adapted directly from sbcshop's PiBeam.py
# (examples folder of github.com/sbcshop/PiBeam_Software) - it is the
# proven, working transmit engine, just wired up to accept our own raw
# universal timing arrays instead of their protocol-specific encoders.
# =============================================================================

import sys
import ujson
import utime
import select
import rp2
import micropython
from machine import Pin, PWM
from array import array

# Required for meaningful error reports if a hard ISR ever raises.
micropython.alloc_emergency_exception_buf(100)

# ------------------------- CONFIG (confirmed pins) --------------------------
IR_TX_PIN = 0           # GP0 - IR Transmitter (per sbcshop PiBeam pinout table)
IR_RX_PIN = 1           # GP1 - IR Receiver (per sbcshop PiBeam pinout table)

CARRIER_HZ = 38000      # standard consumer IR carrier
CARRIER_DUTY_PCT = 50   # raised from 33% for stronger radiated output
MAX_EDGES = 300         # max mark/space segments captured per code
FRAME_END_GAP_US = 15000    # idle gap (us) that terminates a capture
LEARN_TIMEOUT_MS = 15000    # give up learning after 15 s of no signal
# -----------------------------------------------------------------------------


# ===================== PIO-driven precise transmit engine ===================
# Adapted from sbcshop/PiBeam_Software examples/PiBeam.py (RP2_RMT / irqtrain).
# The PIO program generates hardware-timed IRQs at each on/off interval;
# the Python IRQ callback toggles the actual carrier PWM duty in sync with
# those hardware-timed pulses, so timing precision comes from the PIO
# state machine's clock, not from the Python interpreter's loop speed.

@rp2.asm_pio(autopull=True, pull_thresh=32)
def _irqtrain():
    wrap_target()
    out(x, 32)             # mark duration (1 tick = 1us at our sm_freq)
    irq(rel(0))
    label('loop')
    jmp(x_dec, 'loop')
    wrap()


class _RP2_RMT:
    """Feeds a flat array of [mark, space, mark, space, ..., 0] microsecond
    durations to a PIO state machine; toggles the carrier PWM duty in
    lock-step via the PIO's own IRQs for hardware-accurate timing."""

    def __init__(self, tx_pin, freq, duty_pct, sm_no=0, sm_freq=1_000_000):
        self.pwm = PWM(tx_pin)
        self.pwm.freq(freq)
        self.pwm.duty_u16(0)
        self.on_duty = int(0xFFFF * duty_pct // 100)
        # PIO instruction order is: pull duration -> RAISE IRQ -> delay.
        # So each IRQ fires at the START of its interval (the first one
        # ~2us after the SM starts). Interval 1 is a MARK, so IRQ 1
        # (count=0) must switch the carrier ON, IRQ 2 OFF, alternating:
        # (ON, OFF) indexed by count & 1.
        self.duty_levels = (self.on_duty, 0)
        # hard=True: toggle the carrier the instant the PIO interval ends,
        # not whenever the soft-IRQ scheduler gets around to it. Soft
        # scheduling latency (up to ~ms) is longer than typical IR
        # intervals (0.5-1.7 ms), which garbles the signal completely.
        self.sm = rp2.StateMachine(sm_no, _irqtrain, freq=sm_freq)
        rp2.PIO(0).irq(self._on_irq, hard=True)
        self.arr = None
        self.ptr = 0
        self.count = 0
        self.done_evt = False

    def _on_irq(self, pio):
        # Alternate the carrier on/off in step with each PIO-timed interval.
        self.pwm.duty_u16(self.duty_levels[self.count & 1])
        self.count += 1
        if self.ptr < len(self.arr):
            self.sm.put(self.arr[self.ptr])
            self.ptr += 1
        else:
            self.done_evt = True

    def send_blocking(self, timings, max_wait_ms=500):
        """Send a raw [mark, space, ...] us list and block until finished."""
        if not timings:
            return False
        arr = array('I', timings)
        # Ensure it ends on a SPACE (carrier off) so the IR LED doesn't
        # stay on; append a small trailing space if it ends on a mark.
        if len(arr) % 2 == 1:
            arr = array('I', list(arr) + [1])
        self.arr = arr
        self.ptr = 0
        self.count = 0
        self.done_evt = False
        # Carrier starts OFF; the first IRQ (fires at the start of
        # interval 1, ~2us after activation) switches it ON for the
        # first mark.
        self.pwm.duty_u16(0)
        self.sm.active(1)
        n = min(4, len(arr))
        for i in range(n):
            self.sm.put(arr[i])
        self.ptr = n
        start = utime.ticks_ms()
        while not self.done_evt:
            if utime.ticks_diff(utime.ticks_ms(), start) > max_wait_ms:
                self.sm.active(0)
                self.pwm.duty_u16(0)
                return False
            utime.sleep_ms(1)
        self.sm.active(0)
        self.pwm.duty_u16(0)
        return True


_rmt = _RP2_RMT(Pin(IR_TX_PIN), CARRIER_HZ, CARRIER_DUTY_PCT)


def transmit_ir(timings):
    return _rmt.send_blocking(timings)


# ===================== Interrupt-driven precise capture ====================
# Timestamps every edge the instant it occurs (hardware IRQ), rather than
# polling the pin in a software loop - avoids missing or mistiming short
# pulses due to interpreter overhead between polls.

_rx_pin = Pin(IR_RX_PIN, Pin.IN, Pin.PULL_UP)
_MAXBUF = MAX_EDGES + 2
_edge_times = array('i', (0 for _ in range(_MAXBUF)))
_edge_count = 0
_capturing = False


def _rx_isr(pin):
    global _edge_count
    if _capturing and _edge_count < _MAXBUF:
        _edge_times[_edge_count] = utime.ticks_us()
        _edge_count += 1


# hard=True is essential here: soft pin-IRQ handlers are queued (depth 1)
# and executed by the interpreter's scheduler - edges arriving while one is
# queued are silently DROPPED, and the queue isn't serviced at all during
# a sleep_us() busy-wait. That combination is what limited captures to ~2
# edges. A hard ISR timestamps every edge the instant it happens.
_rx_pin.irq(handler=_rx_isr, trigger=Pin.IRQ_FALLING | Pin.IRQ_RISING,
            hard=True)


def capture_ir():
    """Block until an IR frame is captured (via ISR timestamps) or timeout.
    Returns a list of [mark, space, mark, ...] microsecond durations, or
    None on timeout with nothing captured."""
    global _edge_count, _capturing
    _edge_count = 0
    _capturing = True
    start = utime.ticks_ms()
    try:
        # Wait for the first edge (start of a frame).
        while _edge_count == 0:
            if utime.ticks_diff(utime.ticks_ms(), start) > LEARN_TIMEOUT_MS:
                return None
            utime.sleep_ms(2)
        # Keep collecting until the receiver idles for FRAME_END_GAP_US
        # or we hit the edge-count ceiling. (sleep_ms, not sleep_us: with
        # a hard ISR this matters less, but sleep_ms also keeps the
        # system serviced during the wait.)
        while _edge_count < MAX_EDGES:
            last_n = _edge_count
            utime.sleep_ms(FRAME_END_GAP_US // 1000)
            if _edge_count == last_n:
                break  # no new edges arrived during that idle window
    finally:
        _capturing = False

    n = _edge_count
    if n < 2:
        return None
    timings = []
    for i in range(n - 1):
        timings.append(utime.ticks_diff(_edge_times[i + 1], _edge_times[i]))
    return timings if timings else None


# ============================ Serial JSON protocol ==========================
last_capture = None

poller = select.poll()
poller.register(sys.stdin, select.POLLIN)


def reply(obj):
    sys.stdout.write(ujson.dumps(obj) + "\n")


def handle(line):
    global last_capture
    try:
        msg = ujson.loads(line)
    except ValueError:
        reply({"evt": "error", "msg": "bad json"})
        return
    cmd = msg.get("cmd")

    try:
        if cmd == "ping":
            reply({"evt": "pong", "fw": "2.3"})

        elif cmd == "learn":
            reply({"evt": "learn_start"})
            data = capture_ir()
            if data is None:
                reply({"evt": "learn_timeout"})
            else:
                last_capture = data
                reply({"evt": "captured", "data": data})

        elif cmd == "send":
            data = msg.get("data")
            if not data:
                reply({"evt": "error", "msg": "no data"})
            elif transmit_ir(data):
                reply({"evt": "sent"})
            else:
                reply({"evt": "error", "msg": "transmit timed out"})

        elif cmd == "test":
            if last_capture is None:
                reply({"evt": "error", "msg": "nothing captured yet"})
            elif transmit_ir(last_capture):
                reply({"evt": "sent"})
            else:
                reply({"evt": "error", "msg": "transmit timed out"})

        elif cmd == "selftest":
            # Loopback: capture our own transmission with the onboard
            # receiver (hold a reflector ~5-10 cm in front of the device,
            # or rely on direct bleed - TX and RX are adjacent). Returns
            # both what we intended to send and what the receiver heard,
            # so replay fidelity can be verified end-to-end.
            global _edge_count, _capturing
            code = msg.get("data") or last_capture
            if not code:
                reply({"evt": "error", "msg": "no code to self-test"})
            else:
                _edge_count = 0
                _capturing = True
                ok = transmit_ir(code)
                utime.sleep_ms(30)          # let trailing edges land
                _capturing = False
                n = _edge_count
                heard = []
                for i in range(n - 1):
                    heard.append(utime.ticks_diff(_edge_times[i + 1],
                                                  _edge_times[i]))
                reply({"evt": "selftest",
                       "tx_ok": ok,
                       "sent_edges": len(code),
                       "heard_edges": len(heard),
                       "sent": code,
                       "heard": heard})

        else:
            reply({"evt": "error", "msg": "unknown cmd"})

    except Exception as e:
        # Never let an unexpected error silently kill the listener loop -
        # report it and keep serving subsequent commands.
        reply({"evt": "error", "msg": "exception: {}".format(e)})


buf = ""
while True:
    if poller.poll(50):
        ch = sys.stdin.read(1)
        if ch:
            if ch == "\n":
                if buf.strip():
                    handle(buf.strip())
                buf = ""
            else:
                buf += ch
