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
