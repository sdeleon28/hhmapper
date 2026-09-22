# hhmapper

Maps the whole TD-17 kit (hi-hat CC4 openness x zone, snare head / rimshot / cross-stick, toms, cymbals)
to GetGood Drums notes in the GroupCtl map, sent through the virtual MIDI port "hhmapper" that Bitwig sees as a device.
Run with `.venv/bin/python hhmapper.py --out`.

## Git: commit and push when a task is done (required)

Commits are allowed here without asking. When a task is finished and verified, commit it
and push to `origin` (current branch, `master`) without being asked; the same rule holds in
`../drumhero`, whose `origin` is github `sdeleon28/batero`. If the repo has no `origin`
yet (hhmapper as of 2026-09-12), commit and say so; do not add a remote on your own.

## Sibling project: drumhero

`../drumhero` (Guitar Hero style trainer) is often worked on from this directory.
After any change there, run `../drumhero/deploy.sh`: it rebuilds `/Applications/drumhero.app`
and relaunches it, because the user opens the game through an rcmd shortcut bound to that
app and expects it to always run the latest code. See `../drumhero/CLAUDE.md`.

## The hi-hat gesture (Roland TD-17 with VH-10, measured 2026-09-06/07)

This is the ground truth for both repos. hhmapper turns it into One Kit Wonder
articulations for Bitwig; drumhero turns it into game hits. Both must agree.

**What the module sends.**

| gesture | note | notes |
|---|---|---|
| stick on the bow (body), pedal closed | 42 | |
| stick on the bow, pedal open | 46 | |
| stick on the edge, pedal closed | 22 | Roland-specific |
| stick on the edge, pedal open | 26 | Roland-specific |
| pedal chick (foot close) | 44 | velocity from the stomp |
| pedal position | CC4 | 0 = fully open, 90 = fully closed on this pedal, nonlinear: "half by feel" reads 8..28 |

The module picks 42/46 or 22/26 by its own closed/open threshold; the CC value
at the moment of the stroke is the real openness. Zone = edge (22/26) or bow
(42/46); openness = from the last CC4 value, not from the note number.

**Openness classes** (closedness = CC4 value): tight >= 80, open <= 10, mid in
between. hhmapper maps zone x openness to: Tip/Edge Tight (41/42), Tip/Edge
Closed (43/44), Open 2/3 (46/47), Pedal (48) in One Kit Wonder, Kontakt C3 = 60.

**Ghost notes the pedal produces** (nobody hit the hat):

| when | note | velocity | how to catch it |
|---|---|---|---|
| ~30 ms before the chick, pedal still at CC 0 | 46 | 7..22 | velocity floor |
| 3..8 ms after the chick, pedal moving fast | 46 | 48..94 | window after chick |
| up to ~250 ms after the chick, pedal settling | 42 | 22..36 | settle window, soft closed note |
| 8..25 ms after a stroke, the other zone, one stroke read twice | 42 or 46 (or 22/26) | 0.7..1.5 x the stroke, up to 122 | within 30 ms: drop, any velocity |
| 8..48 ms after an edge stroke (the mass at 40..48), any strength | 42 or 46 | up to 92, whatever the stroke: 70 % of a 120, 140 % of a 55 | near crosstalk: 50 ms window, velocity cap |
| 73..93 ms after an edge stroke, one or two of them, nearly every hard stroke; 95..111 ms after a 113..127 | 42 or 46 | 28..70 % of the stroke (a hat left open under double kick swings and reads higher) | zone crosstalk |
| ~55 ms after a hard bow stroke, rare | 22 | ~40 % of the stroke | zone crosstalk |
| chick double trigger ~110 ms after a chick | 44 | 16..20 | chick velocity floor |

**Real strokes that look like ghosts** (measured 2026-09-09 on paradiddles, fast bow/edge
alternation, chick + stroke together, open hats; the rules must keep all of these):

