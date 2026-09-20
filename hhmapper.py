"""hhmapper: TD-17 -> GetGood Drums, the whole kit.

Listens to the TD-17, tracks the hi-hat pedal CC, turns every stroke into an
articulation (hi-hat openness x zone, snare head / rimshot / cross-stick, toms,
cymbals) and sends the note GetGood Drums expects for it, in the GroupCtl map
(see OUTPUT_NOTES), through a virtual MIDI port Bitwig sees as a device.

Usage (from the project venv):
    .venv/bin/python hhmapper.py --out       # live Rich UI, sending through the virtual port "hhmapper"
    .venv/bin/python hhmapper.py --out IAC   # ...or through an existing port (substring), e.g. an IAC bus
    .venv/bin/python hhmapper.py             # live UI, no output
    .venv/bin/python hhmapper.py --plain     # one line per hit, no UI
    .venv/bin/python hhmapper.py --raw       # dump every incoming MIDI message
    .venv/bin/python hhmapper.py --probe --out   # play every output note in turn, to check the map by ear
    .venv/bin/python hhmapper.py --port X    # pick an input port by substring (default: TD-17)
    .venv/bin/python hhmapper.py --kit FILE  # zone -> input notes (default: drumhero's ~/.config/drumhero/kit.json)
"""
import argparse
import json
import os
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
CLOSED_HAT_NOTES = {42, 22}  # the numbers the module picks when its own threshold says closed

# Input notes per zone of the kit. The defaults are the TD-17 factory numbers, confirmed
# on the module on 2026-09-06/07; drumhero's kit wizard saves the same zone keys to
# ~/.config/drumhero/kit.json and load_kit() prefers that file, so both programs agree.
KIT_PATH = os.path.expanduser("~/.config/drumhero/kit.json")
FACTORY_KIT = {
    "kick": [36, 35], "snare": [38], "snare_rim": [40], "snare_xstick": [37],
    "hihat": [42, 46], "hihat_edge": [22, 26], "hihat_pedal": [44],
    "crash": [49], "crash_edge": [55], "crash2": [57], "crash2_edge": [52],
    "tom1": [48], "tom1_rim": [50], "floor": [43, 45], "floor_rim": [58, 47],
    "ride": [51], "ride_edge": [59], "ride_bell": [53],
}
ZONE_LABELS = {
    "kick": "kick", "snare": "snare head", "snare_rim": "snare rim", "snare_xstick": "snare cross-stick",
    "crash": "crash L bow", "crash_edge": "crash L edge", "crash2": "crash R bow", "crash2_edge": "crash R edge",
    "tom1": "rack tom", "tom1_rim": "rack tom rim", "floor": "floor tom", "floor_rim": "floor tom rim",
    "ride": "ride bow", "ride_edge": "ride edge", "ride_bell": "ride bell",
}


def load_kit(path: str = KIT_PATH) -> dict:
    """Zone -> input note numbers. The drumhero kit file on top of the factory table
    (zones the wizard skipped keep their factory numbers); 37 is always cross-stick."""
    kit = {k: list(v) for k, v in FACTORY_KIT.items()}
    try:
        with open(path) as f:
            data = json.load(f)
        for k, v in data.items():
            if k in kit and v:
                kit[k] = [int(n) for n in v]
    except (OSError, ValueError):
        pass
    return kit


KIT = load_kit()
EDGE_NOTES = BODY_NOTES = NOTE_ZONE = None
PEDAL_NOTE = None


def _rebind_kit():
    global EDGE_NOTES, BODY_NOTES, PEDAL_NOTE, NOTE_ZONE
    EDGE_NOTES = set(KIT["hihat_edge"])   # 22 = closed edge, 26 = open edge
    BODY_NOTES = set(KIT["hihat"])        # 42 = closed bow,  46 = open bow
    PEDAL_NOTE = KIT["hihat_pedal"][0]    # foot chick (pedal close)
    NOTE_ZONE = {}                        # input note -> zone key; hi-hat zones go by CC instead
    for zk, notes in KIT.items():
        if not zk.startswith("hihat"):
            for n in notes:
                NOTE_ZONE.setdefault(n, zk)


_rebind_kit()

# Snare rim, from the TD17Remapper Bitwig script: a soft rim hit is a cross-stick, a hard one
# a rimshot, each with its velocity spread over the full range.
RIM_SOFT_HARD_THRESHOLD = 50   # rim velocity below this = cross-stick
RIMSHOT_VELOCITY_MIN = 99      # rimshots are sent at this velocity or more
SNARE_HEAD_VELOCITY_MAX = 98   # head hits never reach the rimshot velocity band

