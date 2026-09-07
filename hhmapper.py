"""hhmapper prototype, iteration 1: console output only.

Listens to the TD-17, tracks the hi-hat pedal CC, and on every hi-hat hit
shows the combination of openness (tight / mid / open) and zone (edge / body).

Usage (from the project venv):
    .venv/bin/python hhmapper.py            # live Rich UI with huge label
    .venv/bin/python hhmapper.py --plain    # one line per hit, no UI
    .venv/bin/python hhmapper.py --raw      # dump every incoming MIDI message
    .venv/bin/python hhmapper.py --port X   # pick a port by substring (default: TD-17)
"""
import argparse
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass

import mido
import pyfiglet
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ---------------------------------------------------------------------------
# Constants: adjust to what the module actually sends (check with --raw).
# ---------------------------------------------------------------------------
PORT_NAME_SUBSTRING = "TD-17"

HH_CC = 4  # hi-hat pedal position controller (CC#4 on Roland modules)

# TD-17 hi-hat notes. The module already switches notes depending on pedal
# state, so each zone has two possible note numbers.
# All confirmed against the actual module on 2026-09-06.
EDGE_NOTES = {22, 26}   # 22 = closed edge, 26 = open edge
BODY_NOTES = {42, 46}   # 42 = closed bow,  46 = open bow
PEDAL_NOTE = 44         # foot chick (pedal close)

# Ghost-hit filtering. Measured on 2026-09-06 while stomping the pedal:
#   - ~30 ms BEFORE the chick: note 46 at vel 9..14 with the pedal still at 0
#   - 3..5 ms AFTER the chick: note 46 at vel 60..78 with the pedal moving fast
#   - up to ~250 ms after: note 42 at vel 30..36 while the pedal settles
# The softest real hit seen so far was vel 23.
GHOST_VELOCITY_MAX = 15     # edge/body hits at or below this velocity are ghosts
CHICK_SPLASH_MS = 60        # edge/body hits this soon after a chick are ghosts
PEDAL_MOTION_CC = 20        # edge/body hits are ghosts if the pedal moved at least this much...
PEDAL_MOTION_MS = 50        # ...within this many milliseconds before the hit
CHICK_GHOST_VELOCITY_MAX = 20  # chick double-triggers come in at vel 16..20
PRE_CHICK_HOLD_MS = 40      # edge/body hits are held this long; a chick arriving meanwhile cancels them
ZONE_CROSSTALK_MS = 100     # a stroke on one zone makes the other zone fire late (measured 73 ms)...
ZONE_CROSSTALK_RATIO = 0.5  # ...at a fraction of the velocity; within the window and under the ratio it is a ghost

# Pedal CC value interpretation. Measured on this TD-17 (2026-09-06):
# value rises as the pedal is pressed, 0 = fully open, 90 = fully closed.
# "About halfway" by feel landed around 8..28, so the response is nonlinear.
CLOSED_IS_HIGH = True
PEDAL_CLOSED_VALUE = 90

# Openness thresholds on the "closedness" scale (same as the CC value here).
TIGHT_MIN = 80   # closedness >= TIGHT_MIN  -> tight
OPEN_MAX = 10    # closedness <= OPEN_MAX   -> open
                 # anything in between      -> mid

# Velocity threshold: at or above this the hit is the hard articulation.
HIGH_VELOCITY_MIN = 100

# MIDI output. Each label maps to the note GetGood Drums One Kit Wonder (Metal)
# expects for that articulation, read off its mapping screen on 2026-09-06.
# Kontakt numbers notes with C3 = 60 (so C-2 = 0). None = don't send.
OUTPUT_PORT_SUBSTRING = "IAC"      # substring of the MIDI output port (e.g. an IAC bus into Reaper)
OUTPUT_CHANNEL = 9                  # 0-based, so 9 = MIDI channel 10
OUTPUT_NOTES = {
    "tight body": 41,   # F1   Tip Tight
    "tight edge": 42,   # F#1  Edge Tight
    "mid body": 43,     # G1   Tip Closed
    "mid edge": 44,     # G#1  Edge Closed
    "open body": 46,    # A#1  Open 2
    "open edge": 47,    # B1   Open 3
    "pedal chick": 48,  # C2   Pedal
    # unused for now: 45 (A1, Open 1), 17 (F-1, CC-controlled hat)
}
OUTPUT_NOTE_LENGTH_MS = 30          # note_off is sent this long after note_on

