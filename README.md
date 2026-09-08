# hhmapper

Turns the TD-17 KVX into GetGood Drums articulations in Bitwig: the hi-hat gesture
(pedal CC4 openness x bow/edge, pedal chick, with the module's ghost notes filtered
out), snare head / rimshot / cross-stick, toms, ride bow / edge / bell and two
crashes. Output notes follow the GroupCtl map, so live playing, the drumhero game
and the songs edited with the Launchpad editor all hit the same sounds.

```
.venv/bin/python hhmapper.py --out              # live UI; creates the virtual MIDI port "hhmapper"
.venv/bin/python hhmapper.py --out IAC          # ...or send through an existing port (substring)
.venv/bin/python hhmapper.py --probe --out      # play every output note with its label: check the map by ear
.venv/bin/python hhmapper.py --plain            # one line per hit
.venv/bin/python hhmapper.py --raw              # every incoming MIDI message
```

No IAC bus is needed: `--out` alone creates a CoreMIDI port named "hhmapper" that
Bitwig lists as a MIDI input while hhmapper runs (add it as a controller or as the
track input once; Bitwig reconnects to it by name).

Input zones come from drumhero's kit file (`~/.config/drumhero/kit.json`, `--kit`
to override). The map, the thresholds and where they were measured are in
`CLAUDE.md` and at the top of `hhmapper.py`.