# Ghost-hit filtering. Measured on 2026-09-06 while stomping the pedal:
#   - ~30 ms BEFORE the chick: note 46 at vel 7..22 with the pedal still at 0
#   - 3..8 ms AFTER the chick: note 46 at vel 48..94 with the pedal moving fast
#   - up to ~250 ms after: note 42 at vel 22..36 while the pedal settles
# Re-tuned 2026-09-09 on real playing (paradiddles, fast bow/edge alternation, chick +
# stroke together, open hats): every rule keeps what a real stroke measured there.
GHOST_VELOCITY_MAX = 24     # edge/body hits at or below this velocity are ghosts (softest real tap: 29)
CHICK_SPLASH_MS = 10        # edge/body hits this soon after a chick are ghosts...
CHICK_SPLASH_VELOCITY_MIN = 100  # ...unless this loud: a stick landing with the chick reads 113..126
PEDAL_MOTION_CC = 20        # edge/body hits are ghosts if the pedal moved at least this much...
PEDAL_MOTION_MS = 50        # ...within this many milliseconds before the hit...
PEDAL_MOTION_VELOCITY_MIN = 50  # ...unless this loud: real strokes while opening read 116..127
PEDAL_SETTLE_MS = 250       # closed-note hits this long after a chick...
PEDAL_SETTLE_VELOCITY_MAX = 40  # ...at or below this velocity are the pedal settling (real ones: >= 56)
CHICK_GHOST_VELOCITY_MAX = 20  # chick double-triggers come in at vel 16..20
KIT_VELOCITY_MIN = 8        # other pads: below this nothing is sent (sticks resting on a pad read 4..14)
# A hard stroke on one zone makes the other zone fire late. Near: 8..48 ms after the stroke,
# never above 92 whatever the stroke's velocity (2026-09-19, 174 cases in ten days of drumhero's
# MIDI trace, none a chart note; the ratio runs 0.7 at 120 to 1.4 at 55, so a ratio cannot
# describe it): a note on the other zone within CROSSTALK_NEAR_MS at or under
# CROSSTALK_NEAR_VELOCITY_MAX is that stroke heard twice. Late: 73..111 ms after at 28..56 %
# (the hardest strokes, 113..127, ring the longest: 95..111 ms, 2026-09-20),
# at up to 0.70 (drumhero's run logs, 2026-09-20 evening: 51 escapes between 0.58 and 0.80, hat
# open or tight alike, one of them a chart note, at 0.71). The double the 2026-09-09 measurement
# kept (44..90 ms at 63..85 %) is now partly eaten; the user chose one note over two. Within
# 30 ms the other zone falls at any velocity: 8..25 ms at 0.7..1.5 x, one stroke read on both
# zones, nobody plays two hat strokes 30 ms apart. (window ms, max velocity ratio) tiers, in order.
CROSSTALK_ONE_STROKE_MS = 30    # two zones this close are one stroke read twice, whatever the velocities
CROSSTALK_NEAR_MS = 50
CROSSTALK_NEAR_VELOCITY_MAX = 95
ZONE_CROSSTALK = [(115, 0.70)]
# Beater bounce on the kick (2026-09-19, drumhero's Pop punk course, burying the beater): the KD
# pad throws it back and the module sends a kick nobody played, 36..60 ms after the stroke at
# 12..54 % of it (a few up to 93 ms at 13..26 %) and a slower one 160..250 ms after at 12..48 %
# (the beater settling on release). Measured over 5081 kick strokes in the run logs: no real kick
# within 70 ms of another, none under 60 % of the previous within 250 ms. (window ms, max
# velocity ratio to the last real kick) tiers; the reference stays the last real kick.
KICK_BOUNCE = [(80, 0.6), (250, 0.4)]

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

