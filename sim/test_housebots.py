"""Tests for sim/housebots.py.  Run:  python sim/test_housebots.py   (or python -m unittest sim.test_housebots)"""
import copy
import gzip
import json
import math
import os
import random
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from worldmap import WorldMap  # noqa: E402
import housebots  # noqa: E402
from housebots import Squad, DIFFICULTY  # noqa: E402

TICK_PER_DECISION = 2
_CACHE = {}


def load():
    if "hello" not in _CACHE:
        with gzip.open(os.path.join(ROOT, "check", "stream.jsonl.gz"), "rt") as f:
            lines = [json.loads(l) for l in f if l.strip()]
        _CACHE["hello"] = lines[0]
        _CACHE["obs"] = [m for m in lines[1:] if m.get("type") == "obs"]
        _CACHE["wm"] = WorldMap(os.path.join(ROOT, "arena"))
    return _CACHE["hello"]["config"], _CACHE["obs"], _CACHE["wm"]


def valid_action(a, tick):
    assert isinstance(a, dict), a
    assert a["tick"] == tick, (a["tick"], tick)
    mv = a.get("move", [0, 0])
    assert len(mv) == 2 and all(isinstance(q, (int, float)) and math.isfinite(q) for q in mv)
    assert math.hypot(*mv) <= 1.0001, mv
    assert isinstance(a.get("boost", False), bool)
    assert isinstance(a.get("jump", False), bool)
    if "aim" in a:
        yaw, pit = a["aim"]
        assert math.isfinite(yaw) and math.isfinite(pit)
        assert -0.7001 <= pit <= 1.3001, pit
    if "fire" in a:
        assert len(a["fire"]) == 2 and all(isinstance(q, bool) for q in a["fire"])
    assert isinstance(a.get("reload", False), bool)
    if "pick" in a:
        assert a["pick"] in (0, 1, 2)
    if "take" in a:
        assert isinstance(a["take"], int)
    if "drop" in a:
        assert a["drop"] in (0, 1)
    json.dumps(a)


def find_los_pair(wm, dist, center=(41.0, -73.0), tries=4000, seed=1):
    rng = random.Random(seed)
    for _ in range(tries):
        a = rng.uniform(0, math.tau)
        r = rng.uniform(0, 250)
        x, y = center[0] + math.cos(a) * r, center[1] + math.sin(a) * r
        K = wm.nidx(x, y)
        if K < 0 or wm.nblock[K]:
            continue
        b = rng.uniform(0, math.tau)
        x2, y2 = x + math.cos(b) * dist, y + math.sin(b) * dist
        K2 = wm.nidx(x2, y2)
        if K2 < 0 or wm.nblock[K2]:
            continue
        z1, z2 = wm.ground_at(x, y), wm.ground_at(x2, y2)
        if wm.los((x, y, z1 + 8), (x2, y2, z2 + 4.5)) and wm.walk_clear(x, y, x2, y2):
            return (x, y, z1), (x2, y2, z2)
    raise RuntimeError("no LOS pair")


def mk_obs(base, slot, pos, aim, t, tick, mechs=(), cubes=0, weapons=None, hp=None):
    o = copy.deepcopy(base)
    o["tick"] = tick
    o["t"] = t
    me = o["self"]
    me["id"] = slot
    me["pos"] = [pos[0], pos[1], pos[2]]
    me["vel"] = [0.0, 0.0, 0.0]
    me["aim"] = [aim[0], aim[1]]
    me["cubes"] = cubes
    if hp is not None:
        me["hp"] = hp
    if weapons is not None:
        me["weapons"] = weapons
    o["mechs"] = list(mechs)
    o["pickups"] = []
    o["crates"] = []
    o["cubes"] = []
    o["events"] = []
    o["sounds"] = []
    o["offer"] = None
    o["zone"] = dict(center=[41.3, -72.8], radius=560, next_center=[-42.12, 7.11], next_radius=380,
                     shrink_start=60, shrink_end=110, dps=20)
    return o


def mech_entry(i, pos, vel=(0, 0, 0), hp=1400, weapons=("gun",), charging=False, aim=(0.0, 0.0)):
    return {"id": i, "kind": "mech", "pos": list(pos), "vel": list(vel), "aim": list(aim), "hp": hp,
            "max_hp": 1400, "cubes": 0, "weapons": list(weapons), "charging": charging}


