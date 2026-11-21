"""Run one offline match: external bots (mechfight/1 subprocesses) + in-process house bots.

    python -m sim.run_match --bot "python -u bot.py" --seed 7 --replay sim/out/m7.jsonl
"""
import argparse
import json
import math
import os
import queue
import random
import shlex
import subprocess
import sys
import threading
import time

from . import config as C
from .engine import Match

DIFFS = ["easy"] * 4 + ["medium"] * 4 + ["hard"] * 4 + ["expert"] * 3


# ------------------------------------------------------------------ placeholder squad (import fallback)
class PlaceholderSquad:
    """Minimal built-in house AI used only if sim.housebots cannot be imported."""

    def __init__(self, cfg, wm, member_slots, prey_slots, difficulty, seed):
        self.cfg, self.wm = cfg, wm
        self.members = list(member_slots)
        self.prey = set(prey_slots)
        self.rng = random.Random(seed)
        self.err = {"easy": 0.12, "medium": 0.07, "hard": 0.04, "expert": 0.02}
        self.diff = difficulty

    def act_all(self, obs_by_slot, t):
        out = {}
        for s, obs in obs_by_slot.items():
            me = obs["self"]
            px, py, pz = me["pos"]
            tgt = None
            bd = 1e18
            for m in obs["mechs"]:
                if m["id"] in self.prey and m["kind"] == "mech":
                    d = math.hypot(m["pos"][0] - px, m["pos"][1] - py)
                    if d < bd:
                        bd, tgt = d, m
            act = {"tick": obs["tick"]}
            if tgt is not None:
                tx, ty, tz = tgt["pos"]
                tm = bd / 252.0
                tx += tgt["vel"][0] * tm
                ty += tgt["vel"][1] * tm
                e = self.err.get(self.diff.get(s, "medium"), 0.07)
                yaw = math.atan2(ty - py, tx - px) + self.rng.gauss(0, e)
                pit = math.atan2(tz + 4.5 - (pz + 7), max(1.0, bd)) + self.rng.gauss(0, e)
                act["aim"] = [yaw, pit]
                act["fire"] = [bd < 200, False]
                if bd > 60:
                    act["move"] = [math.cos(yaw), math.sin(yaw)]
                else:
                    act["move"] = [-math.sin(yaw), math.cos(yaw)]
            else:
                z = obs["zone"]
                cx, cy = z["next_center"] if not me["in_zone"] else z["center"]
                dx, dy = cx - px, cy - py
                d = math.hypot(dx, dy)
                if d > 30:
                    act["move"] = [dx / d, dy / d]
                    act["aim"] = [math.atan2(dy, dx), 0.0]
            out[s] = act
        return out


def make_squad(cfg, wm, members, prey, diffs, seed):
    try:
        from sim.housebots import Squad
        return Squad(cfg, wm, members, prey, diffs, seed), "housebots"
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[run_match] housebots import failed ({e!r}); using placeholder squad\n")
        return PlaceholderSquad(cfg, wm, members, prey, diffs, seed), "placeholder"


# ------------------------------------------------------------------ external bot
class ExtBot:
    def __init__(self, cmd, slot, logpath, hello):
        self.slot = slot
        self.name = f"bot{slot}"
        self.logf = open(logpath, "wb")
        argv = shlex.split(cmd, posix=(os.name != "nt"))
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.logf,
                                     cwd=C.ROOT, bufsize=0)
        self.q = queue.Queue()
        self.dead = False
        self.late = 0
        self.stale = 0
        self.valid = 0
        self.lat = []
        self.last = {}
        self.missed_s = 0.0
        self.ended = False
        threading.Thread(target=self._reader, daemon=True).start()
        self.send(hello)

    def _reader(self):
        try:
            for line in iter(self.proc.stdout.readline, b""):
                if len(line) > 65536:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if isinstance(msg, dict):
                    self.q.put((time.perf_counter(), msg))
        except Exception:
            pass
        self.q.put(None)

    def send(self, obj):
        if self.dead:
            return
        try:
            self.proc.stdin.write((json.dumps(obj, separators=(",", ":")) + "\n").encode())
            self.proc.stdin.flush()
        except Exception:
            self.dead = True

    def wait_ready(self, budget_s):
        end = time.perf_counter() + budget_s
        while True:
            rem = end - time.perf_counter()
            if rem <= 0:
                self.dead = True
                return False
            try:
                item = self.q.get(timeout=rem)
            except queue.Empty:
                self.dead = True
                return False
            if item is None:
                self.dead = True
                return False
            if item[1].get("type") == "ready":
                self.name = str(item[1].get("name", self.name))[:32]
                return True

    def collect(self, tick, t_send, budget_s):
        deadline = t_send + budget_s
        while True:
            rem = deadline - time.perf_counter()
            try:
                item = self.q.get(timeout=max(0.0, rem))
            except queue.Empty:
                self.late += 1
                return None
            if item is None:
                self.dead = True
                return None
            ts, msg = item
            if msg.get("type") == "ready" or msg.get("tick") != tick:
                self.stale += 1
                continue
            if ts > deadline:
                self.late += 1
                return None
            self.valid += 1
            self.lat.append(ts - t_send)
            return msg

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=2.0)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.logf.close()


