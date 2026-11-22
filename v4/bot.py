"""Mech Royale bot, v4.

Each tick: update memory, the heatmap layers and the judge (engaged mechs, target lock, firing
solution), then run four independent controllers on
that shared picture. None waits on another's decision; each owns its part of the action.

    legs     -> move, boost, jump      (where to stand, heat-weighted route)
    torso    -> aim                    (where to look)
    triggers -> fire, reload           (what to shoot)
    economy  -> loot goal              (read by the legs on the next tick)
"""
import gc
import json
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import mechfight as mf  # noqa: E402
from economy import Economy  # noqa: E402
from evade import Evade  # noqa: E402
from fight import Fight  # noqa: E402
from heat import Heat  # noqa: E402
from judge import Judge  # noqa: E402
from legs import Legs  # noqa: E402
from memory import Memory  # noqa: E402
from torso import Torso  # noqa: E402
from triggers import Triggers  # noqa: E402
from worldmap import WorldMap  # noqa: E402


def find_map_dir(hello):
    cands = [hello.get("map", {}).get("dir", "/arena"), "/arena",
             os.path.join(HERE, "arena"), os.path.join(HERE, "..", "arena")]
    for c in cands:
        if c and os.path.exists(os.path.join(c, "v2", "world.json")):
            return c
    return cands[0]


class Ctx:
    """The shared picture for one tick. Controllers read it; none writes to it."""
    __slots__ = ("t", "me", "obs", "mem", "heat", "judge", "economy", "wm", "zone", "loot_goal", "hunt")


class Bot:
    name = "v4"

    def __init__(self, hello):
        cfg = hello["config"]
        self.cfg = cfg
        rng = random.Random(hello.get("seed", 0))
        self.wm = WorldMap(find_map_dir(hello), step_up=cfg.get("mech", {}).get("step_up", 1.2))
        self.mem = Memory(cfg, self.wm)
        self.heat = Heat(self.wm, cfg)
        self.judge = Judge(self.wm, cfg)
        self.legs = Legs(self.wm, rng, Fight(self.wm, cfg, rng, self.judge), Evade(self.wm, cfg), cfg)
        self.torso = Torso(self.judge)
        self.triggers = Triggers(self.judge)
        self.economy = Economy(self.wm, cfg)
        self.loot_goal = None
        self.max_ms = 0.0
        self.gc_ms = 0.0
        self.gc_t = 0.0
        self.part_ms = {}
        self.ticks = 0
        self.dump, self.dump_t = None, -1e9
        if os.environ.get("MECH_DUMP"):       # local debugging only: minimap for the viewer
            try:
                self.dump = open(os.environ["MECH_DUMP"], "w")
                wm = self.wm
                self.dump.write(json.dumps({"type": "mind_meta", "x0": wm.x0, "y0": wm.y0, "N": wm.N,
                                            "nnx": wm.nnx, "nny": wm.nny, "layers": True}) + "\n")
            except OSError:
                self.dump = None
        # Garbage collection off the tick path: the seen-by map and route caches hold tens of
        # thousands of small objects, and a full automatic sweep over them landed on random
        # ticks as 50+ ms pauses. Freeze what startup built, collect the young generations on a
        # timer (refcounting still frees almost everything at once).
        gc.collect()
        gc.freeze()
        gc.disable()

    def act(self, obs):
        t0 = time.perf_counter()
        self.mem.update(obs)
        t1 = time.perf_counter()
        self.heat.update(self.mem)
        t2 = time.perf_counter()
        self.judge.update(self.mem, obs, self.loot_goal, self.heat)
        t3 = time.perf_counter()

        ctx = Ctx()
        ctx.t, ctx.me, ctx.obs = obs["t"], obs["self"], obs
        ctx.mem, ctx.heat, ctx.judge, ctx.wm, ctx.zone = self.mem, self.heat, self.judge, self.wm, obs["zone"]
        ctx.loot_goal = self.loot_goal
        ctx.economy = self.economy
        me = obs["self"]
        ctx.hunt = (self.heat.hunt_goal(me["pos"][0], me["pos"][1], obs["zone"])
                    if me["hp"] >= 0.6 * me["max_hp"] else None)

        move, boost, jump = self.legs.decide(ctx)
        t4 = time.perf_counter()
        aim = self.torso.decide(ctx)
        fire, reload = self.triggers.decide(ctx)
        econ = self.economy.decide(ctx)
        self.loot_goal = self.economy.goal

        act = {"move": [round(move[0], 4), round(move[1], 4)], "boost": bool(boost),
               "aim": [round(aim[0], 4), round(aim[1], 4)], "fire": fire}
        if jump:
            act["jump"] = True
        if reload:
            act["reload"] = True
        act.update(econ)
        mf.draw.label(f"{self.legs.mode}/{self.torso.mode}/{self.triggers.mode}/{self.economy.mode}")

        if self.dump is not None and ctx.t - self.dump_t >= 0.5:
            self.dump_t = ctx.t
            self.write_mind(ctx)
        if ctx.t - self.gc_t >= 2.0:
            self.gc_t = ctx.t
            g0 = time.perf_counter()
            gc.collect(1)
            self.gc_ms = max(self.gc_ms, (time.perf_counter() - g0) * 1000)
        t5 = time.perf_counter()
        for k, a, b in (("mem", t0, t1), ("heat", t1, t2), ("judge", t2, t3), ("legs", t3, t4), ("rest", t4, t5)):
            self.part_ms[k] = max(self.part_ms.get(k, 0.0), (b - a) * 1000)
        self.max_ms = max(self.max_ms, (t5 - t0) * 1000)
        self.ticks += 1
        return act

    def write_mind(self, ctx):
        mem = self.mem
        t = ctx.t
        tracks = [[round(e.pos[0], 1), round(e.pos[1], 1), round(t - e.seen_t, 1), e.id]
                  for e in mem.tracks.values() if e.kind == "mech" and t - e.seen_t < 15]
        items = ([["cube", round(c["pos"][0], 1), round(c["pos"][1], 1)] for c in mem.cubes.values()]
                 + [[p["kind"], round(p["pos"][0], 1), round(p["pos"][1], 1)] for p in mem.pickups.values()]
                 + [["crate", round(c["pos"][0], 1), round(c["pos"][1], 1)] for c in mem.crates.values()])
        g = self.legs.goal
        plan = [g[2], round(g[0], 1), round(g[1], 1), 0] if g else None
        rec = {"type": "mind", "t": round(t, 2), "layers": self.heat.dump(), "tracks": tracks,
               "items": items, "plan": plan, "fight": self.legs.fight.cells,
               "look": [round(v, 1) for v in self.torso.look] if self.torso.look else None, "path": [[round(x, 1), round(y, 1)] for x, y in self.legs.path[:12]],
               "state": f"{self.legs.mode}/{self.torso.mode}/{self.triggers.mode}/{self.economy.mode}"
               + (f" tgt#{self.judge.target.id}" if self.judge.target else "")}
        try:
            self.dump.write(json.dumps(rec, separators=(",", ":")) + "\n")
            self.dump.flush()
        except (OSError, ValueError):
            self.dump = None

    def on_end(self, msg):
        mf.log(f"v4 end: ticks={self.ticks} max_ms={self.max_ms:.1f} kills={self.mem.kills} "
               f"fire_ticks={self.triggers.shots} gc_ms={self.gc_ms:.1f} part_max_ms={ {k: round(v, 1) for k, v in self.part_ms.items()} }")


mf.run(Bot)
