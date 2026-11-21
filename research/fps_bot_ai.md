# Classic FPS bot AI: what to steal for Mech Royale

Scope: pre-ML shooter AI, mapped to `mechfight/1` (10 decisions/s, 50 ms, 1 CPU, 120 deg cone + 45 m proximity, sound bearing +-7 deg, torso 4 rad/s, rear armor x1.5, regen 45 HP/s after 5 s, weapon triangle). Our situation: 15 cooperating house bots hunt us weakest-first; points = our kills; we average about 3.

Source confidence: pages I searched (titles/abstracts confirmed): Quake III thesis, Killzone paper, F.E.A.R. GDC paper, L4D slides, Isla's occupancy-map papers. Details of Counter-Strike bot, Halo 2 and Dave Mark's utility work are from my background knowledge, not re-fetched; check the linked originals before quoting.

## Ranked summary (expected impact on kills per match)

| # | Technique | Why it ranks here | Main source |
|---|---|---|---|
| 1 | Fight-or-flight scoring + regen cycles | Our deaths cap our kills. 1400 HP at 45 HP/s regen = a full heal in about 31 s if we break contact for 5 s. Disciplined retreat turns one life into many fights. | Quake III goal fuzzy logic; Booth/Valve bots; Dave Mark utility AI |
| 2 | Tactical position scoring (corners/chokepoints, exposure count) | Counters 15-on-1 focus fire: pick spots where at most 1-2 enemies have line of sight. | Straatman and van der Sterren, Killzone |
| 3 | Target selection / kill securing | Score is kills, not damage. Finish low-HP targets, stay on one target, avoid spreading damage. | Quake III, CS bots, Halo target selection |
| 4 | Aim model: iterative lead, torso pre-aim, always face threats | Gun at 252 m/s with spread is the main damage source; rear hits cost x1.5. | Quake III aim skill model; ballistics basics |
| 5 | Dodging and strafing (visible projectiles, charging lasers, mortar shells) | Everything but gun bullets is telegraphed; cheap HP saved every second. | Quake III / UT bot strafe logic |
| 6 | Weapon selection by range and the weapon triangle (+ loot priority) | Free multiplier: counter what enemies hold (`mechs[].weapons`, sound kind). | Quake III weapon weights |
| 7 | Occupancy map for search, ambush, and sound reaction | We need to find isolated or weak enemies and not walk into the pack. | Isla, Third Eye Crime; Darken particle filters |
| 8 | Anytime utility architecture with hysteresis (FSM execution, utility selection) | Keeps behaviour stable inside 50 ms in Python. Enabler, not a direct kill source. | Dave Mark; Orkin (3-state FSM); Isla behaviour DAG |

Squad tactics to counter (focus fire, flanking, weakest-first hunting) are covered in section C.

---

## A. Architectures (pick the cheap ones)

**FSM / hierarchical FSM.** Classic Quake and UT bots ran top-level goal states (get item, fight, retreat) with sub-states for movement. Fast, debuggable, but transitions explode and behaviour flickers. Keep as the execution layer, not the decision layer. (Our current design.)

**Behaviour trees / behaviour DAGs (Halo 2/3, Damian Isla).** Prioritised, re-evaluated-each-tick tree of behaviours with "impulses" and stimulus behaviours; reuse via a DAG instead of a tree. Useful idea: re-evaluate from the top every decision and let a higher-priority behaviour (e.g. "I am being focused, break LOS") pre-empt. Source: Isla, "Handling Complexity in the Halo 2 AI" (GDC 2005, Gamasutra/Game Developer write-up; search that title).

**Utility AI (Dave Mark, *Behavioral Mathematics for Game AI*; GDC "Improving AI Decision Modeling Through Utility Theory").** Every option scores 0..1 from response curves over inputs; pick the highest, optionally with weighted-random among near-ties. Apply: our "judgement score" is already one input. Make each option (engage target X, retreat to cover Y, loot Z, search, move to zone) a score computed from shared inputs (own HP fraction, time-to-kill ratio, enemies in LOS, zone distance). Add hysteresis: current action gets +10-20% bonus and a minimum commit time (about 0.5-1 s) to stop dithering.