# MIDI output: the GroupCtl map (~/js-projects/bitwig-maps/GroupCtl, GgdDrumMap.java), the
# 16 voices the Launchpad editor draws on "ggd" tracks and that the songs already use.
# Bitwig and Kontakt both call MIDI 60 "C3". Articulations the editor has no row for
# (closed hat, pedal chick, cross-stick) use the notes of the Modern & Massive 2 map
# preset "gruopctl" (~/Music/GGD/Modern & Massive 2/Presets/Map). None = don't send.
#   Kick 1 60  Kick 2 62  Snare 1 61  Snare 2 63  HH1 55  HH2 59  HH3 56  HH4 54
#   Tom 1 66  Tom 2 68  Tom 3 71  Ride 73  Ride Bell 75  China 77  Crash 1 80  Crash 2 82
OUTPUT_VIRTUAL_NAME = "hhmapper"    # the virtual CoreMIDI port hhmapper creates; Bitwig sees it as a MIDI input
OUTPUT_CHANNEL = 9                  # 0-based, so 9 = MIDI channel 10
OUTPUT_NOTES = {
    # hi-hat: openness x zone
    "tight body": 55,        # HH1  Tight Tip
    "tight edge": 55,        # HH1  Tight Tip (M&M2 has Tight Edge at 10, outside the editor)
    "mid body": 59,          # HH2  closed (the M&M2 preset file has Closed Tip at 53; see README)
    "mid edge": 59,          # HH2
    "open body": 54,         # HH4  Open 1
    "open edge": 56,         # HH3  Open 2, the edge rings more open
    "pedal chick": 52,       # M&M2 Pedal Chick (no editor row)
    # drums
    "kick": 60,              # Kick 1
    "snare head": 61,        # Snare 1  centre
    "snare rimshot": 63,     # Snare 2  (rim hit at or above RIM_SOFT_HARD_THRESHOLD)
    "snare cross-stick": 30, # M&M2 Cross Stick (no editor row)
    "rack tom": 66,          # Tom 1
    "rack tom rim": 66,      # Tom 1 (no rim articulation)
    "floor tom": 71,         # Tom 3
    "floor tom rim": 71,     # Tom 3
    # cymbals
    "ride bow": 73,          # Ride
    "ride edge": 73,         # Ride
    "ride bell": 75,         # Ride Bell
    "crash L bow": 80,       # Crash 1
    "crash L edge": 80,      # Crash 1
    "crash R bow": 82,       # Crash 2
    "crash R edge": 82,      # Crash 2 (China is 77 if you would rather have it here)
}
OUTPUT_NOTE_LENGTH_MS = 30          # note_off is sent this long after note_on

# UI
FIGLET_FONTS = ["ansi_shadow", "big", "doom", "slant", "standard"]  # first that fits wins
HISTORY_LEN = 12
OPENNESS_COLORS = {"tight": "red", "mid": "yellow", "open": "green", "pedal": "cyan", "kit": "magenta"}


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
    """edge / body / pedal for the hi-hat, the zone key for the rest of the kit, None if unknown."""
    if note in EDGE_NOTES:
        return "edge"
    if note in BODY_NOTES:
        return "body"
    if note == PEDAL_NOTE:
        return "pedal"
    return NOTE_ZONE.get(note)


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
    def hihat(self) -> bool:
        return self.zone in ("edge", "body", "chick")

    @property
    def label(self) -> str:
        if self.zone == "chick":
            return "pedal chick"
        if self.zone in ("edge", "body"):
            return f"{self.openness} {self.zone}"
        if self.zone == "snare_rim":
            return "snare rimshot" if self.velocity >= RIM_SOFT_HARD_THRESHOLD else "snare cross-stick"
        return ZONE_LABELS.get(self.zone, self.zone)

    @property
    def out_velocity(self) -> int:
        """Velocity sent: rim hits are spread over the cross-stick or rimshot band, head
        hits stay under the rimshot band (TD17Remapper's rule)."""
        v = self.velocity
        if self.zone == "snare_rim":
            if v < RIM_SOFT_HARD_THRESHOLD:
                return _scale(v, 1, RIM_SOFT_HARD_THRESHOLD - 1, 1, 127)
            return _scale(v, RIM_SOFT_HARD_THRESHOLD, 127, RIMSHOT_VELOCITY_MIN, 127)
        if self.zone == "snare":
            return min(v, SNARE_HEAD_VELOCITY_MAX)
        return v

    @property
    def hard(self) -> bool:
        return self.velocity >= HIGH_VELOCITY_MIN

    @property
    def ghost(self) -> bool:
        return self.ghost_reason is not None


def _scale(v, in_min, in_max, out_min, out_max):
    out = out_min + round((v - in_min) * (out_max - out_min) / (in_max - in_min))
    return max(1, min(127, out))