# UI
FIGLET_FONTS = ["ansi_shadow", "big", "doom", "slant", "standard"]  # first that fits wins
HISTORY_LEN = 12
OPENNESS_COLORS = {"tight": "red", "mid": "yellow", "open": "green", "pedal": "cyan"}


# ---------------------------------------------------------------------------
# Mapping logic
# ---------------------------------------------------------------------------
def closedness(cc_value: int) -> int:
    return cc_value if CLOSED_IS_HIGH else 127 - cc_value


def openness_label(cc_value: int) -> str:
    c = closedness(cc_value)
    if c >= TIGHT_MIN:
        return "tight"
    if c <= OPEN_MAX:
        return "open"
    return "mid"


def zone_label(note: int):
    if note in EDGE_NOTES:
        return "edge"
    if note in BODY_NOTES:
        return "body"
    if note == PEDAL_NOTE:
        return "pedal"
    return None


@dataclass
class Hit:
    openness: str      # tight / mid / open / pedal
    zone: str          # edge / body / chick
    note: int
    velocity: int
    cc: int
    t: float
    ghost_reason: str = None   # None = real hit; otherwise why it was discarded

    @property
    def label(self) -> str:
        return "pedal chick" if self.zone == "chick" else f"{self.openness} {self.zone}"

    @property
    def hard(self) -> bool:
        return self.velocity >= HIGH_VELOCITY_MIN

    @property
    def ghost(self) -> bool:
        return self.ghost_reason is not None


def classify(note: int, velocity: int, pedal_cc: int, t: float = None):
    """Map a note-on to a Hit, or None if it is not a hi-hat note. No ghost filtering here."""
    if t is None:
        t = time.time()
    zone = zone_label(note)
    if zone is None:
        return None
    if zone == "pedal":
        return Hit("pedal", "chick", note, velocity, pedal_cc, t)
    return Hit(openness_label(pedal_cc), zone, note, velocity, pedal_cc, t)


def ghost_reason(hit: Hit, last_chick_t: float, pedal_motion: int, last_stroke: Hit = None) -> str:
    """Why this hit should be ignored, or None if it looks real.

    pedal_motion: how much the CC moved within the last PEDAL_MOTION_MS.
    last_stroke: the previous real (or still pending) edge/body hit, for zone crosstalk.
    """
    if hit.zone == "chick":
        if hit.velocity <= CHICK_GHOST_VELOCITY_MAX:
            return "soft chick"
        return None
    if hit.velocity <= GHOST_VELOCITY_MAX:
        return "too soft"
    if last_chick_t is not None and (hit.t - last_chick_t) * 1000 <= CHICK_SPLASH_MS:
        return "chick splash"
    if pedal_motion >= PEDAL_MOTION_CC:
        return "pedal moving"
    if last_stroke is not None and last_stroke.zone != "chick" and last_stroke.zone != hit.zone \
            and (hit.t - last_stroke.t) * 1000 <= ZONE_CROSSTALK_MS \
            and hit.velocity <= ZONE_CROSSTALK_RATIO * last_stroke.velocity:
        return "zone crosstalk"
    return None


# ---------------------------------------------------------------------------
# MIDI plumbing
# ---------------------------------------------------------------------------
def pick_port(substring: str, names=None, kind="input") -> str:
    if names is None:
        names = mido.get_input_names()
    matches = [n for n in names if substring.lower() in n.lower()]
    if not matches:
        print(f"No MIDI {kind} matching '{substring}'. Available {kind}s:")
        for n in names:
            print(f"  - {n}")
        sys.exit(1)
    return matches[0]


class Sender:
    """Sends mapped notes to the output port. No-op when no port is given."""

    def __init__(self, port_name: str = None):
        self.port = mido.open_output(port_name) if port_name else None

    def send(self, hit: Hit):
        if self.port is None:
            return
        note = OUTPUT_NOTES.get(hit.label)
        if note is None:
            return
        self.port.send(mido.Message("note_on", channel=OUTPUT_CHANNEL, note=note, velocity=hit.velocity))
        threading.Timer(
            OUTPUT_NOTE_LENGTH_MS / 1000,
            self.port.send,
            args=[mido.Message("note_off", channel=OUTPUT_CHANNEL, note=note, velocity=0)],
        ).start()

    def close(self):
        if self.port is not None:
            self.port.close()