| gesture | what arrives |
|---|---|
| softest real tap | 29 (taps in fast doubles 29..48; a missed tap can read 7..18, lost) |
| bow tap right after an edge accent (fast alternation) | 42 at 44..90 ms, 63..85 % of the accent |
| stick landing together with the chick | 44 then, 3..5 ms later, 46 at 113..126 |
| stick landing just after the chick | 44, the 46 ghost at 3..8 ms, then 42 at 11..52 ms, velocity 56..127 |
| stroke while the pedal is still opening (CC moving 20+ in 50 ms) | 46 at 116..127 |

Resting sticks on a pad: 4..14.

**Filter rules, identical in both repos** (hhmapper.py constants, drumhero/ghost.py):

- hi-hat stick note with velocity < 25: drop (both repos; hhmapper no longer holds
  strokes 40 ms for a following chick, that hold cost 40 ms of latency on every stroke)
- hi-hat stick note within 10 ms after a chick (44) with velocity < 100: drop (chick splash)
- closed-hat note (42/22) within 250 ms after a chick with velocity <= 40: drop (pedal settling)
- hi-hat stick note while CC4 moved >= 20 within the last 50 ms, velocity < 50: drop
- hi-hat stick note on the other zone than the previous stroke, within 30 ms at any velocity:
  drop (one stroke read on both zones, 2026-09-20 evening: 8..25 ms apart at 0.7..1.5 x, up to
  122; nobody plays two hat strokes 30 ms apart; in the run logs 7 such, 6 strays, one GOOD
  whose edge twin had the note anyway).
- hi-hat stick note on the other zone than the previous stroke, within 50 ms at velocity
  <= 95: drop (near crosstalk, since 2026-09-19: one stroke heard twice, in Bitwig as two
  notes. Measured over ten days of drumhero's MIDI trace, 118k notes: 174 bow notes within
  50 ms of an edge stroke, none over 92, none a chart note, the charts' closest hi-hat
  figure being 178 ms. It costs the real double that lands edge then bow within 50 ms at 95
  or under; until that date it was let through for that double's sake, and a friend who
  plays every hat stroke hard on the edge got a stray per stroke.)
- hi-hat stick note on the other zone than the previous stroke, within 115 ms at
  <= 70 % of its velocity: drop (late zone crosstalk; the reference stays the last real
  stroke, so chained ghosts fall too; 95 ms until 2026-09-20, when the hardest strokes,
  113..127, were seen ringing the other zone at 95..111 ms and 28..50 %, six strays a
  run on the kick gallop; 58 % until that evening, when the run logs showed 51 escapes at
  58..80 %, hat open or tight alike, 50 of them strays and one a chart note at 71 %: the
  double-kick levels leave the hat open and swinging, and a chained 46 at 60 % took the
  reference with it). This eats the soft real double the 2026-09-09 measurement kept
  (44..90 ms at 63..85 %): the user chose one note over two. Above 70 % nothing is dropped
  past 50 ms.
- kick note within 80 ms after the last real kick at <= 60 % of its velocity, or within
  250 ms at <= 40 %: drop (beater bounce, since 2026-09-19: burying the beater on the KD pad
  throws it back, 36..60 ms later at 12..54 %, a slower settle 160..250 ms later; on an
  acoustic drum a buried beater sounds once. Over 5081 logged kick strokes no real kick came
  within 70 ms of another or under 60 % of the previous within 250 ms; the fastest chart
  figure is 94 ms apart. The reference stays the last real kick.)
- snare note within 70 ms after the last real stroke **on the same zone** (head 38, rim 40) at
  <= 60 % of its velocity: drop (pad rebound, since 2026-09-22: a hard stroke comes back 41..60 ms
  later at 30..60 %, the median delay 51 ms on every one of the thirteen days of the MIDI trace and
  the median ratio 0.40..0.45, which is a pad retriggering, not a hand. Only strokes at 80 and over
  do it, and it is growing: 0.04 % of the day's snare strokes on 09-20, 1.78 % on 09-21, 2.65 % on
  09-22, the highest logged, so the module's retrigger-cancel is worth a look as well. Head and rim
  do not ring each other (1106 snare notes that day, not one 38/40 pair within 200 ms), hence a
  reference per zone; the reference stays the last real stroke, so a chain falls whole. Over the
  whole trace the rule drops 367 strays and 81 notes the judge had accepted, but 78 of those 81 are
  followed within 200 ms by a louder snare stray: the echo had taken the chart note and the real
  stroke behind it was counted a stray, so dropping it gives the note back. The fastest snare
  figure any chart writes is 89 ms apart.)
