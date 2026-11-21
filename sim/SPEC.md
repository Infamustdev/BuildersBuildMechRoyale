# mechsim: offline Mech Royale simulator — spec

Goal: run full practice-style matches offline, faster than real time, so we can compare bot
versions without spending submissions. Fidelity target: the rules in `../protocol.md` (the
authoritative rulebook) with the numbers from the real `hello.config` recorded in
`../check/stream.jsonl.gz` (first line). Where the rulebook is silent, choose the simplest
reasonable behaviour and list it in `sim/ASSUMPTIONS.md`.

Python 3.12, standard library only. Reuse `../worldmap.py` (`WorldMap`) for terrain, building
heights, line of sight and nav — do not duplicate it.

## Package layout (one owner per file — never edit files you don't own)

| File | Owner | What |
|---|---|---|
| `sim/SPEC.md` | orchestrator | this file |
| `sim/config.py` | engine | loads the real config from `check/stream.jsonl.gz` line 1 |
| `sim/engine.py` | engine | world state, physics, combat, zone, items, observations, sounds |
| `sim/run_match.py` | engine | one match: spawns bots, runs the protocol loop, writes replay + result |
| `sim/batch.py` | engine | N matches (multiprocessing), prints the practice score |
| `sim/ASSUMPTIONS.md` | engine | every place the sim guesses |
| `sim/housebots.py` | house-bot agent | the 15-bot squad AI (in-process) |
| `sim/viewer.html` | viewer agent | standalone top-down replay viewer |
| `research/fps_bot_ai.md` | research agent | classic game-AI research report |

## Interfaces

### 1. External bots (our `bot.py`) — exact `mechfight/1`
Subprocess, JSON lines on stdin/stdout, exactly as protocol.md section 2–6: `hello` → `ready`
(within `hello_budget_ms`), then `obs` every decision (every `decide_every`=2 sim ticks at
`tick_hz`=20) while alive, `end` on death or match end. Enforce `tick_budget_ms` with wall-clock
timing and the coast rules (continuous parts kept, one-shots dropped, idle after `coast_s`).
Answer must echo `tick`. Read stderr into a per-bot log file (don't let pipes fill and block).

### 2. House bots — in-process, same message shapes
```python
# sim/housebots.py
class Squad:
    def __init__(self, cfg: dict, wm: "WorldMap", member_slots: list[int], prey_slots: list[int],
                 difficulty: dict[int, str], seed: int): ...
    def act_all(self, obs_by_slot: dict[int, dict], t: float) -> dict[int, dict]:
        """obs_by_slot: the exact `obs` dict the engine built for each living squad member.
        Returns an `action` dict (protocol section 6 shape, `tick` included) per slot."""
```
- `difficulty[slot]` ∈ {"easy","medium","hard","expert"}. Practice lineup: 4/4/4/3.
- The squad shares vision (it may merge members' observations), never targets its own members,
  hunts prey weakest-first ("one early, more as you thin them out, all of them late").
- The engine also guarantees no damage between squad members (team flag), so friendly fire
  can't happen even if the AI aims badly.

### 3. Engine API (used by run_match/batch)
```python
class Match:
    def __init__(self, cfg, wm, seed, teams: dict[int, int]): ...   # slot -> team id
    def observation(self, slot) -> dict      # protocol `obs` for that slot
    def hello(self, slot) -> dict            # protocol `hello`
    def apply_action(self, slot, action) -> None
    def step(self) -> None                   # advance one sim tick (1/tick_hz s)
    def alive_slots(self) -> list[int]
    def done(self) -> bool                   # one mech left, or duration_s reached
    def placements(self) -> dict[int, int]   # slot -> place (same-tick deaths share a place)
    def replay_frame(self) -> dict           # see 4.
```

### 4. Replay file (JSONL) — consumed by viewer.html
Line 1:
```json
{"type":"meta","version":1,"bounds":{"minX":..,"maxX":..,"minY":..,"maxY":..},
 "buildings":[{"ring":[x0,y0,x1,y1,...],"top":z}, ...],
 "slots":[{"slot":0,"name":"oval-sentinel","team":0,"difficulty":null}, ...]}
```
`buildings` = prisms of kind building/stadium (rings only, holes may be omitted).
Then one line per decision (every 0.1 s):
```json
{"type":"frame","t":12.3,
 "zone":{"cx":..,"cy":..,"r":..,"ncx":..,"ncy":..,"nr":..},
 "mechs":[{"slot":3,"x":..,"y":..,"z":..,"yaw":..,"hp":..,"max_hp":..,"energy":..,"cubes":0,
           "weapons":["gun","laser"],"alive":true,"charging":false,"label":"Figh/Figh/..."}],
 "npcs":[{"id":..,"x":..,"y":..,"hp":..}],
 "shots":[{"kind":"gun|laser|lightning|mortar","from":[x,y,z],"to":[x,y,z],"by":3}],
 "items":[{"kind":"crate|cube|laser|lightning|mortar|drop","x":..,"y":..}],
 "events":["t=12.3 slot 3 killed slot 7"]}
```
`label` = the external bot's `debug.label` if it sent one. Last line:
```json
{"type":"result","places":{"3":13,...},"kills":{"3":2,...},"duration":312.4}
```

### 5. CLI
```
python -m sim.run_match --bot "python -u bot.py" --seed 7 --replay out/m7.jsonl
python -m sim.batch --bot "python -u bot.py" --matches 16 --seeds 1-16 --workers 4
```
`batch` prints per-match place/kills and the **practice score** = mean(16 − place) over matches,
which is the number the real leaderboard shows. Practice mode = our bot in 1 random slot,
15 house bots (4 easy, 4 medium, 4 hard, 3 expert) on one team.

## Must-match rules (check each against protocol.md)
Units m/s/rad; x east, y north, z up; yaw CCW from +x. Mech = cylinder r 2.5 h 9 (scaled by
cubes), eye z+8, muzzle z+7, mortar tube z+9.5, shot origin 3 m out along aim. Torso slews at
turn_rate/pitch_rate; legs independent. Accel ground 58.5 / air 12, gravity 22, step_up 1.2,
slopes > 50° unwalkable, walls slide, jets (boost in air), energy/regen, roofs walkable.
HP 1400, regen 45/s after 5 s without dealing/taking damage (not outside zone), rear hits ×1.5
outside front arc, armor. Weapons gun/laser/lightning/mortar exactly per section "Weapons" incl.
spread, mag/reload, charge/hold/auto-release, chains, cook-off, short, splash + walls,
self-splash 0.5, concussion. Trees block vision (trunks; dense crowns) but never shots.
Vision: near_range 45 any direction or range 200 in the 120° yaw wedge, LOS not through
buildings; 5 s memory of mechs/NPCs. Sounds with carry ranges, bearing noise σ 7°, near/mid/far
buckets, step sounds above 5 m/s every 0.6 s. Zone phases from config with linear shrink, dps,
next circle inside current. Spawns: 16 mechs, ≥130 m apart, inside 0.8 of the zone. 40 floor
weapons (¼ on roofs), 36 crates (¼ roofs, 300 HP, 1 cube, 50% weapon), 12 NPC drones (rules in
protocol), cubes (+4% HP/dmg, cap 10, size +5%/cube), upgrade offers at thresholds with 5 s
expiry (default choice 0), supply drops at 70/140/210 s. Placement: 16 − place scoring,
same-tick deaths share the place. Match ends at one mech left or 480 s.

Performance target: a full 480 s match with 1 external bot + 15 house bots in < 90 s wall time.
