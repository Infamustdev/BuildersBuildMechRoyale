# Simulator assumptions (where protocol.md is silent)

- **Place on same-tick deaths**: every mech dying on a tick gets place = number alive at the start of that tick's death processing (so two dying with 10 alive both get 10). Timeout: survivors ranked by HP (1..k). Early stop (external bot dead, default) ranks survivors provisionally by HP.
- **Kill credit**: the attacker whose hit took HP to <= 0; zone and NPC kills give no credit. Self-splash kills give none.
- **Gun spread**: Gaussian, sigma = `spread_deg` on yaw and pitch independently. NPC spread likewise.
- **Gun reload**: starts automatically when the magazine empties (or on `reload`); cooldown carries over so a held trigger fires at exactly 1/cooldown on average.
- **Laser**: released early fires at `min_charge`; charge reported in obs is capped at `charge`; hold timer counts only after full charge with trigger held; shorted laser cannot start charging for `short_s`.
- **Lightning**: cone is +/- `cone_deg` of aim yaw; range is 3D distance; LOS uses static geometry (trees ignored); chain picks nearest unhit target within `chain_range` with LOS; teammates skipped; energy < cost = no shot.
- **Mortar splash**: distance measured from blast point to nearest point of the target cylinder; direct hit gets `damage` plus full-strength splash; blocked splash = no LOS from the blast point (last free sample) to center of mass. Concussion applies to teammates never; to self yes. Mortar splash and laser/lightning also hit NPCs and crates.
- **Rear test**: angle between victim torso yaw and bearing to source > front_arc/2. Order: cube damage mult, rear mult, armor; self-damage x0.5 for own splash.
- **Movement**: axis-separated moves; walls = building cells (top-ground > 0.01) higher than feet+step_up, tested at 5 ring points ahead; terrain-only cells are never walls, slope > max_slope blocks by rise/run of the center surface. Bridges ignored (as in worldmap.py). Jet = vz += (jet_accel - gravity)*dt capped at max_rise_speed. Boost on ground drains `boost_drain` only while moving. No ceilings/eaves. Roof-edge rule approximated: landing on an overhanging edge, then sliding off 0.6 m/tick unless moving toward the roof.
- **Mech overlap**: pairwise horizontal push-apart (half each) if vertical extents overlap.
- **Vision**: near range is 3D sphere, buildings only; far range uses 3D distance <= range plus horizontal 120 deg wedge; LOS from eye to center, else top. Trees: trunk = circle radius trunkR+0.3 up to z+crownBase, crown = vertical cylinder crownR from z+crownBase to z+height; foliage path length accumulated at 1.5 m samples; blocked when `density*length > -ln(canopyBlockT)`. Crates/cubes/pickups/projectiles use their center point. Own-team projectiles from other mechs are visible; own are omitted. Beams visible if end points or midpoint are in view.
- **Memory**: tracked targets are listed with real current position and hp for 5 s after last seen; dead ones vanish.
- **Sounds**: 3D distance vs carry range, no occlusion; bearing noise sigma x `hearing` stat; `jet` sound every 0.6 s while jetting; `crate` sound on destruction; `reload` on reload start; `drop` on landing; `death` at death. Zone damage events are merged per decision.
- **Zone**: next centers are pre-drawn uniformly (dist = 0.85 * (r - r_next) * sqrt(U)) so each circle lies inside the previous. After the last phase the zone stays at radius 0. dps of phase k applies from phase start through its shrink.
- **Spawns/loot**: random open points (nav grid cells with all 8 neighbours free, not building, 30 m from map edge). Roof items on interior roof cells of buildings 3..65 m tall. Floor/crate weapon kinds equal weights over mortar/laser/lightning. Dead-mech drops scatter within 2-3 m.
- **NPCs**: wander to random points within leash, aggro scan every 0.3 s (LOS static), approach to 18 m, shoot at <120 m with LOS, no lead. NPC bullets (range 150 m) hit mechs only. NPC walking only checks the 2 m solid grid, no slopes.
- **Supply drop**: position = random open point within `spread` x radius of the circle the zone is closing to at call time; capture checks horizontal distance only; capture grants cubes, full HP and energy.
- **Offers**: queued per mech; next draws 3 random eligible upgrades when the previous resolves; thresholds beyond the configured list continue with the last step. `pick` out of range = choice 0. Gun mag upgrades add the extra rounds immediately.
- **Taking**: closest mech wins a contested pickup (ties lower id), others get `take_failed: gone`. Dropped weapons keep their state (ammo/reload/cooldown); equip lockout = max(equip_s, cooldown).
- **Timing**: external bot answers arriving after `tick_budget_ms` (wall clock, from obs send) count as missed (coast, then idle after `coast_s`). `--budget-ms` lets heavy parallel batches relax this. Bot stderr goes straight to a log file (no pipe, so it cannot block).
- **Early stop**: matches stop when all external bots are dead (practice score only needs their place); use `--full` to play to the end.
- **House bots**: only living members are passed to `Squad.act_all`; actions consisting of only `tick` are counted as `squad_errors` in the result.

## Calibration overrides (orchestrator)
- NPC drones deal no damage to the house squad (team 100): the real hide probe scored 0.04, so house bots never die without our kills.
- Zone damage cannot kill a squad member (HP floors at 1): sim house bots leave the late zone; real ones evidently do not die to it.
- `HB_AIM_SCALE` (housebots.py) multiplies all house-bot aim error; tuned so the exp-4 bot matches its real 2.88.
