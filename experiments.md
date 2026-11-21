# Tuning experiments

Practice = 48 matches on fixed private seeds vs 15 house bots that never fight each other,
so score ≈ number of house bots dead when we die. One change per submission; keep a change
only if it beats the best score by more than the noise.

Submit: track page → paste `https://github.com/Infamustdev/BuildersBuildMechRoyale` (scores the
latest commit on `main`). Limit: 4 per hour, one queued at a time.

| # | Commit | Change vs baseline | Score | Notes |
|---|--------|--------------------|-------|-------|
| 0 | f79563a | Baseline (flee_adv 0.6, low-HP flee below 1.3, min_hit_prob 0.65) | 2.58 | Starter = 1.15 |
| 2 | aggressive | flee_adv 0.6→0.4, low_hp_flee_adv 1.3→0.9 | | Flee less, stay in fights |
| 1 | (re-push of baseline) | Same code as #0, measures noise | | Run after #2 by reverting tuning.py |
| 3 | | min_hit_prob 0.65→0.45 | | More shots at range |
| 4 | | Retreat only below 25% HP | | |
| 5 | | w_roof 1.2→0 (no roofs) | | |
| 6 | | finish_hp_frac 0.25→0.4, finish_max_dist 140→200 | | |

**Best so far:** #0, 2.58 (f79563a)

**Noise:** unknown (fill in from #1: |score #1 − score #0|)