def classify(note: int, velocity: int, pedal_cc: int, t: float = None):
    """Map a note-on to a Hit, or None if it is not a hi-hat note. No ghost filtering here."""
    if t is None:
        t = time.time()
    zone = zone_label(note)
    if zone is None:
        return None
    if zone == "pedal":
        return Hit("pedal", "chick", note, velocity, pedal_cc, t)
    if zone in ("edge", "body"):
        return Hit(openness_label(pedal_cc), zone, note, velocity, pedal_cc, t)
    return Hit("kit", zone, note, velocity, pedal_cc, t)


def ghost_reason(hit: Hit, last_chick_t: float, pedal_motion: int, last_stroke: Hit = None,
                 last_kick: Hit = None) -> str:
    """Why this hit should be ignored, or None if it looks real.

    pedal_motion: how much the CC moved within the last PEDAL_MOTION_MS.
    last_stroke: the previous real edge/body hit, for zone crosstalk.
    last_kick: the previous real kick, for the beater bounce.
    """
    if hit.zone == "chick":
        if hit.velocity <= CHICK_GHOST_VELOCITY_MAX:
            return "soft chick"
        return None
    if not hit.hihat:
        if hit.velocity < KIT_VELOCITY_MIN:
            return "too soft"
        if hit.zone == "kick" and last_kick is not None:
            dt = (hit.t - last_kick.t) * 1000
            for window_ms, ratio in KICK_BOUNCE:
                if dt <= window_ms:
                    if hit.velocity <= ratio * last_kick.velocity:
                        return "beater bounce"
                    break
        return None
    if hit.velocity <= GHOST_VELOCITY_MAX:
        return "too soft"
    if last_chick_t is not None:
        since_chick = (hit.t - last_chick_t) * 1000
        if since_chick <= CHICK_SPLASH_MS and hit.velocity < CHICK_SPLASH_VELOCITY_MIN:
            return "chick splash"
        if (since_chick <= PEDAL_SETTLE_MS and hit.velocity <= PEDAL_SETTLE_VELOCITY_MAX
                and hit.note in CLOSED_HAT_NOTES):
            return "pedal settling"
    if pedal_motion >= PEDAL_MOTION_CC and hit.velocity < PEDAL_MOTION_VELOCITY_MIN:
        return "pedal moving"
    if last_stroke is not None and last_stroke.zone != "chick" and last_stroke.zone != hit.zone:
        dt = (hit.t - last_stroke.t) * 1000
        if dt <= CROSSTALK_ONE_STROKE_MS or (dt <= CROSSTALK_NEAR_MS and hit.velocity <= CROSSTALK_NEAR_VELOCITY_MAX):
            return "zone crosstalk"
        for window_ms, ratio in ZONE_CROSSTALK:
            if dt <= window_ms:
                if hit.velocity <= ratio * last_stroke.velocity:
                    return "zone crosstalk"
                break
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
    """Sends mapped notes to the output port. No-op when no port is given.
    virtual=True creates a CoreMIDI port of that name (no IAC bus needed): Bitwig lists
    it as a MIDI input device as long as hhmapper runs, and reconnects to it by name."""

    def __init__(self, port_name: str = None, virtual: bool = False):
        self.port = mido.open_output(port_name, virtual=virtual) if port_name else None

    def send(self, hit: Hit):
        if self.port is None:
            return
        note = OUTPUT_NOTES.get(hit.label)
        if note is None:
            return
        self.send_note(note, hit.out_velocity)

    def send_note(self, note: int, velocity: int):
        self.port.send(mido.Message("note_on", channel=OUTPUT_CHANNEL, note=note, velocity=velocity))
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
        self.last_hat_hit = None         # last real hi-hat stick hit, the zone-crosstalk reference
        self.last_kick = None            # last real kick, the beater-bounce reference
        self.history = deque(maxlen=HISTORY_LEN)   # real and ghost hits, for the table
        self.other_notes = deque(maxlen=4)
        self.dirty = True

    def _commit(self, hit: Hit):
        self.history.appendleft(hit)
        if not hit.ghost:
            self.last_hit = hit
            if hit.zone == "chick":
                self.last_chick_t = hit.t
            elif hit.hihat:
                self.last_hat_hit = hit
            elif hit.zone == "kick":
                self.last_kick = hit
            self.sender.send(hit)
        self.dirty = True

    def flush(self, now: float = None):
        """Nothing is held back any more (the 40 ms pre-chick hold cost 40 ms of latency on
        every stroke and the velocity floor catches those ghosts); kept for the callers."""

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
                    hit.ghost_reason = ghost_reason(hit, self.last_chick_t, self.pedal_motion(t),
                                                    self.last_hat_hit, self.last_kick)
                    self._commit(hit)
                self.dirty = True


