# hhmapper

Turns the TD-17 KVX into GetGood Drums articulations in Bitwig: the hi-hat gesture
(pedal CC4 openness x bow/edge, pedal chick, with the module's ghost notes filtered
out), snare head / rimshot / cross-stick, toms, ride bow / edge / bell and two
crashes. Output notes follow the GroupCtl map, so live playing, the drumhero game
and the songs edited with the Launchpad editor all hit the same sounds.

```
.venv/bin/python hhmapper.py --out IAC          # live UI, sending to the first IAC bus
.venv/bin/python hhmapper.py --probe --out IAC  # play every output note with its label: check the map by ear
.venv/bin/python hhmapper.py --plain            # one line per hit
.venv/bin/python hhmapper.py --raw              # every incoming MIDI message
```

Input zones come from drumhero's kit file (`~/.config/drumhero/kit.json`, `--kit`
to override). The map, the thresholds and where they were measured are in
`CLAUDE.md` and at the top of `hhmapper.py`.