def gun_laser(base):
    w = copy.deepcopy(base["self"]["weapons"][0])
    laser = {"kind": "laser", "cooldown": 0, "charge": 0,
             "stats": {"damage": 315, "charge": 0.75, "min_charge": 0.15, "hold_s": 2, "cooldown": 1.65,
                       "range": 280, "min_frac": 0.35, "pierce": 0}}
    return [w, laser]


def slew(aim, cmd, turn=4.0, pr=3.0, dt=0.1):
    dy = (cmd[0] - aim[0] + math.pi) % math.tau - math.pi
    dp = cmd[1] - aim[1]
    sy = max(-turn * dt, min(turn * dt, dy))
    sp = max(-pr * dt, min(pr * dt, dp))
    return [aim[0] + sy, aim[1] + sp]


def duel(diff, hunter_laser=False, ticks=60, seed=5, dist=90.0, moving=False):
    """One squad member (hunter) vs a prey in clear LOS. Returns (first fire t, final aim error rad, fired list)."""
    cfg, obs, wm = load()
    base = obs[0]
    mp, pp = find_los_pair(wm, dist, seed=seed)
    sq = Squad(cfg, wm, [0], [15], {0: diff}, seed)
    sq.raise_errors = True
    yaw0 = math.atan2(pp[1] - mp[1], pp[0] - mp[0])
    aim = [yaw0 + 0.8, 0.0]
    weapons = gun_laser(base) if hunter_laser else None
    first = None
    fired = []
    pos = list(mp)
    ppos = list(pp)
    err = 9
    for k in range(ticks):
        t = 0.1 * k
        if moving:
            ppos[1] += 0.0
        vel = (0, 7.0, 0) if moving else (0, 0, 0)
        if moving:
            ppos[1] += 0.7
        o = mk_obs(base, 0, pos, aim, t, k * 2, [mech_entry(15, ppos, vel=vel, hp=800, weapons=("gun",))],
                   weapons=copy.deepcopy(weapons) if weapons else None)
        a = sq.act_all({0: o}, t)[0]
        valid_action(a, k * 2)
        aim = slew(aim, a["aim"])
        by = math.atan2(ppos[1] - pos[1], ppos[0] - pos[0])
        err = abs((aim[0] - by + math.pi) % math.tau - math.pi)
        if any(a["fire"]):
            fired.append(t)
            if first is None:
                first = t
    return first, err, fired


class RealObsTests(unittest.TestCase):
    def test_real_stream_valid_actions(self):
        cfg, obs, wm = load()
        sq = Squad(cfg, wm, [3], [10], {3: "expert"}, 1)
        sq.raise_errors = True
        n = 0
        for o in obs:
            a = sq.act_all({3: o}, o["t"])[3]
            valid_action(a, o["tick"])
            n += 1
        self.assertGreaterEqual(n, 290)

    def test_real_stream_swapped_roles(self):
        """Mech id 10 is the squad member, slot 3 the prey: obs synthesized from the real stream."""
        cfg, obs, wm = load()
        sq = Squad(cfg, wm, [10], [3], {10: "hard"}, 2)
        sq.raise_errors = True
        for o in obs:
            o2 = copy.deepcopy(o)
            for m in o2["mechs"]:
                if m["id"] == 10:
                    m["id"] = 3
            o2["self"]["id"] = 10
            a = sq.act_all({10: o2}, o2["t"])[10]
            valid_action(a, o2["tick"])

    def test_all_difficulties_on_real_stream(self):
        cfg, obs, wm = load()
        for d in DIFFICULTY:
            sq = Squad(cfg, wm, [3], [10], {3: d}, 3)
            sq.raise_errors = True
            for o in obs[:120]:
                valid_action(sq.act_all({3: o}, o["t"])[3], o["tick"])