class State:
    def __init__(self, sender: Sender = None):
        self.sender = sender or Sender()
        self.lock = threading.Lock()
        self.pedal_cc = PEDAL_CLOSED_VALUE if CLOSED_IS_HIGH else 0  # assume closed until told otherwise
        self.cc_trail = deque()          # (t, cc) samples within the last PEDAL_MOTION_MS
        self.last_chick_t = None
        self.last_hit = None             # last REAL hit (ghosts never land here)
        self.history = deque(maxlen=HISTORY_LEN)   # real and ghost hits, for the table
        self.other_notes = deque(maxlen=4)
        self.pending = None              # edge/body hit waiting out PRE_CHICK_HOLD_MS
        self.dirty = True

    def _commit(self, hit: Hit):
        self.history.appendleft(hit)
        if not hit.ghost:
            self.last_hit = hit
            if hit.zone == "chick":
                self.last_chick_t = hit.t
            self.sender.send(hit)
        self.dirty = True

    def flush(self, now: float = None):
        """Commit the pending hit once its hold time has passed. Call this regularly."""
        if now is None:
            now = time.time()
        with self.lock:
            if self.pending is not None and (now - self.pending.t) * 1000 >= PRE_CHICK_HOLD_MS:
                self._commit(self.pending)
                self.pending = None

    def pedal_motion(self, t: float) -> int:
        cutoff = t - PEDAL_MOTION_MS / 1000
        while self.cc_trail and self.cc_trail[0][0] < cutoff:
            self.cc_trail.popleft()
        if len(self.cc_trail) < 2:
            return 0
        values = [cc for _, cc in self.cc_trail]
        return max(values) - min(values)

    def feed(self, msg, t: float = None):
        if t is None:
            t = time.time()
        with self.lock:
            if msg.type == "control_change" and msg.control == HH_CC:
                self.pedal_cc = msg.value
                self.cc_trail.append((t, msg.value))
                self.dirty = True
            elif msg.type == "note_on" and msg.velocity > 0:
                hit = classify(msg.note, msg.velocity, self.pedal_cc, t)
                if hit is None:
                    self.other_notes.appendleft((msg.note, msg.velocity))
                else:
                    ref = self.pending if self.pending is not None else self.last_hit
                    hit.ghost_reason = ghost_reason(hit, self.last_chick_t, self.pedal_motion(t), ref)
                    if hit.zone == "chick":
                        if self.pending is not None:
                            if not hit.ghost:
                                self.pending.ghost_reason = "pre-chick"
                            self._commit(self.pending)
                            self.pending = None
                        self._commit(hit)
                    elif hit.ghost:
                        self._commit(hit)
                    else:
                        if self.pending is not None:
                            self._commit(self.pending)
                        self.pending = hit
                self.dirty = True


# ---------------------------------------------------------------------------
# Output modes
# ---------------------------------------------------------------------------
def run_raw(port_name: str):
    with mido.open_input(port_name) as port:
        for msg in port:
            print(msg)


def run_plain(port_name: str, out_name: str = None):
    state = State(Sender(out_name))
    printed = 0
    with mido.open_input(port_name) as port:
        while True:
            for msg in port.iter_pending():
                state.feed(msg)
                if msg.type == "note_on" and msg.velocity > 0 and zone_label(msg.note) is None:
                    print(f"{'':<22}(note {msg.note} vel {msg.velocity}, not a hi-hat note)")
            state.flush()
            with state.lock:
                new = list(state.history)[: len(state.history) - printed]
                printed = len(state.history)
            for hit in reversed(new):
                extra = f"(note {hit.note}, vel {hit.velocity} {'hard' if hit.hard else 'soft'}, cc{HH_CC}={hit.cc})"
                if hit.ghost:
                    print(f"{'':<22}{extra}  ghost: {hit.ghost_reason}")
                else:
                    print(f"{hit.label:<22}{extra}")
            time.sleep(0.002)


def figlet(text: str, font: str) -> str:
    lines = pyfiglet.figlet_format(text, font=font).splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def pick_font(console_width: int) -> str:
    probe = ["TIGHT BODY", "PEDAL CHICK"]
    for font in FIGLET_FONTS:
        w = max(len(l) for t in probe for l in pyfiglet.figlet_format(t, font=font).splitlines())
        if w <= console_width - 4:
            return font
    return FIGLET_FONTS[-1]


