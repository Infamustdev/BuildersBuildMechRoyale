# Tuning experiments

Practice = 48 matches on fixed private seeds vs 15 house bots that never fight each other,
so score ≈ number of house bots dead when we die. One change per submission; keep a change
only if it beats the best score by more than the noise.

Submit: track page → paste `https://github.com/Infamustdev/BuildersBuildMechRoyale` (scores the
latest commit on `main`). Limit: 4 per hour, one queued at a time.

| # | Commit | Change vs baseline | Score | Notes |
|---|--------|--------------------|-------|-------|
| 0 | f79563a | Baseline (flee_adv 0.6, low-HP flee below 1.3, min_hit_prob 0.65) | 2.58 | Starter = 1.15 |
| 2 | 7aa57c2 | flee_adv 0.6→0.4, low_hp_flee_adv 1.3→0.9 | 2.38 | Worse or noise; reverted |
| 3 | 9fbd759 | Baseline retreat; no hit-chance threshold: fire every lined-up shot; auto-lock onto any visible enemy in range | 2.83 | **Best.** Recorded match: 66 fire decisions vs 46 |
| P1 | f512edb | PROBE hide: silent creep to covered edge spot, never shoot/loot | 0.04 | House bots never die without us: **practice score = our kills**. Hiding doesn't save us either |
| 4 | (this push) | #3 + cover fighting: peek, shoot, duck back to reload/charge laser/heal | | Planner finds cover on 59% of recorded ticks, 2.3 ms |
| 1 | (re-push of baseline) | Same code as #0, measures noise | | Optional |
| 4 | | Retreat only below 25% HP | | |
| 5 | | w_roof 1.2→0 (no roofs) | | |
| 6 | | finish_hp_frac 0.25→0.4, finish_max_dist 140→200 | | |

**Best so far:** #3, 2.83 (9fbd759). After the probe, set `PROBE["hide"] = False` to get it back.