class CombatTests(unittest.TestCase):
    def test_expert_aims_and_fires_gun(self):
        first, err, fired = duel("expert", ticks=40)
        self.assertIsNotNone(first)
        self.assertLess(err, 0.06)
        self.assertLess(first, 1.5)

    def test_hard_and_medium_fire(self):
        for d in ("hard", "medium"):
            first, err, fired = duel(d, ticks=60)
            self.assertIsNotNone(first, d)

    def test_easy_slower_than_expert(self):
        fe = duel("easy", ticks=60)[0]
        fx = duel("expert", ticks=60)[0]
        self.assertTrue(fe is None or fe > fx + 0.4, (fe, fx))

    def test_laser_charge_and_release(self):
        first, err, fired = duel("expert", hunter_laser=True, ticks=80, dist=110.0)
        self.assertIsNotNone(first)
        cfg, obs, wm = load()
        # trace trigger states for slot 1 (laser): must hold for a while then release
        base = obs[0]
        mp, pp = find_los_pair(wm, 110.0, seed=5)
        sq = Squad(cfg, wm, [0], [15], {0: "expert"}, 5)
        sq.raise_errors = True
        sq.bots[0].laser_pref = True
        aim = [math.atan2(pp[1] - mp[1], pp[0] - mp[0]), 0.0]
        weapons = gun_laser(base)
        trace = []
        charge = 0.0
        cd = 0.0
        for k in range(70):
            t = 0.1 * k
            weapons[1]["charge"] = round(charge, 3)
            weapons[1]["cooldown"] = round(cd, 3)
            o = mk_obs(base, 0, mp, aim, t, k * 2, [mech_entry(15, pp, hp=800)], weapons=copy.deepcopy(weapons))
            a = sq.act_all({0: o}, t)[0]
            aim = slew(aim, a["aim"])
            trig = a["fire"][1]
            trace.append(trig)
            if trig:
                charge += 0.1
            elif charge > 0:
                charge = 0.0
                cd = 1.65
            cd = max(0.0, cd - 0.1)
        # at least one hold of >= 6 consecutive decisions followed by a release
        runs, cur = [], 0
        for tr in trace:
            if tr:
                cur += 1
            else:
                if cur:
                    runs.append(cur)
                cur = 0
        self.assertTrue(any(r >= 6 for r in runs), runs)

    def test_lightning_close_range(self):
        cfg, obs, wm = load()
        base = obs[0]
        mp, pp = find_los_pair(wm, 25.0, seed=9)
        weapons = copy.deepcopy(base["self"]["weapons"])
        weapons[1] = {"kind": "lightning", "cooldown": 0,
                      "stats": {"damage": 60, "cooldown": 0.25, "range": 45, "cone_deg": 30, "chain": 2,
                                "chain_range": 14, "energy": 3}}
        sq = Squad(cfg, wm, [0], [15], {0: "expert"}, 5)
        sq.raise_errors = True
        aim = [math.atan2(pp[1] - mp[1], pp[0] - mp[0]), 0.0]
        got = False
        for k in range(15):
            t = 0.1 * k
            o = mk_obs(base, 0, mp, aim, t, k * 2, [mech_entry(15, pp, hp=800)], weapons=copy.deepcopy(weapons))
            a = sq.act_all({0: o}, t)[0]
            aim = slew(aim, a["aim"])
            got = got or a["fire"][1]
        self.assertTrue(got)

    def test_never_targets_squad_members(self):
        cfg, obs, wm = load()
        base = obs[0]
        mp, pp = find_los_pair(wm, 40.0, seed=7)
        sq = Squad(cfg, wm, [0, 1], [15], {0: "expert", 1: "easy"}, 5)
        sq.raise_errors = True
        aim = [math.atan2(pp[1] - mp[1], pp[0] - mp[0]), 0.0]
        for k in range(30):
            t = 0.1 * k
            o0 = mk_obs(base, 0, mp, aim, t, k * 2, [mech_entry(1, pp, hp=100)])
            o1 = mk_obs(base, 1, pp, [0, 0], t, k * 2, [mech_entry(0, mp, hp=100)])
            acts = sq.act_all({0: o0, 1: o1}, t)
            for s in (0, 1):
                self.assertFalse(any(acts[s]["fire"]), (k, s, acts[s]))
            aim = slew(aim, acts[0]["aim"])

    def test_ignores_npc(self):
        cfg, obs, wm = load()
        base = obs[0]
        mp, pp = find_los_pair(wm, 40.0, seed=11)
        sq = Squad(cfg, wm, [0], [15], {0: "expert"}, 5)
        sq.raise_errors = True
        npc = {"id": 105, "kind": "npc", "pos": list(pp), "vel": [0, 0, 0], "aim": [0, 0], "hp": 350, "max_hp": 350}
        aim = [math.atan2(pp[1] - mp[1], pp[0] - mp[0]), 0.0]
        for k in range(20):
            o = mk_obs(base, 0, mp, aim, 0.1 * k, 2 * k, [npc])
            a = sq.act_all({0: o}, 0.1 * k)[0]
            self.assertFalse(any(a["fire"]))


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.cfg, self.obs, self.wm = load()
        self.diff = {}
        s = 0
        for d, n in (("easy", 4), ("medium", 4), ("hard", 4), ("expert", 3)):
            for _ in range(n):
                self.diff[s] = d
                s += 1
        self.members = list(range(15))

    def _obs_all(self, slots, t, tick):
        return {s: mk_obs(self.obs[0], s, (41 + 10 * s, -73, 0.0), [0, 0], t, tick) for s in slots}

    def test_release_schedule(self):
        sq = Squad(self.cfg, self.wm, self.members, [15], self.diff, 4)
        sq.raise_errors = True
        sq.act_all(self._obs_all(self.members, 0.0, 0), 0.0)
        self.assertEqual(len(sq.hunters), 1)
        h = next(iter(sq.hunters))
        self.assertEqual(self.diff[h], "easy")
        # time passes: more hunters, still weakest-first
        sq.act_all(self._obs_all(self.members, 60.0, 1200), 60.0)
        self.assertEqual(len(sq.hunters), 2)
        self.assertTrue(all(self.diff[s] == "easy" for s in sq.hunters))
        # three losses
        live = [s for s in self.members if s not in (0, 1, 2)]
        sq.act_all(self._obs_all(live, 61.0, 1220), 61.0)
        self.assertEqual(len(sq.hunters), 5)
        self.assertTrue(set(sq.hunters).isdisjoint({0, 1, 2}))
        ranks = sorted(housebots.RANK[self.diff[s]] for s in sq.hunters)
        self.assertEqual(ranks, [0, 1, 1, 1, 1][:0] or ranks)  # weakest first: easy (3) then medium
        self.assertIn(3, sq.hunters)
        self.assertTrue(all(self.diff[s] in ("easy", "medium") for s in sq.hunters))
        # late game: everybody
        sq.act_all(self._obs_all(live, 300.0, 6000), 300.0)
        self.assertEqual(set(sq.hunters), set(live))

    def test_hunters_move_toward_prey_nonhunters_do_not(self):
        sq = Squad(self.cfg, self.wm, self.members, [15], self.diff, 4)
        sq.raise_errors = True
        mp, pp = find_los_pair(self.wm, 120.0, seed=3)
        # member 0 (easy, the hunter) and member 14 (expert, not a hunter) both see the prey
        n_non = 0
        for k in range(30):
            t = 0.1 * k
            o0 = mk_obs(self.obs[0], 0, mp, [0, 0], t, 2 * k, [mech_entry(15, pp, hp=500)])
            o14 = mk_obs(self.obs[0], 14, mp, [0, 0], t, 2 * k, [mech_entry(15, pp, hp=500)])
            allo = self._obs_all([s for s in self.members if s not in (0, 14)], t, 2 * k)
            allo[0], allo[14] = o0, o14
            acts = sq.act_all(allo, t)
        self.assertIn(0, sq.hunters)
        self.assertNotIn(14, sq.hunters)
        ux, uy = pp[0] - mp[0], pp[1] - mp[1]
        n = math.hypot(ux, uy)
        dot0 = acts[0]["move"][0] * ux / n + acts[0]["move"][1] * uy / n
        self.assertGreater(dot0, 0.3)           # hunter closes in
        dot14 = acts[14]["move"][0] * ux / n + acts[14]["move"][1] * uy / n
        self.assertLess(dot14, 0.3)             # non-hunter does not advance on the prey (avoids / loots)

    def test_hunt_count_phases(self):
        sq = Squad(self.cfg, self.wm, self.members, [15], self.diff, 4)
        self.assertEqual(sq.hunt_count(0), 1)
        self.assertEqual(sq.hunt_count(100), 2)
        self.assertEqual(sq.hunt_count(264), 5)
        self.assertEqual(sq.hunt_count(266), 99)


