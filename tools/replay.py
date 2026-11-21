"""Offline replay harness: feed a recorded match stream to the bot in-process.

The recording doesn't react to our actions, so this checks decisions and speed, not skill.
  python tools/replay.py                    # summary: timing + state timeline
  python tools/replay.py --every 5          # print every 5th decision
  python tools/replay.py --stream other.jsonl.gz
"""
import argparse
import gzip
import io
import json
import os
import sys
import time
from contextlib import redirect_stderr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bot as botmod  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stream", default=os.path.join(ROOT, "check", "stream.jsonl.gz"))
    ap.add_argument("--every", type=int, default=0, help="print every Nth decision")
    ap.add_argument("--quiet-log", action="store_true", help="hide the bot's stderr log")
    a = ap.parse_args()
    op = gzip.open if a.stream.endswith(".gz") else open
    with op(a.stream, "rt") as f:
        msgs = [json.loads(l) for l in f if l.strip()]
    hello = msgs[0]
    hello["map"]["dir"] = os.path.join(ROOT, "arena")
    err = io.StringIO()
    t0 = time.perf_counter()
    with redirect_stderr(err if a.quiet_log else sys.stderr):
        b = botmod.Bot(hello)
    print(f"init {1000 * (time.perf_counter() - t0):.0f} ms")
    times = []
    n = 0
    for m in msgs[1:]:
        if m.get("type") == "end":
            with redirect_stderr(err if a.quiet_log else sys.stderr):
                b.on_end(m)
            break
        if m.get("type") != "obs":
            continue
        s = time.perf_counter()
        with redirect_stderr(err if a.quiet_log else sys.stderr):
            botmod.mf.draw.clear()
            act = b.act(m)
        times.append(1000 * (time.perf_counter() - s))
        n += 1
        if a.every and n % a.every == 0:
            me = m["self"]
            print(f"t={m['t']:5.1f} hp={me['hp']:5.0f} pos=({me['pos'][0]:.0f},{me['pos'][1]:.0f},{me['pos'][2]:.0f}) "
                  f"{b.move_state}/{b.aim_state}/{b.shoot_state}/{b.loot_state} move={act.get('move')} "
                  f"boost={act.get('boost')} aim={act.get('aim')} fire={act.get('fire')} "
                  f"{'take=' + str(act['take']) if 'take' in act else ''} {'pick=' + str(act['pick']) if 'pick' in act else ''}")
    times.sort()
    if times:
        print(f"decisions={len(times)} mean={sum(times) / len(times):.2f}ms p50={times[len(times) // 2]:.2f} "
              f"p99={times[int(len(times) * 0.99)]:.2f} max={times[-1]:.2f}ms")


if __name__ == "__main__":
    main()