# ---------------------------------------------------------------------------
# Output modes
# ---------------------------------------------------------------------------
def run_raw(port_name: str):
    with mido.open_input(port_name) as port:
        for msg in port:
            print(msg)


def run_plain(port_name: str, out_name: str = None, virtual: bool = False):
    state = State(Sender(out_name, virtual))
    printed = 0
    with mido.open_input(port_name) as port:
        while True:
            for msg in port.iter_pending():
                state.feed(msg)
                if msg.type == "note_on" and msg.velocity > 0 and zone_label(msg.note) is None:
                    print(f"{'':<22}(note {msg.note} vel {msg.velocity}, not in the kit file)")
            state.flush()
            with state.lock:
                new = list(state.history)[: len(state.history) - printed]
                printed = len(state.history)
            for hit in reversed(new):
                extra = f"(note {hit.note}, vel {hit.velocity} {'hard' if hit.hard else 'soft'}, cc{HH_CC}={hit.cc})"
                if hit.ghost:
                    print(f"{'':<22}{extra}  ghost: {hit.ghost_reason}")
                else:
                    print(f"{hit.label:<22}{extra}  -> note {OUTPUT_NOTES.get(hit.label)} vel {hit.out_velocity}")
            time.sleep(0.002)


def run_probe(out_name: str, virtual: bool = False, gap_s: float = 0.9):
    """Play every output note once, printing its label, so the map can be checked by ear
    in Bitwig: each line should sound like what it says."""
    sender = Sender(out_name, virtual)
    if virtual:
        input("Port ready. Arm a ggd track in Bitwig with input 'hhmapper', then press Enter... ")
    print(f"Sending to: {sender.port.name}  (Ctrl+C to stop)")
    seen = set()
    for label, note in OUTPUT_NOTES.items():
        if note is None:
            print(f"{label:<20} (not mapped)")
            continue
        same = f"  (same note as {next(l for l, n in OUTPUT_NOTES.items() if n == note)})" if note in seen else ""
        seen.add(note)
        print(f"{label:<20} note {note:3d}{same}", flush=True)
        sender.send_note(note, 110)
        time.sleep(gap_s)
    sender.close()


def figlet(text: str, font: str) -> str:
    lines = pyfiglet.figlet_format(text, font=font).splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def pick_font(console_width: int) -> str:
    probe = ["TIGHT BODY", "PEDAL CHICK", "SNARE CROSS-STICK", "FLOOR TOM RIM"]
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
        out += f" · sent note {OUTPUT_NOTES.get(hit.label)} vel {hit.out_velocity}"
    footer = Text(f"output: {out}" + (f"    unknown notes: {other}" if other else ""), style="dim")

    return Group(label_panel, pedal_line, Panel(table, title="recent hits", border_style="dim"), footer)


def run_ui(port_name: str, out_name: str = None, virtual: bool = False):
    console = Console()
    state = State(Sender(out_name, virtual))
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
    ap.add_argument("--out", nargs="?", const=OUTPUT_VIRTUAL_NAME, default=None,
                    help=f"send mapped notes: no value = create the virtual port '{OUTPUT_VIRTUAL_NAME}' that Bitwig "
                         "sees as a MIDI input; a value = an existing output port (substring), e.g. an IAC bus")
    ap.add_argument("--kit", default=KIT_PATH, help="zone -> input notes JSON (drumhero's kit file)")
    ap.add_argument("--probe", action="store_true", help="play every output note in turn with its label (needs --out)")
    args = ap.parse_args()

    if args.kit != KIT_PATH:
        globals()["KIT"] = load_kit(args.kit)
        _rebind_kit()
    virtual = args.out == OUTPUT_VIRTUAL_NAME
    out_name = args.out if virtual else (pick_port(args.out, mido.get_output_names(), "output") if args.out else None)
    if args.probe:
        if not out_name:
            sys.exit("--probe needs --out")
        run_probe(out_name, virtual)
        return
    port_name = pick_port(args.port)
    if args.raw:
        print(f"Listening on: {port_name}  (Ctrl+C to quit)")
        run_raw(port_name)
    elif args.plain:
        print(f"Listening on: {port_name}  (Ctrl+C to quit)")
        run_plain(port_name, out_name, virtual)
    else:
        run_ui(port_name, out_name, virtual)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