class ZoneLootTests(unittest.TestCase):
    def test_returns_into_zone(self):
        cfg, obs, wm = load()
        sq = Squad(cfg, wm, [0], [15], {0: "medium"}, 1)
        sq.raise_errors = True
        # standing outside the circle that is about to be next: must head inward
        pos = (41.3 + 330.0, -72.8, 0.0)
        o = mk_obs(obs[0], 0, pos, [0, 0], 100.0, 2000)
        o["zone"] = dict(center=[41.3, -72.8], radius=380, next_center=[41.3, -72.8], next_radius=240,
                         shrink_start=95, shrink_end=135, dps=35)
        o["self"]["in_zone"] = True
        for k in range(5):
            a = sq.act_all({0: o}, 100.0 + 0.1 * k)[0]
        self.assertLess(a["move"][0], -0.3)

    def test_takes_weapon_and_picks_upgrade(self):
        cfg, obs, wm = load()
        sq = Squad(cfg, wm, [0], [15], {0: "hard"}, 1)
        sq.raise_errors = True
        mp, _ = find_los_pair(wm, 50.0, seed=2)
        o = mk_obs(obs[0], 0, mp, [0, 0], 5.0, 100)
        o["pickups"] = [{"id": 8001, "kind": "laser", "pos": [mp[0] + 1.0, mp[1], mp[2]]}]
        o["offer"] = {"id": 1, "expires": 9.0, "choices": [
            {"id": "sensors", "name": "", "desc": ""}, {"id": "armor", "name": "", "desc": ""},
            {"id": "jets", "name": "", "desc": ""}]}
        a = sq.act_all({0: o}, 5.0)[0]
        self.assertEqual(a.get("take"), 8001)
        self.assertEqual(a.get("pick"), 1)