# ------------------------------------------------------------------ the match
def run_match(bots, seed, replay=None, stop_on_death=True, budget_ms=None, out_dir=None, validate=0,
              verbose=True, wm=None):
    cfg = C.load_config()
    wm = wm or C.load_worldmap(cfg["mech"]["step_up"])
    out_dir = out_dir or os.path.join(C.HERE, "out")
    os.makedirs(out_dir, exist_ok=True)
    rng = random.Random(seed * 7919 + 13)
    n = cfg["match"]["mechs"]
    slots = list(range(n))
    rng.shuffle(slots)
    ext_slots = sorted(slots[:len(bots)])
    house = sorted(slots[len(bots):])
    teams = {s: s for s in ext_slots}
    for s in house:
        teams[s] = 100
    diffs = {}
    order = DIFFS[:]
    rng.shuffle(order)
    for i, s in enumerate(house):
        diffs[s] = order[i % len(order)]
    t_wall0 = time.perf_counter()
    match = Match(cfg, wm, seed, teams)
    match.record = bool(replay)
    squad, squad_kind = make_squad(cfg, wm, house, ext_slots, diffs, seed)
    budget = (budget_ms if budget_ms is not None else cfg["bot"]["tick_budget_ms"]) / 1000.0
    de = cfg["match"]["decide_every"]
    dec_dt = de * match.dt

    ext = {}
    for s, cmd in zip(ext_slots, bots):
        ext[s] = ExtBot(cmd, s, os.path.join(out_dir, f"bot_seed{seed}_slot{s}.log"), match.hello(s))
    for s, e in ext.items():
        e.wait_ready(cfg["bot"]["hello_budget_ms"] / 1000.0)

    rf = None
    if replay:
        os.makedirs(os.path.dirname(os.path.abspath(replay)), exist_ok=True)
        rf = open(replay, "w", encoding="utf-8")
        info = []
        for s in range(n):
            info.append({"slot": s, "name": ext[s].name if s in ext else f"house-{diffs[s]}", "team": teams[s],
                         "difficulty": diffs.get(s)})
        rf.write(json.dumps(match.replay_meta(info), separators=(",", ":")) + "\n")

    sys.setswitchinterval(0.001)
    squad_errors = 0
    schema_errors = []
    n_valid_checked = 0
    ext_done = set()
    stopped_early = False

    def finish_ext(s):
        e = ext[s]
        if s in ext_done:
            return
        ext_done.add(s)
        m = match.mechs[s]
        place = m.place if m.place is not None else match.placements().get(s, 1)
        e.send({"type": "end", "place": place, "kills": m.kills})

    while not match.done():
        if match.tick % de == 0:
            t = match.time
            for s in ext:
                if not match.mechs[s].alive:
                    finish_ext(s)
            if stop_on_death and ext and all(not match.mechs[s].alive for s in ext):
                stopped_early = True
                break
            match.prune()
            sent = {}
            for s, e in ext.items():
                if match.mechs[s].alive:
                    o = match.observation(s)
                    if validate and (n_valid_checked % validate == 0) and len(schema_errors) < 20:
                        schema_errors += C.schema_validate(o)[:3]
                    n_valid_checked += 1
                    sent[s] = (o["tick"], time.perf_counter())
                    e.send(o)
            hobs = {s: match.observation(s) for s in house if match.mechs[s].alive}
            if validate and hobs and (n_valid_checked % validate == 0) and len(schema_errors) < 20:
                schema_errors += C.schema_validate(next(iter(hobs.values())))[:3]
            hacts = squad.act_all(hobs, t) if hobs else {}
            for s, a in hacts.items():
                if len(a) <= 1:
                    squad_errors += 1
                match.apply_action(s, a)
            for s, (tick, ts) in sent.items():
                e = ext[s]
                act = None
                if not e.dead:
                    act = e.collect(tick, ts, budget)
                if act is not None:
                    e.last = act
                    e.missed_s = 0.0
                    lab = (act.get("debug") or {}).get("label") if isinstance(act.get("debug"), dict) else None
                    if lab:
                        match.labels[s] = str(lab)[:40]
                    match.apply_action(s, act)
                else:
                    e.missed_s += dec_dt
                    if e.dead or e.missed_s > cfg["bot"]["coast_s"]:
                        match.apply_action(s, {"tick": tick, "move": [0, 0]})
                    else:
                        la = e.last
                        co = {"tick": tick, "move": la.get("move", [0, 0]), "boost": bool(la.get("boost", False))}
                        if "aim" in la:
                            co["aim"] = la["aim"]
                        match.apply_action(s, co)
            if rf is not None:
                rf.write(json.dumps(match.replay_frame(), separators=(",", ":")) + "\n")
        match.step()

    # finish
    for s in ext:
        if stopped_early or match.done():
            pl = match.placements()
            m = match.mechs[s]
            if s not in ext_done:
                ext_done.add(s)
                ext[s].send({"type": "end", "place": pl.get(s, 1), "kills": m.kills})
    places = match.placements()
    kills = match.kills()
    wall = time.perf_counter() - t_wall0
    res = {"type": "result", "places": {str(k): v for k, v in places.items()},
           "kills": {str(k): v for k, v in kills.items()}, "duration": round(match.time, 2)}
    if rf is not None:
        rf.write(json.dumps(res, separators=(",", ":")) + "\n")
        rf.close()
    summary = {"seed": seed, "duration": res["duration"], "wall_s": round(wall, 1), "squad": squad_kind,
               "truncated": stopped_early, "squad_errors": squad_errors, "schema_errors": schema_errors[:5],
               "ext": []}
    for s, e in ext.items():
        pl = places.get(s)
        lat = sorted(e.lat)
        summary["ext"].append({"slot": s, "name": e.name, "place": pl, "kills": kills.get(s, 0),
                               "score": (16 - pl) if pl is not None else None, "late": e.late, "stale": e.stale,
                               "answered": e.valid, "crashed": e.dead,
                               "lat_p50_ms": round(1000 * lat[len(lat) // 2], 1) if lat else None,
                               "lat_max_ms": round(1000 * lat[-1], 1) if lat else None})
    for e in ext.values():
        e.close()
    res["summary"] = summary
    return res


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", action="append", required=True, help="external bot command (repeatable)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--replay", default=None)
    ap.add_argument("--full", action="store_true", help="keep simulating after the external bot(s) die")
    ap.add_argument("--budget-ms", type=float, default=None)
    ap.add_argument("--validate", type=int, default=0, help="schema-check every Nth observation")
    ap.add_argument("--out-dir", default=None)
    a = ap.parse_args(argv)
    res = run_match(a.bot, a.seed, a.replay, stop_on_death=not a.full, budget_ms=a.budget_ms,
                    out_dir=a.out_dir, validate=a.validate)
    print(json.dumps(res["summary"]))
    for e in res["summary"]["ext"]:
        print(f"seed={a.seed} slot={e['slot']} place={e['place']} kills={e['kills']} score={e['score']} "
              f"duration={res['duration']}s wall={res['summary']['wall_s']}s late={e['late']}")


if __name__ == "__main__":
    main()