**GOAP (F.E.A.R., Orkin).** A 3-state FSM (Goto, Animate, UseSmartObject) plus A* over action preconditions/effects. Source: [Orkin, "Three States and a Plan", GDC 2006](https://pages.cs.wisc.edu/~dyer/cs540/handouts/gdc2006_orkin_jeff_fear.pdf). Our action space is small (shoot, reload, take cover, loot, heal-hide), so a planner is overkill; keep the *idea* of goals with costs and of re-planning when the world changes.

**HTN (Killzone 2, Horizon).** Hierarchical task decomposition for squad behaviours ([Guerrilla: Beyond Killzone](https://fr.slideshare.net/guerrillagames/beyond-killzone-creating-new-ai-systems-for-horizon-zero-dawn-slide-78); [Verweij bot system, VUA07](https://www.guerrilla-games.com/media/News/Files/VUA07_Verweij_Hierarchically-Layered-MP-Bot_System.pdf)). Overkill for one bot; the house bots may use something like it. Skip.

**Quake III bot (van Waveren).** [Thesis](https://www.cs.rochester.edu/u/brown/242/docs/QuakeIII.pdf), [fuzzy-logic manual](https://icculus.org/gtkradiant/documentation/bot_manual/fuzzy.html), [Sanglard code review](https://fabiensanglard.net/quake3/a.i.php). Pieces: AAS (area awareness system) for navigation, long-term goal selection where each item's desire is a fuzzy function of own health/armor/weapons and distance (travel time), a per-bot "characteristic" file (aim skill, reaction time, weapon weights, aggression), and combat sub-FSM (attack, strafe, retreat when health low). Apply: copy the weighting style (desire = f(my state) / travel time) for loot and cover choices, and keep tunables in a single dict so we can parameter-sweep in our own simulator.

**Counter-Strike bots (Mike Booth; PODBot).** Bots used a nav mesh, "hiding spots" precomputed from geometry, a morale/aggression value, and retreat to hide when hurt or outnumbered; later Booth's Left 4 Dead bots and [Director](https://cdn.fastly.steamstatic.com/apps/valve/2009/ai_systems_of_l4d_mike_booth.pdf) used pacing (build-up, peak, relax). Apply: precompute hiding spots at `hello` (20 s budget) from `world.json` prisms; use the Director idea for ourselves: alternate engage and relax phases (regen) on purpose.

---

## B. Techniques with application notes

### 1. Fight-or-flight scoring and regen cycles
**How it works.** Compare expected time-to-kill (their HP / our DPS at current range and hit probability) with expected time-to-die (our HP / summed DPS of all enemies that can see or reach us). Quake III bots switched to retreat when health fell below a skill-dependent threshold; Booth's CS bots hid when outnumbered. Sources as in section A.
**Apply.** `ratio = TTD_me / TTK_target`; engage if > about 1.3, retreat if < 0.8 (the gap is hysteresis). TTD must count *unseen but known* enemies (5 s memory plus sound events) and incoming `damaged` events with `rear`. Retreat means: break LOS with every known enemy, then stay out of damage and out of vision for 5 s; regen then heals 45 HP/s (about 900 HP in 20 s). Never sit in the zone edge while regenerating (regen is off outside the zone). Retreat toward the zone center, away from the hunting group, using step/boost sound discipline: moving under 5 m/s is silent, so *creep* while hiding (no `step` sounds, so no bearing leak).

### 2. Tactical position scoring (Killzone) and cover points
**How it works.** Straatman and van der Sterren score candidate positions by a weighted sum of static and dynamic terms (cover quality, distance to enemy, line of fire, exposure to other enemies, distance from current position), pick the max, and re-evaluate; see [Killzone's AI: dynamic procedural combat tactics](https://www.guerrilla-games.com/read/killzones-ai-dynamic-procedural-tactics) and the [GDCE05 slides](https://www.guerrilla-games.com/media/News/Files/gdce05_killzone_ai.pdf).
**Apply.** At startup rasterise prisms to a 2-4 m grid and precompute, per cell: (a) wall-hugging "corner" cells (a mech is 5 m wide, so use real clearance), (b) a coarse visibility count (how many ring directions have more than 60 m of open sightline), (c) height. At runtime score about 30 nearby cells: `+ cover from all known enemies' bearings`, `+ exposure to at most one lane`, `- distance`, `- zone risk`, `+ having a retreat path behind`, `+ range band that suits our weapon`. Key goal against 15 hunters: choose spots where one enemy at a time can arrive (doorway, corridor, building corner), so focus fire degrades into a 1v1 queue. High ground: roof positions give sight over cover and eye height, but jetting costs energy (about 60 m climb from 100 energy) and the 45 m proximity ring is all-around, so check that you can drop off.

### 3. Target selection and kill securing
**How it works.** Bots rank visible enemies by threat/opportunity (distance, health, weapon, whether it is aiming at us). Halo-style target tracking additionally "locks" a target to avoid flicker.
**Apply.** Score = `kill_value / TTK`, with a large bonus for enemies already damaged (HP is shown), a bonus for being the one who landed damage (we only get the kill), and a switching cost of about 20% to stay on target. Do not spread damage; house bots do not fight each other, so damaged ones are never finished by others, which means a wounded house bot that retreats to regen is a kill we should chase *only if* its pack is not near. Kills drop all its cubes plus two (+4% damage/HP each, but +5% size each, so watch the hitbox growth) and its weapons (grab laser/lightning/mortar to cover the triangle).

### 4. Aiming model
**How it works.** Quake III bots had an aim "skill" and "accuracy" with error added and reaction time; good ones lead targets by solving intercept. Human-like limits (turn rate, reaction delay) are modelled explicitly.
**Apply.** Iterate intercept: t = dist/252, predicted = pos + vel*t, recompute 2-3 times (use the target's *recent* acceleration only if it is strafing in a regular pattern; otherwise constant velocity). Use their `aim` and `charging` flags to predict dodge. Torso rate is 4 rad/s (about 0.4 rad per decision), so pre-rotate toward the predicted engagement bearing during approach and never spend decisions walking while facing away; since torso and legs are independent, strafe sideways *while* torso tracks. Fire discipline: the gun does 45 dmg at 0.24 s with spread 0.6 deg; at 150 m that is about 1.6 m of scatter, so cut fire above about 150-180 m unless target is stationary or large (mechs are 5 m wide). Reload during hide phases: reload only when not in LOS or in cooldown windows (2.4 s dead time is a killer mid-fight; call `reload` early whenever safe).

### 5. Strafing and dodging
**How it works.** Quake/UT bots strafe perpendicular to the enemy with random direction changes, jump at rockets, and crouch-peek.
**Apply.** (a) Bullets: strafe perpendicular with random reversal times of 0.3-1.0 s; avoid regular periods; `ground_accel` 58.5 m/s^2 means about 0.2 s to reverse, so reversals work. (b) Visible projectiles: compute closest approach for each `projectiles` entry (vel known), sidestep if miss distance < 4 m. (c) `charging: true` on an enemy in view: its beam is hitscan along its `aim`; leave that line (perpendicular move, or LOS break) just before 0.75 s elapses, or take cover. Laser sound `laser_charge` also warns at 140 m. (d) Mortar shell: ballistic, 7 m splash and knockback; move out of the predicted landing point (shells are visible with pos/vel). (e) Do not outrun lightning: its range is 45 m, so stay beyond it if you hold laser, close in if you hold lightning vs laser.

### 6. Weapon selection (range bands + triangle)
**How it works.** Quake III used per-weapon fuzzy weights depending on ammo, range and situation. Same here.
**Apply.** Range bands: laser > 100 m (charge fully only if target cannot sidestep: when it is busy shooting/reloading, or in LOS-break approach); gun 40-180 m; lightning < 45 m (chain through groups, but costs 3 energy per shot, so check energy); mortar for targets behind cover and for flushing, but with 7 m splash, 50% self-damage and a self-concuss, never fire it inside about 15 m. Triangle: use `mechs[].weapons` or heard sound kind to counter (laser vs mortar carriers, mortar vs lightning, lightning vs laser; also, holding a mortar makes us cook-off-vulnerable to lasers, so drop it when the enemy shows a laser). Loot: weapon pickup distance weighted by travel time and by our current slot-1 value; kill drops give guaranteed weapons.

### 7. Occupancy maps, sound heat maps, and target search
**How it works.** Isla's occupancy maps keep a probability per map cell that a hidden target is there, spread/diffuse it over time through walkable neighbours, and zero it out over cells you can currently see ([Isla and Blumberg, Object Persistence for Synthetic Creatures](https://characters.media.mit.edu/Papers/objectPersistence.pdf); [Third Eye Crime](https://ojs.aaai.org/index.php/AIIDE/article/view/12663)). Particle filters are an alternative ([Darken and Anderegg, Game AI Programming Wisdom 4](https://faculty.nps.edu/cjdarken/pubs/simulacra-abstract.html)).
**Apply.** We already have a sound heat map; extend it. Each sound adds a cone with sigma 7 deg out to distance bins (near less than 60, mid less than 160, far) and the sound type gives weapon kind. Diffuse and decay; zero cells inside our current 120 deg cone or 45 m ring with LOS. Because house bots hunt us, flip the usage: treat heat as *threat map* (where the pack probably is) for retreat routing and as *prey map* for isolated targets (single shots far from other sounds). Intersect two sounds from different times/positions to triangulate. Look direction: move the torso (free, 4 rad/s) to scan the hottest sector; a 120 deg cone leaves a 240 deg blind arc beyond 45 m.

### 8. Anytime computation under 50 ms (Python, 1 CPU)
**How it works.** Do the cheapest decision first, refine while budget remains, and always have a valid answer. Valid in Python because the lockstep server waits for the slow.
**Apply.** (1) Precompute everything static at `hello` (20 s): grid, visibility, cover. (2) Per tick: reflex layer (dodge, face threat, fire) first, in about 5 ms; (3) then utility scoring over a capped candidate set, time-checked with `time.perf_counter()`; stop at about 35 ms; (4) cache last strategic decision and only re-score it every 3-5 ticks unless an event (`damaged`, `kill`, new enemy) invalidates it. Use numpy-free pure Python unless vendored; keep JSON parse lean. Late answers coast and then idle, so stay under budget with margin (conformance check fails above 2% late).

---

## C. Squad tactics to counter (house bots)

- **Focus fire:** the main killer. Counter: fight at chokepoints (technique 2), retreat as soon as 2+ shooters have LOS, and lose pursuers using doors and buildings (their sight is also LOS-limited; memory is 5 s).
- **Flanking / rear shots (x1.5 damage):** `damaged.rear` and sounds behind you must cause a quick torso turn toward the bearing, and wall-backed positions help (cover your back with geometry instead of open ground).
- **Weakest-first hunting:** we will be chased when hurt, so do not regen in the open; regen where pursuit paths are long and one-at-a-time. If they all converge on us, kiting through a loop around a building to string them out into a line can create 1v1s.
- **Zone and supply drops:** drops need 4 s alone in an 8 m ring; with 15 bots that is usually contested and a trap. Use them only when we are first by a wide margin or as an ambush site (enemies stand still in a known place, ideal for mortar/laser).
- **Ambush / kite:** pick a site along the likely path (zone-shrink direction), stand in cover with the weapon pre-aimed, fire first with laser/mortar, then back off to a second prepared position (peek-shoot-hide, already implemented; add fallback positions and a leapfrog route).
- **Last-circle behaviour:** zone damage reaches 100-260 HP/s; stay in the safe zone early to avoid giving free kills to attrition, and keep distance from pack edges.

## D. Open-source arena / FPS bot frameworks (just names)

- [Omni-bot](https://github.com/jswigart/omni-bot): bot framework for several FPS engines (scripted goals, waypoints).
- [ioquake3](https://github.com/ioquake/ioq3): the Quake III source with the van Waveren bot code (`code/botlib`, `code/game/ai_*.c`) to read.
- [Wilderness Scavenger / inspirai_fps](https://pypi.org/project/inspirai-fps): Unity open-world FPS AI platform.
- [Quagents](https://www.cs.rochester.edu/trac/quagents/wiki): Quake-based agent testbed.
- [DECOY](https://github.com/HATS-ICT/decoy): multi-agent CS:GO-style movement simulator.
- [AgentArena](https://github.com/JustInternetAI/AgentArena): Godot 4 agent arena.

## E. Build order for the next iteration

1. Fight-or-flight ratio with explicit TTD from all known threats, plus a hide-and-regen routine that breaks LOS for 5 s.
2. Precomputed cover/choke grid; position score with exposure count.
3. Target switching cost and kill-securing bonus.
4. Iterative lead + torso-first facing + early reload; reflex dodge layer for lasers and shells.
5. Weapon counters and loot priority.
6. Occupancy map upgrade.
7. A headless simulator to sweep the fight/flight thresholds, since all of the above is tunable numbers (Quake III "characteristics" style).
