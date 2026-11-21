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
