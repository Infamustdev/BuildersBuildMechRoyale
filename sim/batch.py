"""Run N matches over seeds (multiprocessing) and print the practice score.

    python -m sim.batch --bot "python -u bot.py" --matches 16 --seeds 1-16 --workers 4
"""
import argparse
import json
import multiprocessing as mp
import os
import time


def parse_seeds(spec, n):
    if not spec:
        return list(range(1, n + 1))
    out = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            out += list(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def _one(args):
    bots, seed, full, budget_ms, out_dir, replay_dir = args
    from sim.run_match import run_match
    rp = os.path.join(replay_dir, f"m{seed}.jsonl") if replay_dir else None
    try:
        res = run_match(bots, seed, rp, stop_on_death=not full, budget_ms=budget_ms, out_dir=out_dir)
        return res["summary"]
    except Exception as e:  # noqa: BLE001
        return {"seed": seed, "error": repr(e), "ext": []}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", action="append", required=True)
    ap.add_argument("--matches", type=int, default=4)
    ap.add_argument("--seeds", default=None, help="e.g. 1-16 or 1,3,5")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--budget-ms", type=float, default=None)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--replay-dir", default=None)
    a = ap.parse_args(argv)
    seeds = parse_seeds(a.seeds, a.matches)[:a.matches] if a.seeds is None else parse_seeds(a.seeds, a.matches)
    jobs = [(a.bot, s, a.full, a.budget_ms, a.out_dir, a.replay_dir) for s in seeds]
    t0 = time.time()
    scores, places, kills = [], [], []
    with mp.Pool(max(1, a.workers)) as pool:
        for r in pool.imap_unordered(_one, jobs):
            if r.get("error"):
                print(f"seed={r['seed']} ERROR {r['error']}", flush=True)
                continue
            for e in r["ext"]:
                if e["place"] is None:
                    continue
                scores.append(e["score"])
                places.append(e["place"])
                kills.append(e["kills"])
                print(f"seed={r['seed']} slot={e['slot']} place={e['place']} kills={e['kills']} score={e['score']} "
                      f"dur={r['duration']}s wall={r['wall_s']}s late={e['late']} squad_err={r['squad_errors']}",
                      flush=True)
    if scores:
        n = len(scores)
        print(json.dumps({"matches": n, "mean_place": round(sum(places) / n, 2), "mean_kills": round(sum(kills) / n, 2),
                          "practice_score": round(sum(scores) / n, 3), "wall_s": round(time.time() - t0, 1)}))
        print(f"practice score = mean(16 - place) = {sum(scores) / n:.3f} over {n} matches")


if __name__ == "__main__":
    main()