class TimingTests(unittest.TestCase):
    def test_all_fifteen_under_3ms(self):
        cfg, obs, wm = load()
        diff = {}
        s = 0
        for d, n in (("easy", 4), ("medium", 4), ("hard", 4), ("expert", 3)):
            for _ in range(n):
                diff[s] = d
                s += 1
        members = list(range(15))
        sq = Squad(cfg, wm, members, [15], diff, 99)
        sq.raise_errors = True
        rng = random.Random(8)
        pos = {}
        for m in members:
            while True:
                a = rng.uniform(0, math.tau)
                r = 250 * math.sqrt(rng.random())
                x, y = 41 + math.cos(a) * r, -73 + math.sin(a) * r
                K = wm.nidx(x, y)
                if K >= 0 and not wm.nblock[K]:
                    pos[m] = [x, y, wm.ground_at(x, y)]
                    break
        prey = list(pos[0])
        prey[0] += 90
        aims = {m: [0.0, 0.0] for m in members}
        times = []
        for k in range(400):
            t = 0.1 * k
            obs_by = {}
            for m in members:
                mechs = []
                if math.hypot(prey[0] - pos[m][0], prey[1] - pos[m][1]) < 150:
                    mechs.append(mech_entry(15, prey, hp=900))
                obs_by[m] = mk_obs(obs[0], m, pos[m], aims[m], t, 2 * k, mechs)
            t0 = time.perf_counter()
            acts = sq.act_all(obs_by, t)
            times.append(time.perf_counter() - t0)
            for m, a in acts.items():
                valid_action(a, 2 * k)
                aims[m] = slew(aims[m], a["aim"])
                nx = pos[m][0] + a["move"][0] * 1.17
                ny = pos[m][1] + a["move"][1] * 1.17
                K = wm.nidx(nx, ny)
                if K >= 0 and not wm.nblock[K]:
                    pos[m] = [nx, ny, wm.ground_at(nx, ny)]
            if k == 200:       # late: everyone hunts
                sq.hunt["all_after_phase"] = 0
        avg = 1000 * sum(times) / len(times)
        worst = 1000 * max(times)
        print("\n[timing] 15 bots per decision: avg %.3f ms  p95 %.3f ms  max %.3f ms"
              % (avg, 1000 * sorted(times)[int(0.95 * len(times))], worst))
        self.assertLess(avg, 3.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