- chick with velocity <= 20: drop (hhmapper)
- any note with velocity < 8: drop (drumhero)

A change to a threshold goes to both repos and to this section. Validated 2026-09-09 by
replaying two recorded takes (306 real strokes) through both filters: no real stroke
dropped, every ghost above still caught. Record a take with `mido` (timestamps in ms)
and replay it through `State` / `GhostFilter` before touching a number. Since 2026-09-19 the
MIDI trace (`~/Library/Logs/drumhero-midi.log`: wall time, note, velocity, the filter's
reason or the judge, CC4) is the larger check: replay it and count what falls that was
judged PERFECT / GOOD / OK.

The articulation labels (tight/mid/open x body/edge, pedal chick) are shared
verbatim: hhmapper's `OUTPUT_NOTES` keys and drumhero's `chart.HH_ARTS` (its
hi-hat lessons ask for them by name and judge them). Rename in both or neither.

**Kit wizard rule (drumhero).** The wizard has one step per zone of the TD-17
(17 zones, `chart.ZONES`): the hi-hat has three, bow, edge and pedal. In the
bow step hearing 42 or 46 assigns both; in the edge step 22 or 26 assigns
both; 44 is only assigned in the pedal step. A number heard in two steps goes
to the later zone. Charts still refer to instruments (kick/snare/hihat/crash,
plus tom1/floor/ride), and an instrument accepts every note of all its zones.
Menu navigation ignores hits under velocity 25; every other hit is one action, immediately,
no debounce (the user wants the game to feel instant); every hit above 15 is heard.

## The rest of the kit: output map (hhmapper, 2026-09-07)

Input zones come from drumhero's kit file (`~/.config/drumhero/kit.json`, same zone
keys; factory TD-17 numbers as fallback, `--kit FILE` overrides). Note 37 is always
snare cross-stick.

Output is the **GroupCtl map** (`~/js-projects/bitwig-maps/GroupCtl/.../GgdDrumMap.java`),
the 16 voices the Launchpad editor draws on "ggd" tracks and that the existing songs
(e.g. into-the-darketa) use. Bitwig and Kontakt both call MIDI 60 "C3":

| voice | note | fed by |
|---|---|---|
| Kick 1 | 60 | kick |
| Snare 1 | 61 | snare head (velocity capped at 98) |
| Snare 2 | 63 | snare rim hit at velocity >= 50, sent at 99..127 (rimshot) |
| HH1 | 55 | tight body / edge |
| HH2 | 59 | mid (closed) body / edge |
| HH3 | 56 | open edge (Open 2) |
| HH4 | 54 | open body (Open 1) |
| Tom 1 | 66 | rack tom head and rim |
| Tom 3 | 71 | floor tom head and rim |
| Ride / Ride Bell | 73 / 75 | ride bow and edge / bell |
| Crash 1 / Crash 2 | 80 / 82 | crash L / crash R, bow and edge |
| unused rows | Kick 2 62, Tom 2 68, China 77 | |

Articulations the editor has no row for take the notes of the user's Modern & Massive 2
map preset "gruopctl" (`~/Music/GGD/Modern & Massive 2/Presets/Map/gruopctl.preset`):
pedal chick 52, snare cross-stick 30 (rim hit under velocity 50, velocity spread to
1..127). That preset also says Closed Tip = 53 while HH2 is 59: if closed hats are
silent in Bitwig, switch "mid body/edge" to 53. The snare rim rule is the one from the
TD17Remapper Bitwig script in the same repo.

`hhmapper.py --probe --out` plays every output note with its label, to check the
map by ear on both the One Kit Wonder (Kontakt) and Modern & Massive 2 tracks.