def render(state: State, font: str, width: int):
    hit = state.last_hit
    cc = state.pedal_cc

    # --- huge label
    if hit is None:
        big = Text(figlet("READY", font), style="dim")
        title = "waiting for a hi-hat hit"
    else:
        color = OPENNESS_COLORS[hit.openness]
        big = Text(figlet(hit.label.upper(), font), style=f"bold {color}")
        title = f"note {hit.note} · vel {hit.velocity} ({'hard' if hit.hard else 'soft'}) · cc{HH_CC} {hit.cc}"
    label_panel = Panel(big, title=title, border_style=OPENNESS_COLORS[hit.openness] if hit else "dim")

    # --- pedal bar
    bar_w = max(20, width - 30)
    pos = min(bar_w, round(closedness(cc) / PEDAL_CLOSED_VALUE * bar_w))
    bar = Text()
    for i in range(bar_w):
        c = i / bar_w * PEDAL_CLOSED_VALUE
        zone_color = OPENNESS_COLORS[openness_label(c if CLOSED_IS_HIGH else 127 - c)]
        bar.append("█" if i < pos else "░", style=zone_color if i < pos else f"dim {zone_color}")
    now = openness_label(cc)
    pedal_line = Text.assemble(
        ("pedal ", "bold"), bar, f"  cc{HH_CC}={cc:>3}  ", (now.upper(), f"bold {OPENNESS_COLORS[now]}")
    )

    # --- history
    table = Table(expand=True, show_edge=False, pad_edge=False, box=None)
    table.add_column("label", style="bold", min_width=12)
    table.add_column("note", justify="right", width=4)
    table.add_column("vel", justify="right", width=4)
    table.add_column("cc", justify="right", width=4)
    table.add_column("", width=20)
    for h in state.history:
        if h.ghost:
            table.add_row(
                Text(h.label, style="dim strike"), Text(str(h.note), style="dim"),
                Text(str(h.velocity), style="dim"), Text(str(h.cc), style="dim"),
                Text(f"ghost: {h.ghost_reason}", style="dim"),
            )
        else:
            table.add_row(
                Text(h.label, style=OPENNESS_COLORS[h.openness]),
                str(h.note), str(h.velocity), str(h.cc), "hard" if h.hard else "",
            )
    other = ", ".join(f"{n} v{v}" for n, v in state.other_notes)
    out = state.sender.port.name if state.sender.port else "none (use --out)"
    if hit is not None and state.sender.port:
        out += f" · sent note {OUTPUT_NOTES.get(hit.label)}"
    footer = Text(f"output: {out}" + (f"    other pads: {other}" if other else ""), style="dim")

    return Group(label_panel, pedal_line, Panel(table, title="recent hits", border_style="dim"), footer)


def run_ui(port_name: str, out_name: str = None):
    console = Console()
    state = State(Sender(out_name))
    font = pick_font(console.width)

    with mido.open_input(port_name, callback=state.feed):
        with Live(render(state, font, console.width), console=console, screen=True, auto_refresh=False) as live:
            while True:
                time.sleep(1 / 100)
                state.flush()
                with state.lock:
                    if not state.dirty:
                        continue
                    state.dirty = False
                    renderable = render(state, font, console.width)
                live.update(renderable, refresh=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=PORT_NAME_SUBSTRING, help="substring of the MIDI input port name")
    ap.add_argument("--raw", action="store_true", help="print every incoming MIDI message")
    ap.add_argument("--plain", action="store_true", help="one line per hit, no live UI")
    ap.add_argument("--out", nargs="?", const=OUTPUT_PORT_SUBSTRING, default=None,
                    help="send mapped notes to this MIDI output (substring); no value = " + OUTPUT_PORT_SUBSTRING)
    args = ap.parse_args()

    port_name = pick_port(args.port)
    out_name = pick_port(args.out, mido.get_output_names(), "output") if args.out else None
    if args.raw:
        print(f"Listening on: {port_name}  (Ctrl+C to quit)")
        run_raw(port_name)
    elif args.plain:
        print(f"Listening on: {port_name}  (Ctrl+C to quit)")
        run_plain(port_name, out_name)
    else:
        run_ui(port_name, out_name)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