**Noise:** unknown (fill in from #1: |score #1 − score #0|)

## Simulator results (same 16 seeds, `python -m sim.batch --matches 16 --seeds 1-16`)

| Variant | Sim score | Notes |
|---|---|---|
| Exp 4 (current main) | 3.56 (8 seeds: 2.875 vs real 2.88) | Baseline |
| Snowball: bigger loot detours, farm drones/crates until 70 s, damage-first upgrades | 2.75 | Fewer kills (1.31 vs 1.81); farming exposes us. Rejected |
| Damage-first upgrades only | 3.25 | No gain. Rejected |

**Simulator recalibrated** (drones and zone can't kill house bots; house aim error ×1.6): exp-4 bot = **3.00 score, 3.00 kills** on seeds 1–16 (real: 2.88). The snowball results above came from the uncalibrated sim (house bots dying to drones inflated scores) and need re-running.

### Calibrated sim, seeds 1–16 (16 matches each)
| Variant | Score |
|---|---|
| Exp 4 baseline, run A | 3.00 |
| Exp 4 baseline, run B (identical code) | 1.94 |
| Snowball loot only | 2.50 |
| Snowball loot + damage upgrades | 2.38 |
| Damage upgrades only | 2.44 |

**Noise:** the same bot on the same seeds scored 3.00 and 1.94, so 16-match results are ±1 noise:
all snowball variants are inside it. Use 64+ matches per variant before deciding anything.

### Danger map + utility planner vs exp 4 (calibrated sim, seeds 1–16, 16 matches each)
| Bot | Run A | Run B | Earlier runs | Mean |
|---|---|---|---|---|
| Current (danger map, utility planner, fc74e24) | 1.81 | 1.75 | — | **1.78** |
| Exp 4 (610fdb8, real 2.88) | 1.94 | 2.38 | 3.00, 1.94 | **2.32** |

The planner is not better than exp 4 in the sim, probably slightly worse. Exp 4 stays the version to submit.

## Exp 7 — item pull: distance-scaled danger discount + approach commitment

**Why.** `DANGER.item_cool` was a flat −0.5 over a 3×3 cell patch and the total was clamped
`max(0.0, c)`, so any enemy viewshed (`seen_cost` 4.0) erased it completely. The planner also
re-scored from scratch each tick with a *constant* `stick_bonus`, so travel already spent counted
for nothing: the bot walked up to a cube and turned away at the last metre.

**Change.**
- `danger.py`: `cool` → `pull`, a well around each wanted item, `pull_max * (1 - d/R)^2`
  (`pull_max` 0.85, `pull_radius` 24 m), applied as a *discount* — `c = max(0, hazard) * (1 - pull)`.
  Never negative (A* stays valid) and the clamp can't erase it. Nearest 24 items only (cost guard).
- `utility.py`: stick bonus scales with ground already covered —
  `stick_bonus * (1 + commit_gain * (1 - d/d0))`, `commit_gain` 3.0, `d0` recorded in `bot.plan_d0`.

**Result.** `check.py` PASS, max 11.7 ms. Sim 16-match, seeds 1-16: **1.31, 1.81**
(same code, same seeds — the noise is that wide). exp 4 on the same seeds: **2.06**.
Previous `main`: 1.81, 1.75. So: no measurable gain, still behind exp 4. The mechanism is fixed
but it is not what is costing us the score. Cubes-collected per match was not measured — worth
doing directly rather than reading it off a noisy placement score.

## Exp 8 — opening aerial survey (user's idea)

**Why.** Crates, cubes and pickups spawn randomly each match and the server lists them only
while they are in line of sight, so none of it can be precomputed from `/arena`. Vision is a
120 deg *yaw* wedge with no pitch limit, and `turn_rate` 4 means a full circle takes 1.57 s —
so one spin from above the rooflines is a map-wide item census, and `tracker.py` keeps it.
`energy_regen` 18/s after a 0.6 s delay repays a 30 m climb (~45 energy) in about three seconds.

**Change.** `SCAN` in tuning.py, `Bot.scan_step` + a branch at the top of `act`: jump, jet to
`alt` 30 m, spin at the saturated turn rate the whole way up and down, no firing, land on the
spawn. Aborts on any damage (`abort_hp` 0.97), on being engaged before leaving the ground, or
at `max_s` 9 s. Jetting is latched off once the top is reached — without the latch it re-fired
on the way down (`alt < 30` was true again while falling) and airtime went 5.4 s -> 7.8 s.

**Cost measured (seed 21).** peak 32.9 m at t=3.6 s, airborne 5.4 s, energy never below 55.
Jets are loudness 90, the loudest tier, so the whole squad gets a bearing on the spawn.

**Result.** `check.py` PASS, max 12.3 ms. Single matches only so far (noise is +-0.5, do not
read anything into these): seed 21 went place 15 / 1 kill / died 91 s before the scan, to
place 13 / 3 kills / 128 s and place 14 / 2 kills / 187 s after. Needs 2 x 16-match runs.

## Exp 9 — minimap heatmap, and the camp-pinning bug Exp 7 introduced

**Dump was dead.** `write_mind` still read `self.dm.cool`, renamed to `pull` in Exp 7, so every
snapshot threw and `mind.jsonl` held nothing but its meta line — the overlay has been blank
since. Fixed, and each cell now dumps `[K, cost, pull]`.

**Heatmap.** `sim/viewer.html` paints one ramp over the cost the planner actually pays:
light blue (0) -> amber -> red (8+), alpha raised on cells with a pull discount so wells with
no residual cost still show. Item wells read as blue holes punched into the red. The earlier
threshold version was wrong: a fringe cell with cost 5.4 and pull 0.06 painted blue.

**Why the bot stands in one place.** Three things, compounding:
1. `bot.py` `hold`: with a "camp" or "cover" plan inside `arrive_radius` (6 m), move is set to
   zero outright. Deliberate — under 5 m/s is silent — but it means arriving ends movement.
2. Every option is charged `w_travel * d / walk`, and the camp we are standing on has d = 0.
   Its score therefore *peaks* at the moment we reach it, while every alternative still pays
   its own travel. The planner builds its own local optimum and then sits in it.
3. Exp 7's `commit_gain` made that 4x worse: `prog = 1 - d/d0` is 1.0 when we are on the spot,
   so the camp drew `stick_bonus * (1 + 3) = 4.0`. **Fixed** — the growing part now applies only
   to destinations consumed on arrival (`HOLD_KINDS = camp, cover, flank` excluded).
Point 2 is structural and still there. `r_cover_hurt` 16.0 compounds it: once hurt, cover
outscores almost everything and cover also holds position (check.py run: Retreating 55%).

## Exp 10 — exploration as a reward: standing still can no longer win

**The flaw.** Every option was charged `w_travel * d / walk`, so the spot we are *standing on*
costs nothing while every alternative pays. Its score therefore peaked the moment we arrived,
and nothing could outbid it. The planner built a local optimum and parked in it. No threshold
or special case caused this — it fell out of scoring destinations by position alone, with
nothing in the score for what moving actually earns.

**The fix: give moving a reason that grows.** `recon.py`, a 40 m knowledge grid over the map
holding the last time we had line of sight to each cell, refreshed a slice (16 cells) per tick
round-robin — fov + range + `wm.los` at step 4. Ground we have not looked at recently is the
only place a new cube or weapon can come from (items spawn randomly each match and are listed
only while in sight), so its value rises with staleness: `r_explore` 7.0 x `min(1, age/tau)`,
`tau` 35 s, the 6 stalest reachable cells offered as "look" options each replan.

This closes the loop by itself: arriving makes a cell fresh, its reward collapses to zero, the
next stalest cell wins. Movement is sustained by what movement pays, not by a rule saying move.
And sitting anywhere makes the whole rest of the map go stale around us.

**Plus: held spots decay.** `bot.hold_mult` — camp and cover reward e-folds with time held
(`hold_decay_s` 6 s, floor 0.15). Getting to cover and taking stock is the value; that is spent.

**Result.** `check.py` PASS. State mix moved hard: Retreating 55% -> 27%, Rotating 34% -> 62%.
Seed 21, 185 s match: stillness outside fights is 16 still-runs totalling 16.3 s over 1 s, and
the longest single stretch is 5.9 s (shooting a crate holds position by design and counts here).

**Timing note.** lat_max wanders 18-32 ms run to run, but the very first run of the day on the
*unmodified* bot hit 45 ms, so this is machine jitter, not the new code. Refresh budget drops
16 -> 6 cells after a slow tick anyway.

## Exp 11 — danger that responds to the world (and a hard fact about the aerial scan)

**Measured: the aerial scan cannot pay.** From the seed-21 spawn, only **10 of the match's 76
items are within the 200 m vision range at all**. Line of sight to those 10, by height:
ground 6, 15 m 8, 30 m 8, 45 m 9, 60 m 9, 90 m 9. So the climb buys **two items** over simply
standing still and spinning, and the ceiling is 9 however high we go. The binding constraint is
*range*, not occlusion — the map is far larger than we can see. The 360 spin is where the value
is (6 of 10, free, silent); the climb costs 5.4 s airborne and loudness 90 for +2.
(`scratchpad/losalt.py` recomputes this for any replay.)

**Danger now scales with the enemy's health.** A viewshed is multiplied by
`threat_floor + (1 - threat_floor) * hp_frac` (`threat_floor` 0.2), refreshed every tick whether
or not the viewshed is recast. Hurting someone now *opens ground up* instead of leaving it as
frightening as it was at full health — so we press a wounded enemy rather than letting them
break off, heal and come back.

**The map can go negative.** `DangerMap.signed(K)` = `max(0, hazard) * (1 - pull) - pull_bonus * pull`
(`pull_bonus` 3.0). An item nobody is watching is now worth *less than nothing* to stand on, not
merely "not scary". `cost(K)` clamps at 0 for A* (negative edges break it); the planner uses the
signed value, which is where it changes decisions. Measured: 44,820 negative cells in the run,
most negative -2.5. The viewer paints them bright blue.

**Cubes heal, so they are worth more when hurt.** `r_cube * n * (1 + cube_heal_gain * (1 - hp))`,
gain 2.0, so x3 at 0 HP. This is the direct answer to killing someone and walking away from the
cubes: cover was winning because its reward rises with damage taken and the cube's did not,
even though a cube *is* a heal. Cubes collected on seed 21 went 0 -> 2.

**Still broken, and now the biggest lever: `Fighting` bypasses the planner entirely.**
Trace of the same match: `Figh` from t=8 to past t=60 unbroken, target T11, advantage climbing
2.0 -> 8.0, nobody's HP moving, `cubes=0`. `self.plan` is only computed when the move state is
not Fighting/Finishing, so the dump shows `plan=None` for the first 30 s. The bot enters Fighting
on `sit.engaged and target["engaged"]` and 15 squad bots sharing vision mean that is almost
always true. **That is why it does not loot early**, not the planner's weights. The fix is to make
fighting an option the planner scores against looting rather than a state that switches it off.

## Exp 12 — fighting becomes an option, not a mode

**Change.** `utility.plan` gains a `fight` option scored where we stand (so it pays no travel,
and the danger of the spot counts against it like any other):
`r_fight 5.0 * min(3.0, adv) * (1 + 2.0 * (1 - enemy_hp_frac))`. `act` no longer branches on the
move state: the planner runs every replan whether or not we are engaged, and the fight movement
(cover_move / fight_move) runs only when the fight option *wins*. Aiming and firing are decided
further down regardless, so walking to a cube never costs us our shots.

**It does what it was meant to.** Seed 21 went from "Figh unbroken t=8..60+, cubes 0" to a trace
that interleaves `figh`, `look` and `cube`: place 11, **5 kills**, 4 cubes (best on this seed by
a wide margin; it had been 1-3 kills and 0-2 cubes).

**A performance trap it exposed.** First batch after the change: 1.375 / 1.125 with batch wall
time doubled, 49.4 ms worst decision and late answers. Profiling (`scratchpad/prof_bot.py`,
cProfile over one match) found `wm.nidx` called **920,288 times** — `DangerMap.update` was
rebuilding every item pull well from scratch on every tick. Items do not move, so the wells are
now cached on the tuple of wanted item positions and rebuild only when we learn about or take
one. Also capped `line_cost` at `line_samples` 20 probes per route and dropped
`max_recasts_per_tick` 2 -> 1. Worst decision 49.4 -> 27-31 ms, late answers 2-12 -> 0.

**Honest result.** 16-match runs, seeds 1-16, after the speed fix: **1.188, 1.500**.
Before this change (Exp 7-11 code): 1.31, 1.81. exp 4 on the same seeds: **2.06**.
So the restructure is clearly right in behaviour and clearly helps on some seeds, but on this
seed set it has *not* recovered the gap to exp 4, and `main` as a whole is still behind it.
`610fdb8` (exp 4, 2.88 real) remains the safe submission.

## Exp 13 — exposure, aim priority, purposeful fight movement, contested items

**Exposure: a spot we were seen in gets worse the longer we stay.** `DangerMap.mark_exposure`
accumulates `exposure_rate` 0.5/s per watching enemy into a 20 m patch where we stand, capped at
4.0, fading with a 12 s time constant once nobody can see us, and costed at 1.5 per unit. The
squad shares vision and converges, so danger is not about who can see us *now* but how long they
have known where we are. This is the mechanism behind getting swarmed, and now the planner can
see it coming.

**Aim: being shot at outranks the idle sweep.** New `Checking` aim state, placed above
`SearchingItems` and `Scanning`: with `unseen_dps > 0` and a bearing, swing to the bearing and
then sweep slowly from there. *It did not trigger once in the sampled match* — auto-lock means
that when we are being shot we can usually already see someone, so `shoot_state` is `Fighting`
and the aim is already on a target. The branch is correct but rarely reached; worth revisiting
only if a trace shows us sweeping while taking damage.

**Fight movement has somewhere to go.** Two changes to `fight_move`: a strafe flip now picks the
side that circles behind the target 65% of the time (`rear_mult` is 1.5, so the circle is not
symmetric), and `safer_step` probes five directions on that side at 14 m and bends the strafe
toward the coolest, weighted `fight_cover_bias` 0.55. Strafing in place is what a target does.

**Items: rank and contest.** A weapon is now worth `r_weapon_rank` 4.0 per rank of the weapon
*itself*, not only per rank of upgrade — the laser decides every later fight. Both cubes and
weapons are scaled by `uncontested()`: our distance against the nearest enemy's, over an 80 m
span, floored at 0.25. An item someone is standing on is mostly theirs; one we have a clear run
at is as good as in hand.

**Result.** `check.py` PASS, max 13.2 ms, no late answers in clean runs. 16-match, seeds 1-16:
**1.688, 1.438** (kills 1.69, 1.44). Previous build: 1.188, 1.500. exp 4 same seeds: 2.06.
Best single matches so far: seed 23 place 10 / 6 kills, seed 21 place 11 / 5 kills.
Trending the right way and still short of exp 4.

## Exp 14 — crates reweighted, and what the worst seed shows

**Crates.** A crate is 300 HP for **1 cube, plus a weapon half the time** (mortar/laser/lightning
equally likely). In this file's currency that is `r_cube` 6.0 + 0.5 x ~8 for the average weapon
= about 10, so `r_crate` 2.5 was off by roughly 4x. Set to 7.0 (discounted for the time spent
shooting it and the 220 m noise of it breaking) and scaled by `uncontested()` like other loot.

**Correction to Exp 8.** I called jets "the loudest tier". That is wrong and it was the main
argument against the climb. The sound table is *carry range*: step 40, reload 60, **jet/boost 90**,
lightning 160, npc_gun 200, crate 220, gun 320, laser 380, mortar/explosion 420, death/drop 600.
A jet is one of the *quietest* things in the game and reaches almost nobody on a map this size.
The climb's real cost is being visible from far away, not the noise.

**Per-seed, seeds 1-16** (same build): 0,0,0,1,0,0,1,0,0,3,1,3,0,0,1,2 -> mean 0.81. Note the
spread against the two earlier runs of the *same* build (1.688, 1.438). Eight zeros.
Worst: seed 15, dead at **20.8 s**, 0 kills.

**What seed 15 shows — drone damage is invisible to the judgement system.**
```
t= 2.0 hp=1338  Figh/Figh/Figh/figh  T14 a3.8
t= 6.0 hp=1188  Rota/Sear/Hold/weap        <- walking to a weapon
t=16.0 hp= 875  Rota/Sear/Hold/weap        <- still walking, still being shot, shoot=Hold
t=18.0 hp= 755  Rota/Figh/Figh/weap  T12 a0.4
t=20.8 dead
```
Ten seconds of taking ~15 HP/s with `shoot_state = Hold`, never shooting back, never looking.
Cause: `assess` filters NPC attacks out of `unseen_dps` (`a[3] not in ("npc",)`), so a drone
shooting us produces no threat, no target, and no `Checking` aim — and because the aim stays on
the item, we never see the thing hurting us. Self-reinforcing.

This matters more than it looks: in the sim a drone projectile is dropped against team 100
(`engine.py:1302`), so **drones damage us and never the house bots**. Every point is a pure
differential loss, and a drone is 2 cubes if we turn and kill it. Not yet fixed.

## Exp 15 — drone damage counts (first build to beat exp 4 in the simulator)

**Change.** `assess` now records `npc_dps` and `npc_by` from attacks with weapon `npc`. They
stay out of `unseen_dps` deliberately (a drone must not make us flee a mech fight) but they are
no longer invisible:
- the `Checking` aim fires on drone damage as well, so we look at what is hurting us;
- the planner offers the drone that is shooting us **regardless of HP** -- the `farm_min_hp`
  gate is about whether a drone is worth a detour, not about whether to deal with one already
  chewing on us -- at `r_npc` 5.0 + `r_npc_biting` 14.0;
- `r_npc` raised 3.0 -> 5.0 on its own merits (a drone is 2 cubes).

Also fixed an indentation slip introduced while editing: the drone loop had landed inside the
crate loop, offering every drone once per visible crate.

**Seed 15, the worst seed, before and after:**
```
before  t= 6 hp=1188 Rota/Sear/Hold/weap  ... t=16 hp= 875 (same)  dead 20.8 s
after   t= 2 hp=1338 Rota/Figh/Loot/npc   ... t=12 hp=1512 Rota/Scan/Hold/look
```
It turns, kills the drone, takes the cubes, and is at **full upgraded HP (1512) at t=12** where
it used to be at 875 and falling. It still dies on this seed at 26.8 s, but now to a mech fight
it loses (adv 0.4-0.8), which is an ordinary loss rather than a bot ignoring its own health bar.
Note the aim/shoot states coexisting in that trace (`Figh` aim with `Loot` shooting) -- the thing
the state machine could not express before.

**Result — 16 matches, seeds 1-16:**

| build | run 1 | run 2 |
|---|---|---|
| exp 4 (610fdb8), fresh baseline | 1.938 | - |
| previous build | 1.688 | 1.438 |
| **this build** | **2.250** | **2.125** |

First time `main` has beaten exp 4 in the simulator. Two runs, both above it, against a fresh
baseline on the same seeds — but the spread on this seed set has been as wide as 0.81 to 1.69
for one unchanged build, so this is encouraging rather than settled.
`check.py` PASS, max 12.7 ms.

## Exp 16 — steered descent (kept) and "climb as high as we can" (measured, rolled back)

**Steered descent — kept.** Once the top is latched, the fall is free steering, so `pick_landing`
scores 16 bearings x (0, 25, 50, 80 m) by `dm.at` plus `glide_far_cost` 0.8 per 100 m and the
mech glides to the coolest reachable cell. `dm.at` already prices enemy sight lines weighted by
their health, sound heat, where we have been seen, and the negative pull of unwatched loot, so
choosing a landing spot is one comparison rather than a set of rules. Standing still is one of
the candidates, so we only move for a real improvement. Measured drift: **about 14 m** — the
fall from 32 m only lasts ~2.5 s and `air_accel` is 12, so this nudges the spawn rather than
relocating it.

**Climbing to 60 m — measured worse, rolled back to 32 m.** With `alt` 70 / `energy_floor` 12 it
reached **61.9 m at t=6.4 s** on 88 energy and stayed airborne 8.7 s (vs 5.4 s). It learned
**6 items by t=10**, no more than the 30 m climb's 7-8, exactly as the Exp 14 line-of-sight
table predicted (30 m sees 8 of the 10 items in range, 60 m sees 9, and only 10 of 76 are in
range at all). Benchmarks: 2.125, 1.250. So it costs 3 extra seconds exposed and nearly all our
energy to learn nothing. `alt` 32.0 / `energy_floor` 40.0.

**Result — 16 matches, seeds 1-16, three runs of the kept build: 2.438, 2.062, 2.125.**
exp 4 fresh baseline: 1.938. 60 m variant: 2.125, 1.250. Previous (32 m, no glide): 2.250, 2.125.
The glide is free and the spread tightened; the height was not.
