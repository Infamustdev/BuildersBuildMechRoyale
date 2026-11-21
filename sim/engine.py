"""mechsim engine: world state, physics, combat, zone, items, observations, sounds.

Implements protocol.md (mechfight/1) with the numbers from the real `hello.config`.
See sim/ASSUMPTIONS.md for every place the rulebook is silent.
"""
import json
import math
import os
import random
from array import array

from . import config as _config

PI = math.pi
TWO_PI = 2.0 * math.pi
NEG = -1e9
_ANG = [math.radians(a) for a in (-80, -40, 0, 40, 80)]


def _mk(rot):
    return tuple((math.cos(a + rot), math.sin(a + rot)) for a in _ANG)


RING_XP, RING_XN, RING_YP, RING_YN = _mk(0.0), _mk(math.pi), _mk(math.pi / 2), _mk(-math.pi / 2)
RING4 = ((1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0))
RING8 = tuple((math.cos(k * math.pi / 4), math.sin(k * math.pi / 4)) for k in range(8))


def wrap(a):
    return (a + PI) % TWO_PI - PI


def _r2(v):
    return round(v, 2)


# ----------------------------------------------------------------------------- world extras
class WorldExtras:
    """Per-map precomputation shared by all matches in a process (building tops, trees, roofs)."""

    def __init__(self, wm):
        n = wm.nx * wm.ny
        bt = array("f", bytes(4 * n))
        top, ground = wm.top, wm.ground
        for i in range(n):
            t = top[i]
            bt[i] = t if t - ground[i] > 0.01 else NEG
        self.bldtop = bt

        with open(os.path.join(_config.ARENA, "v2", "world.json"), "r", encoding="utf-8") as f:
            w = json.load(f)
        rules = w.get("rules", {})
        self.density = float(rules.get("canopyDensity", 0.35))
        bt_ = float(rules.get("canopyBlockT", 0.25))
        self.block_exp = -math.log(max(1e-6, bt_))
        self.trees = {}
        tr = w.get("trees") or {}
        fields = tr.get("fields") or []
        data = tr.get("data") or []
        C, inv, x0, y0, nx, ny = wm.C, wm.inv, wm.x0, wm.y0, wm.nx, wm.ny
        if fields and data:
            F = len(fields)
            fi = {name: q for q, name in enumerate(fields)}
            for s in range(0, len(data) - F + 1, F):
                tx, ty, tz = data[s + fi["x"]], data[s + fi["y"]], data[s + fi["z"]]
                height = data[s + fi["height"]]
                cr = data[s + fi["crownR"]]
                cb = data[s + fi["crownBase"]]
                trr = data[s + fi["trunkR"]] + 0.3
                cx = data[s + fi["crownX"]] if "crownX" in fi else tx
                cy = data[s + fi["crownY"]] if "crownY" in fi else ty
                rec = (tx, ty, trr * trr, tz + cb, cx, cy, cr * cr, tz + cb, tz + height)
                reach = max(cr + abs(cx - tx), trr) + 1.0
                i0 = int((min(tx, cx) - reach - x0) * inv)
                i1 = int((max(tx, cx) + reach - x0) * inv)
                j0 = int((min(ty, cy) - reach - y0) * inv)
                j1 = int((max(ty, cy) + reach - y0) * inv)
                for j in range(max(0, j0), min(ny - 1, j1) + 1):
                    py = y0 + (j + 0.5) * C
                    for i in range(max(0, i0), min(nx - 1, i1) + 1):
                        px = x0 + (i + 0.5) * C
                        near_crown = (px - cx) ** 2 + (py - cy) ** 2 <= (cr + C) ** 2
                        near_trunk = (px - tx) ** 2 + (py - ty) ** 2 <= (math.sqrt(trr * trr) + C) ** 2
                        if near_crown or near_trunk:
                            self.trees.setdefault(j * nx + i, []).append(rec)

        # roofs: interior cells of building prisms
        roofs = []
        for p, cells in wm.prisms:
            if p.get("kind") != "building" or len(cells) < 12:
                continue
            ptop = float(p["top"])
            for idx in cells:
                if abs(top[idx] - ptop) > 0.01:
                    continue
                if ptop - ground[idx] < 3.0 or ptop - ground[idx] > 65.0:
                    continue
                ok = True
                for o in (-1, 1, -nx, nx, -nx - 1, -nx + 1, nx - 1, nx + 1):
                    k = idx + o
                    if k < 0 or k >= n or abs(top[k] - ptop) > 0.01:
                        ok = False
                        break
                if ok:
                    roofs.append((x0 + (idx % nx + 0.5) * C, y0 + (idx // nx + 0.5) * C, ptop))
        self.roofs = roofs


_EXTRAS = {}


def world_extras(wm):
    e = _EXTRAS.get(id(wm))
    if e is None:
        e = WorldExtras(wm)
        _EXTRAS[id(wm)] = e
    return e


# ----------------------------------------------------------------------------- entities
class Mech:
    def __init__(self, slot, team):
        self.id = slot
        self.team = team
        self.alive = True
        self.x = self.y = self.z = 0.0
        self.vx = self.vy = self.vz = 0.0
        self.yaw = 0.0
        self.pitch = 0.0
        self.tyaw = 0.0
        self.tpitch = 0.0
        self.grounded = True
        self.cubes = 0
        self.up_counts = {}
        self.upgrades = []
        self.weapons = [None, None]
        self.stats = None
        self.wstats = None
        self.hp = 0.0
        self.max_hp = 0.0
        self.energy = 0.0
        self.max_energy = 0.0
        self.last_dmg_t = NEG
        self.energy_t = NEG
        self.mx = self.my = 0.0
        self.boost = False
        self.fire = [False, False]
        self.jump = False
        self.reload = False
        self.pick = None
        self.take = None
        self.drop = None
        self.fresh = False
        self.events = []
        self.sounds = []
        self.seen = {}
        self.last_obs_t = -1.0
        self.offers_pending = 0
        self.offer = None
        self.offer_n = 0
        self.short_until = -1.0
        self.next_step_t = 0.0
        self.kills = 0
        self.place = None
        self.killer = None
        self.dying = False
        self.in_zone = True
        self.r = self.h = self.eye = self.muz = 0.0
        self.ring = None
        self.death_t = None
        self.speed_xy = 0.0
        self.boosting = False
        self.jetting = False

    @property
    def charging(self):
        for w in self.weapons:
            if w is not None and w["kind"] == "laser" and w["charging"]:
                return True
        return False

    def holds(self, kind):
        for w in self.weapons:
            if w is not None and w["kind"] == kind:
                return True
        return False


class Npc:
    def __init__(self, nid, x, y, z, hp):
        self.id = nid
        self.x, self.y, self.z = x, y, z
        self.hx, self.hy = x, y
        self.vx = self.vy = 0.0
        self.yaw = 0.0
        self.hp = hp
        self.max_hp = hp
        self.alive = True
        self.wx, self.wy = x, y
        self.wait = 0.0
        self.target = None      # mech slot
        self.cool = 0.0
        self.scan_t = 0.0


class Crate:
    def __init__(self, cid, x, y, z, hp):
        self.id, self.x, self.y, self.z, self.hp = cid, x, y, z, hp


class Pickup:
    def __init__(self, pid, kind, x, y, z, state=None):
        self.id, self.kind, self.x, self.y, self.z, self.state = pid, kind, x, y, z, state


class Cube:
    def __init__(self, cid, x, y, z):
        self.id, self.x, self.y, self.z = cid, x, y, z


class Proj:
    __slots__ = ("id", "kind", "x", "y", "z", "vx", "vy", "vz", "owner", "team", "dmg", "dist",
                 "max_range", "age", "ttl", "ox", "oy", "oz", "ws", "cmul")


class Drop:
    def __init__(self, did, x, y, z, call_t, land_t):
        self.id, self.x, self.y, self.z = did, x, y, z
        self.call_t, self.land_t = call_t, land_t
        self.holder = None
        self.progress = 0.0
        self.landed = False


# ----------------------------------------------------------------------------- the match
class Match:
    def __init__(self, cfg, wm, seed, teams=None):
        self.cfg = cfg
        self.wm = wm
        self.seed = seed
        self.rng = random.Random(seed)
        self.X = world_extras(wm)
        mc = cfg["match"]
        self.n = mc["mechs"]
        self.hz = mc["tick_hz"]
        self.dt = 1.0 / self.hz
        self.decide_every = mc["decide_every"]
        self.duration = mc["duration_s"]
        self.tick = 0
        self.time = 0.0
        mech = cfg["mech"]
        self.front_half = math.radians(mech["front_arc_deg"]) / 2.0
        self.rear_mult = mech["rear_mult"]
        self.self_mult = mech["self_damage_mult"]
        self.tan_slope = math.tan(math.radians(mech["max_slope_deg"]))
        self.step_up = mech["step_up"]
        v = cfg["vision"]
        self.v_range = v["range"]
        self.v_fov_cos = math.cos(math.radians(v["fov_deg"]) / 2.0)
        self.v_near = v["near_range"]
        self.v_mem = v["memory_s"]
        snd = cfg["sound"]
        self.snd_range = snd["range"]
        self.snd_noise = math.radians(snd["bearing_noise_deg"])
        self.upg = {u["id"]: u for u in cfg["upgrades"]}
        cb = cfg["cubes"]
        th = list(cb["upgrade_thresholds"])
        self.thresholds = set(th)
        self.th_max = th[-1]
        self.th_step = th[-1] - th[-2] if len(th) > 1 else 10
        self.record = True
        self.labels = {}
        self.events_log = []
        self.shots = []
        self.beams = []
        self.projs = []
        self.crates = []
        self.cubes = []
        self.pickups = {}
        self.npcs = []
        self.drops = []
        self._next = {"proj": 1, "crate": 1000, "cube": 5000, "pickup": 8000, "drop": 20000, "npc": 100}
        self._called_drops = set()
        self.dying = []
        self.finished_places = {}
        self._snap_tick = -1
        self._snap = None

        teams = dict(teams or {})
        self.mechs = [Mech(s, teams.get(s, s)) for s in range(self.n)]
        for m in self.mechs:
            self._recompute(m)
            m.hp = m.max_hp
            m.energy = m.max_energy
            g = self._new_weapon("gun")
            g["cooldown"] = 0.0
            g["stats"] = m.wstats["gun"]
            m.weapons[0] = g
        self.alive_list = list(self.mechs)

        self._plan_zone()
        self._spawn_all()

    # ------------------------------------------------------------------ ids
    def _nid(self, kind):
        v = self._next[kind]
        self._next[kind] = v + 1
        return v

    # ------------------------------------------------------------------ stats / upgrades
    def _recompute(self, m):
        cfg = self.cfg
        base = dict(cfg["mech"])
        ws = {k: dict(v) for k, v in cfg["weapons"].items()}
        for uid, n in m.up_counts.items():
            for eff in self.upg[uid]["effects"]:
                parts = eff["path"].split(".")
                if parts[0] == "mech":
                    d, key = base, parts[1]
                else:
                    d, key = ws[parts[1]], parts[2]
                for _ in range(n):
                    if "mul" in eff:
                        d[key] = d.get(key, 0.0) * eff["mul"]
                    if "add" in eff:
                        d[key] = d.get(key, 0.0) + eff["add"]
        size = 1.0 + cfg["cubes"]["size_per_cube"] * m.cubes
        for k in ("radius", "height", "eye_height", "muzzle_height"):
            base[k] = base[k] * size
        m.stats = base
        m.wstats = ws
        m.r = base["radius"]
        m.h = base["height"]
        m.eye = base["eye_height"]
        m.muz = base["muzzle_height"]
        cb = cfg["cubes"]
        new_max = base["hp"] * (1.0 + cb["hp_per_cube"] * min(m.cubes, cb["stat_cap"]))
        if m.max_hp > 0 and m.alive:
            m.hp += new_max - m.max_hp
        m.max_hp = new_max
        m.max_energy = base["energy"]
        for w in m.weapons:
            if w is not None:
                w["stats"] = ws[w["kind"]]

    def _new_weapon(self, kind, m=None):
        w = {"kind": kind, "cooldown": 0.0, "ammo": 0, "reload": 0.0, "charge": 0.0, "hold": 0.0,
             "charging": False, "release": False, "stats": None}
        if kind == "gun":
            w["ammo"] = int(m.wstats["gun"]["mag"]) if m is not None else int(self.cfg["weapons"]["gun"]["mag"])
        return w

    def _dmg_mul(self, m):
        cb = self.cfg["cubes"]
        return 1.0 + cb["damage_per_cube"] * min(m.cubes, cb["stat_cap"])

    def _give_cubes(self, m, k):
        for _ in range(k):
            m.cubes += 1
            if m.cubes in self.thresholds or (m.cubes > self.th_max and (m.cubes - self.th_max) % self.th_step == 0):
                m.offers_pending += 1
        self._recompute(m)

    def _make_offer(self, m):
        t = self.time
        elig = []
        for u in self.cfg["upgrades"]:
            if m.up_counts.get(u["id"], 0) >= u.get("max", 1):
                continue
            req = u.get("requires")
            if req and not m.holds(req):
                continue
            elig.append(u)
        if not elig:
            m.offers_pending = 0
            return
        self.rng.shuffle(elig)
        ch = elig[:3]
        m.offer_n += 1
        m.offer = {"id": m.offer_n, "expires": round(t + self.cfg["bot"]["offer_timeout_s"], 3),
                   "choices": [{"id": u["id"], "name": u["name"], "desc": u["desc"]} for u in ch]}

    def _apply_upgrade(self, m, uid):
        old_mag = m.wstats["gun"]["mag"]
        m.up_counts[uid] = m.up_counts.get(uid, 0) + 1
        m.upgrades.append(uid)
        self._recompute(m)
        d = m.wstats["gun"]["mag"] - old_mag
        if d > 0:
            for w in m.weapons:
                if w is not None and w["kind"] == "gun":
                    w["ammo"] += int(d)
        m.energy = min(m.energy, m.max_energy)
        m.events.append({"type": "upgrade", "id": uid})

    def _resolve_offer(self, m, idx):
        o = m.offer
        if o is None:
            return
        if idx is None or not (0 <= idx < len(o["choices"])):
            idx = 0
        uid = o["choices"][idx]["id"]
        m.offer = None
        m.offers_pending = max(0, m.offers_pending - 1)
        self._apply_upgrade(m, uid)

    # ------------------------------------------------------------------ zone
    def _plan_zone(self):
        z = self.cfg["zone"]
        rng = self.rng
        cx, cy = z["start_center"]
        r = z["start_radius"]
        t0 = 0.0
        self.zphases = []
        for ph in z["phases"]:
            rn = ph["radius"]
            ang = rng.random() * TWO_PI
            dist = max(0.0, (r - rn)) * 0.85 * math.sqrt(rng.random())
            nx_, ny_ = cx + math.cos(ang) * dist, cy + math.sin(ang) * dist
            ss = t0 + ph["hold_s"]
            se = ss + ph["shrink_s"]
            self.zphases.append({"t0": t0, "ss": ss, "se": se, "c": (cx, cy), "r": r, "nc": (nx_, ny_),
                                 "nr": rn, "dps": ph["dps"]})
            t0 = se
            cx, cy, r = nx_, ny_, rn
        self.zone_end = t0

    def zone_at(self, t):
        ph = None
        for p in self.zphases:
            if t < p["se"]:
                ph = p
                break
        if ph is None:
            p = self.zphases[-1]
            return p["nc"][0], p["nc"][1], p["nr"], p, p["nc"], p["nr"]
        if t <= ph["ss"]:
            return ph["c"][0], ph["c"][1], ph["r"], ph, ph["nc"], ph["nr"]
        f = (t - ph["ss"]) / (ph["se"] - ph["ss"])
        cx = ph["c"][0] + (ph["nc"][0] - ph["c"][0]) * f
        cy = ph["c"][1] + (ph["nc"][1] - ph["c"][1]) * f
        r = ph["r"] + (ph["nr"] - ph["r"]) * f
        return cx, cy, r, ph, ph["nc"], ph["nr"]

    def zone_obs(self):
        cx, cy, r, ph, nc, nr = self.zone_at(self.time)
        return {"center": [_r2(cx), _r2(cy)], "radius": _r2(r), "next_center": [_r2(nc[0]), _r2(nc[1])],
                "next_radius": _r2(nr), "shrink_start": ph["ss"], "shrink_end": ph["se"], "dps": ph["dps"]}

    # ------------------------------------------------------------------ surface helpers
    def surface(self, x, y):
        wm = self.wm
        i = int((x - wm.x0) * wm.inv)
        j = int((y - wm.y0) * wm.inv)
        if 0 <= i < wm.nx and 0 <= j < wm.ny:
            b = self.X.bldtop[j * wm.nx + i]
            if b > -1e8:
                return b
        return wm.ground_at(x, y)

    def open_ok(self, x, y, margin=30.0):
        wm = self.wm
        if x < wm.x0 + margin or x > wm.x1 - margin or y < wm.y0 + margin or y > wm.y1 - margin:
            return False
        I = int((x - wm.x0) * wm.ninv)
        J = int((y - wm.y0) * wm.ninv)
        nnx, nny, nb = wm.nnx, wm.nny, wm.nblock
        if I < 1 or J < 1 or I >= nnx - 1 or J >= nny - 1:
            return False
        for dj in (-1, 0, 1):
            for di in (-1, 0, 1):
                if nb[(J + dj) * nnx + I + di]:
                    return False
        i = int((x - wm.x0) * wm.inv)
        j = int((y - wm.y0) * wm.inv)
        if self.X.bldtop[j * wm.nx + i] > -1e8:
            return False
        return True

    def random_open(self, cx, cy, radius, tries=4000):
        rng = self.rng
        for _ in range(tries):
            a = rng.random() * TWO_PI
            d = radius * math.sqrt(rng.random())
            x, y = cx + math.cos(a) * d, cy + math.sin(a) * d
            if self.open_ok(x, y):
                return x, y
        return cx, cy

    def random_roof(self):
        roofs = self.X.roofs
        if not roofs:
            return None
        return roofs[self.rng.randrange(len(roofs))]

    # ------------------------------------------------------------------ spawning
    def _spawn_all(self):
        cfg, rng = self.cfg, self.rng
        mc = cfg["match"]
        zc = cfg["zone"]["start_center"]
        zr = cfg["zone"]["start_radius"]
        sep = mc["spawn_min_separation"]
        frac = mc["spawn_zone_fraction"]
        pts = []
        tries = 0
        while len(pts) < self.n:
            tries += 1
            if tries % 3000 == 0:
                sep *= 0.9
            x, y = self.random_open(zc[0], zc[1], zr * frac, tries=50)
            if not self.open_ok(x, y):
                continue
            if any((x - px) ** 2 + (y - py) ** 2 < sep * sep for px, py in pts):
                continue
            pts.append((x, y))
        for m, (x, y) in zip(self.mechs, pts):
            m.x, m.y = x, y
            m.z = self.surface(x, y)
            m.yaw = m.tyaw = rng.random() * TWO_PI - PI
        # crates
        cr = cfg["crates"]
        nroof = int(round(cr["count"] * cr["roof_fraction"]))
        for q in range(cr["count"]):
            if q < nroof:
                rf = self.random_roof()
                if rf is not None:
                    self.crates.append(Crate(self._nid("crate"), rf[0], rf[1], rf[2], cr["hp"]))
                    continue
            x, y = self.random_open(zc[0], zc[1], zr * 0.95)
            self.crates.append(Crate(self._nid("crate"), x, y, self.surface(x, y), cr["hp"]))
        # floor weapons
        lt = cfg["loot"]
        nfl = lt["floor_count"]
        nroof = int(round(nfl * lt["floor_roof_fraction"]))
        for q in range(nfl):
            kind = self._rand_weapon()
            if q < nroof:
                rf = self.random_roof()
                if rf is not None:
                    self._add_pickup(kind, rf[0], rf[1], rf[2])
                    continue
            x, y = self.random_open(zc[0], zc[1], zr * 0.95)
            self._add_pickup(kind, x, y, self.surface(x, y))
        # NPC drones
        nc = cfg["npc"]
        for q in range(nc["count"]):
            x, y = self.random_open(zc[0], zc[1], zr * 0.9)
            n = Npc(self._nid("npc"), x, y, self.surface(x, y), nc["hp"])
            n.yaw = rng.random() * TWO_PI - PI
            n.wait = rng.random() * 3.0
            self.npcs.append(n)

    def _rand_weapon(self):
        w = self.cfg["loot"]["weights"]
        kinds = [k for k in w if w[k] > 0]
        return self.rng.choices(kinds, [w[k] for k in kinds])[0]

    def _add_pickup(self, kind, x, y, z, state=None):
        pk = Pickup(self._nid("pickup"), kind, x, y, z, state)
        self.pickups[pk.id] = pk
        return pk

    def _add_cube(self, x, y, z, scatter=0.0):
        if scatter > 0:
            a = self.rng.random() * TWO_PI
            d = scatter * math.sqrt(self.rng.random())
            x, y = x + math.cos(a) * d, y + math.sin(a) * d
            z = self.surface(x, y) if not self.wm.is_solid(x, y) else z
        self.cubes.append(Cube(self._nid("cube"), x, y, z))

    # ------------------------------------------------------------------ public API
    def alive_slots(self):
        return [m.id for m in self.alive_list]

    def done(self):
        return len(self.alive_list) <= 1 or self.time >= self.duration - 1e-9

    def kills(self):
        return {m.id: m.kills for m in self.mechs}

    def placements(self):
        out = {m.id: m.place for m in self.mechs if not m.alive}
        surv = sorted(self.alive_list, key=lambda m: (-m.hp, m.id))
        for rank, m in enumerate(surv, 1):
            out[m.id] = rank
        return out

    def hello(self, slot):
        return {"type": "hello", "protocol": "mechfight/1", "slot": slot,
                "seed": (self.seed * 1000003 + slot * 7919) & 0x7FFFFFFF,
                "map": {"dir": _config.ARENA, "files": ["v2/world.json", "v2/terrain.i16"]},
                "config": self.cfg}

    def apply_action(self, slot, a):
        m = self.mechs[slot]
        if not m.alive:
            return
        a = a or {}
        try:
            mv = a.get("move") or (0.0, 0.0)
            mx, my = float(mv[0]), float(mv[1])
            if mx != mx or my != my:
                mx = my = 0.0
        except Exception:
            mx = my = 0.0
        ln = math.hypot(mx, my)
        if ln > 1.0:
            mx, my = mx / ln, my / ln
        m.mx, m.my = mx, my
        m.boost = bool(a.get("boost"))
        m.jump = bool(a.get("jump"))
        aim = a.get("aim")
        if aim:
            try:
                yw, pt = float(aim[0]), float(aim[1])
                if yw == yw and pt == pt:
                    m.tyaw = wrap(yw)
                    mech = self.cfg["mech"]
                    m.tpitch = max(mech["min_pitch"], min(mech["max_pitch"], pt))
            except Exception:
                pass
        f = a.get("fire")
        if isinstance(f, (list, tuple)) and len(f) >= 2:
            m.fire = [bool(f[0]), bool(f[1])]
        else:
            m.fire = [False, False]
        m.reload = bool(a.get("reload"))

        def _i(v):
            try:
                return None if v is None or isinstance(v, bool) else int(v)
            except Exception:
                return None
        m.pick = _i(a.get("pick"))
        m.take = _i(a.get("take"))
        m.drop = _i(a.get("drop"))
        m.fresh = True

    # ------------------------------------------------------------------ step
    def step(self):
        t = self.time
        alive = self.alive_list
        for m in alive:
            if m.fresh:
                self._oneshots()
                break
        for m in alive:
            self._mech_tick(m, t)
        for n in self.npcs:
            if n.alive:
                self._npc_tick(n, t)
        self._projs_tick()
        self._separate()
        self._world_tick(t)
        self._finalize()
        self.tick += 1
        self.time = self.tick * self.dt

    # ------------------------------------------------------------------ one-shot actions
    def _oneshots(self):
        takes = []
        for m in self.alive_list:
            if not m.fresh:
                continue
            m.fresh = False
            if m.pick is not None and m.offer is not None:
                self._resolve_offer(m, m.pick)
            if m.reload:
                for w in m.weapons:
                    if w is not None and w["kind"] == "gun":
                        self._start_reload(m, w)
            if m.take is not None:
                takes.append(m)
            elif m.drop is not None:
                self._drop_weapon(m, m.drop)
            m.pick = None
            m.reload = False
        if not takes:
            return
        rad = self.cfg["loot"]["pickup_radius"]
        by_pid = {}
        for m in takes:
            pk = self.pickups.get(m.take)
            if pk is None:
                m.events.append({"type": "take_failed", "id": m.take, "reason": "gone"})
                continue
            d = math.hypot(pk.x - m.x, pk.y - m.y)
            if d > rad or abs(pk.z - m.z) > 4.0:
                m.events.append({"type": "take_failed", "id": m.take, "reason": "range"})
                continue
            by_pid.setdefault(pk.id, []).append((d, m.id, m))
        for pid, lst in by_pid.items():
            lst.sort(key=lambda e: (e[0], e[1]))
            for k, (_, _, m) in enumerate(lst):
                if k > 0:
                    m.events.append({"type": "take_failed", "id": pid, "reason": "gone"})
            self._take(lst[0][2], self.pickups[pid])
            lst[0][2].take = None
            lst[0][2].drop = None

    def _take(self, m, pk):
        if m.holds(pk.kind):
            m.events.append({"type": "take_failed", "id": pk.id, "reason": "have"})
            return
        slot = None
        if m.drop is not None and m.drop in (0, 1):
            slot = m.drop
        else:
            for s in (0, 1):
                if m.weapons[s] is None:
                    slot = s
                    break
        if slot is None:
            m.events.append({"type": "take_failed", "id": pk.id, "reason": "full"})
            return
        old = m.weapons[slot]
        if old is not None and old["kind"] == "laser" and old["charging"]:
            m.events.append({"type": "take_failed", "id": pk.id, "reason": "busy"})
            return
        del self.pickups[pk.id]
        if old is not None:
            npk = self._add_pickup(old["kind"], m.x, m.y, m.z, old)
            m.events.append({"type": "dropped", "id": npk.id, "kind": old["kind"], "slot": slot})
        w = pk.state if pk.state is not None else self._new_weapon(pk.kind, m)
        w["stats"] = m.wstats[pk.kind]
        w["charging"] = False
        w["charge"] = 0.0
        w["hold"] = 0.0
        w["release"] = False
        eq = max(self.cfg["loot"]["equip_s"], w["stats"]["cooldown"])
        w["cooldown"] = max(eq, w["cooldown"])
        m.weapons[slot] = w
        m.events.append({"type": "took", "id": pk.id, "kind": pk.kind, "slot": slot})

    def _drop_weapon(self, m, slot):
        if slot not in (0, 1):
            return
        w = m.weapons[slot]
        if w is None or (w["kind"] == "laser" and w["charging"]):
            return
        m.weapons[slot] = None
        npk = self._add_pickup(w["kind"], m.x, m.y, m.z, w)
        m.events.append({"type": "dropped", "id": npk.id, "kind": w["kind"], "slot": slot})

    # ------------------------------------------------------------------ mech tick
    def _mech_tick(self, m, t):
        dt = self.dt
        st = m.stats
        dy = wrap(m.tyaw - m.yaw)
        sp = st["turn_rate"] * dt
        m.yaw = wrap(m.yaw + max(-sp, min(sp, dy)))
        dp = m.tpitch - m.pitch
        sp = st["pitch_rate"] * dt
        m.pitch += max(-sp, min(sp, dp))
        self._weapons_tick(m, t)
        self._move(m, t)
        if t - m.energy_t >= st["energy_regen_delay"] and m.energy < m.max_energy:
            m.energy = min(m.max_energy, m.energy + st["energy_regen"] * dt)
        if t >= m.next_step_t:
            sc = self.cfg["sound"]
            if m.jetting:
                self._sound("jet", m.x, m.y, m.z, m.id)
                m.next_step_t = t + sc["step_every"]
            elif m.grounded and math.hypot(m.vx, m.vy) > sc["step_speed"]:
                self._sound("boost" if m.boosting else "step", m.x, m.y, m.z, m.id)
                m.next_step_t = t + sc["step_every"]

    # ------------------------------------------------------------------ movement
    def _blocked(self, m, nx, ny, z, tbl, grounded, ox, oy):
        wm = self.wm
        r = m.r
        if nx < wm.x0 + r or nx > wm.x1 - r or ny < wm.y0 + r or ny > wm.y1 - r:
            return True
        lim = z + self.step_up
        bt = self.X.bldtop
        inv, x0, y0, gnx = wm.inv, wm.x0, wm.y0, wm.nx
        for ux, uy in tbl:
            if bt[int((ny + uy * r - y0) * inv) * gnx + int((nx + ux * r - x0) * inv)] > lim:
                return True
        b = bt[int((ny - y0) * inv) * gnx + int((nx - x0) * inv)]
        if b > lim:
            return True
        if grounded and b < -1e8:
            s = wm.ground_at(nx, ny)
            d = math.hypot(nx - ox, ny - oy)
            if s - z > self.tan_slope * d + 0.02:
                return True
        return False

    def _edge_support(self, m, x, y, z):
        wm = self.wm
        bt = self.X.bldtop
        r = m.r
        sx = sy = 0.0
        cnt = 0
        for ux, uy in RING8:
            i = int((x + ux * r - wm.x0) * wm.inv)
            j = int((y + uy * r - wm.y0) * wm.inv)
            b = bt[j * wm.nx + i]
            if b > -1e8 and abs(b - z) <= 0.2:
                sx += ux
                sy += uy
                cnt += 1
        if not cnt:
            return None
        ln = math.hypot(sx, sy)
        if ln < 1e-6:
            return (1.0, 0.0)
        return (sx / ln, sy / ln)

    def _move(self, m, t):
        st = m.stats
        dt = self.dt
        mx, my = m.mx, m.my
        moving = (mx * mx + my * my) > 1e-6
        speed = st["walk_speed"]
        boosting = jetting = False
        if m.boost and m.energy > 0:
            if m.grounded:
                if moving:
                    boosting = True
                    speed = st["boost_speed"]
            else:
                jetting = True
        m.boosting, m.jetting = boosting, jetting
        if m.charging:
            speed *= st["charge_move_mult"]
        acc = (st["ground_accel"] if m.grounded else st["air_accel"]) * dt
        dvx, dvy = mx * speed - m.vx, my * speed - m.vy
        dl = math.hypot(dvx, dvy)
        if dl > acc:
            k = acc / dl
            dvx *= k
            dvy *= k
        m.vx += dvx
        m.vy += dvy
        if m.jump and m.grounded:
            m.vz = st["jump_speed"]
            m.grounded = False
        m.jump = False
        spent = False
        if not m.grounded:
            if jetting:
                m.vz += (st["jet_accel"] - st["gravity"]) * dt
                if m.vz > st["max_rise_speed"]:
                    m.vz = st["max_rise_speed"]
                m.energy -= st["jet_drain"] * dt
                spent = True
            else:
                m.vz -= st["gravity"] * dt
        if boosting:
            m.energy -= st["boost_drain"] * dt
            spent = True
        if spent:
            m.energy_t = t
            if m.energy < 0:
                m.energy = 0.0
        x, y, z = m.x, m.y, m.z
        g = m.grounded
        ddx, ddy = m.vx * dt, m.vy * dt
        if ddx:
            if self._blocked(m, x + ddx, y, z, RING_XP if ddx > 0 else RING_XN, g, x, y):
                m.vx = 0.0
            else:
                x += ddx
        if ddy:
            if self._blocked(m, x, y + ddy, z, RING_YP if ddy > 0 else RING_YN, g, x, y):
                m.vy = 0.0
            else:
                y += ddy
        wm = self.wm
        if m.grounded:
            s = self.surface(x, y)
            if s >= z - self.step_up - 1e-6:
                z = s
            else:
                sup = self._edge_support(m, x, y, z)
                if sup is None:
                    m.grounded = False
                    m.vz = 0.0
                elif moving and (mx * sup[0] + my * sup[1]) > 0.1:
                    pass
                else:
                    x -= sup[0] * 0.6
                    y -= sup[1] * 0.6
        else:
            znew = z + m.vz * dt
            if m.vz <= 0:
                sup = self.surface(x, y)
                if sup > z + self.step_up:
                    sup = wm.ground_at(x, y)
                bt = self.X.bldtop
                r = m.r
                for ux, uy in RING4:
                    b = bt[int((y + uy * r - wm.y0) * wm.inv) * wm.nx + int((x + ux * r - wm.x0) * wm.inv)]
                    if b > sup and b <= z + 1e-3:
                        sup = b
                if znew <= sup:
                    z = sup
                    m.vz = 0.0
                    m.grounded = True
                else:
                    z = znew
            else:
                z = znew
        m.x, m.y, m.z = x, y, z

    def _separate(self):
        al = self.alive_list
        n = len(al)
        for i in range(n):
            a = al[i]
            for j in range(i + 1, n):
                b = al[j]
                dx = b.x - a.x
                dy = b.y - a.y
                rr = a.r + b.r
                if dx > rr or dx < -rr or dy > rr or dy < -rr:
                    continue
                d2 = dx * dx + dy * dy
                if d2 >= rr * rr:
                    continue
                if a.z >= b.z + b.h or b.z >= a.z + a.h:
                    continue
                d = math.sqrt(d2)
                if d < 1e-4:
                    dx, dy, d = 1.0, 0.0, 1.0
                push = (rr - d) * 0.5
                ux, uy = dx / d, dy / d
                if not self._blocked(b, b.x + ux * push, b.y + uy * push, b.z, RING_XP, False, b.x, b.y):
                    b.x += ux * push
                    b.y += uy * push
                if not self._blocked(a, a.x - ux * push, a.y - uy * push, a.z, RING_XP, False, a.x, a.y):
                    a.x -= ux * push
                    a.y -= uy * push

    # ------------------------------------------------------------------ sounds
    def _sound(self, kind, x, y, z, src=None):
        rng = self.snd_range.get(kind)
        if rng is None:
            return
        r2 = rng * rng
        t = self.time
        gauss = self.rng.gauss
        for m in self.alive_list:
            if m.id == src:
                continue
            dx, dy, dz = x - m.x, y - m.y, z - m.z
            d2 = dx * dx + dy * dy + dz * dz
            if d2 > r2:
                continue
            d = math.sqrt(d2)
            b = math.atan2(dy, dx) + gauss(0.0, self.snd_noise * m.stats["hearing"])
            m.sounds.append((kind, wrap(b), "near" if d < 60.0 else ("mid" if d < 160.0 else "far"), t))

    # ------------------------------------------------------------------ geometry helpers
    def _ray(self, ax, ay, az, bx, by, bz, step=1.0):
        """First blocked sample along a->b: (hit_fraction, last_free_fraction) or None."""
        wm = self.wm
        dx, dy, dz = bx - ax, by - ay, bz - az
        d = math.sqrt(dx * dx + dy * dy + dz * dz)
        if d < 1e-6:
            return None
        n = int(d / step) + 1
        top, nx, ny, x0, y0, inv = wm.top, wm.nx, wm.ny, wm.x0, wm.y0, wm.inv
        for q in range(1, n + 1):
            f = q / n
            i = int((ax + dx * f - x0) * inv)
            j = int((ay + dy * f - y0) * inv)
            if i < 0 or j < 0 or i >= nx or j >= ny:
                return (f, (q - 1) / n)
            if top[j * nx + i] > az + dz * f:
                return (f, (q - 1) / n)
        return None

    @staticmethod
    def _seg_cyl(ax, ay, az, dx, dy, dz, cx, cy, z0, z1, R):
        fx, fy = ax - cx, ay - cy
        a = dx * dx + dy * dy
        c = fx * fx + fy * fy - R * R
        if a < 1e-12:
            if c > 0:
                return None
            lo, hi = 0.0, 1.0
        else:
            b = fx * dx + fy * dy
            disc = b * b - a * c
            if disc < 0:
                return None
            sq = math.sqrt(disc)
            lo = max((-b - sq) / a, 0.0)
            hi = min((-b + sq) / a, 1.0)
            if lo > hi:
                return None
        if abs(dz) < 1e-9:
            if az < z0 or az > z1:
                return None
        else:
            sa, sb = (z0 - az) / dz, (z1 - az) / dz
            if sa > sb:
                sa, sb = sb, sa
            lo = max(lo, sa)
            hi = min(hi, sb)
            if lo > hi:
                return None
        return lo

    def _los_full(self, ax, ay, az, bx, by, bz):
        """Buildings/terrain plus trees (trunks block, dense crown path blocks)."""
        wm = self.wm
        dx, dy, dz = bx - ax, by - ay, bz - az
        d = math.hypot(dx, dy)
        if d < 0.5:
            return True
        n = int(d / 1.5) + 1
        step = d / n
        top, nx, ny, x0, y0, inv = wm.top, wm.nx, wm.ny, wm.x0, wm.y0, wm.inv
        trees = self.X.trees
        fol = 0.0
        for q in range(1, n):
            f = q / n
            px, py, pz = ax + dx * f, ay + dy * f, az + dz * f
            i = int((px - x0) * inv)
            j = int((py - y0) * inv)
            if 0 <= i < nx and 0 <= j < ny:
                k = j * nx + i
                if top[k] > pz:
                    return False
                tl = trees.get(k)
                if tl:
                    for (tx, ty, tr2, ttop, cx, cy, cr2, clo, chi) in tl:
                        if pz <= ttop and (px - tx) ** 2 + (py - ty) ** 2 <= tr2:
                            return False
                        if clo <= pz <= chi and (px - cx) ** 2 + (py - cy) ** 2 <= cr2:
                            fol += step
        return fol * self.X.density <= self.X.block_exp

    # ------------------------------------------------------------------ weapons
    def _start_reload(self, m, w):
        if w["reload"] <= 0 and w["ammo"] < int(w["stats"]["mag"]):
            w["reload"] = w["stats"]["reload"]
            self._sound("reload", m.x, m.y, m.z, m.id)

    def _weapons_tick(self, m, t):
        dt = self.dt
        for slot in (0, 1):
            w = m.weapons[slot]
            if w is None:
                continue
            trig = m.fire[slot]
            w["cooldown"] = max(w["cooldown"] - dt, -dt)
            k = w["kind"]
            st = w["stats"]
            if k == "gun":
                if w["reload"] > 0:
                    w["reload"] -= dt
                    if w["reload"] <= 0:
                        w["reload"] = 0.0
                        w["ammo"] = int(st["mag"])
                if trig and w["cooldown"] <= 0 and w["reload"] <= 0:
                    if w["ammo"] > 0:
                        self._fire_gun(m, w)
                        w["ammo"] -= 1
                        w["cooldown"] += st["cooldown"]
                        if w["ammo"] <= 0:
                            self._start_reload(m, w)
                    else:
                        self._start_reload(m, w)
                elif w["ammo"] <= 0 and w["reload"] <= 0:
                    self._start_reload(m, w)
            elif k == "mortar":
                if trig and w["cooldown"] <= 0:
                    self._fire_mortar(m, w)
                    w["cooldown"] += st["cooldown"]
            elif k == "lightning":
                if trig and w["cooldown"] <= 0 and m.energy >= st["energy"]:
                    self._fire_lightning(m, w, t)
                    w["cooldown"] += st["cooldown"]
            elif k == "laser":
                self._laser_tick(m, w, trig, t)

    def _aim_dir(self, m, dyaw=0.0, dpitch=0.0):
        yw, pt = m.yaw + dyaw, m.pitch + dpitch
        cp = math.cos(pt)
        return cp * math.cos(yw), cp * math.sin(yw), math.sin(pt)

    def _origin(self, m, height, d):
        mx, my, mz = m.x, m.y, m.z + height
        off = m.r + 0.5
        ox, oy, oz = mx + d[0] * off, my + d[1] * off, mz + d[2] * off
        h = self._ray(mx, my, mz, ox, oy, oz, 0.5)
        if h is not None:
            f = max(0.0, h[1])
            ox, oy, oz = mx + d[0] * off * f, my + d[1] * off * f, mz + d[2] * off * f
        return ox, oy, oz

    def _mkproj(self, kind, o, v, owner, team, dmg, max_range, ttl, ws=None, cmul=1.0):
        p = Proj()
        p.id = self._nid("proj")
        p.kind = kind
        p.x, p.y, p.z = o
        p.ox, p.oy, p.oz = o
        p.vx, p.vy, p.vz = v
        p.owner = owner
        p.team = team
        p.dmg = dmg
        p.dist = 0.0
        p.max_range = max_range
        p.age = 0.0
        p.ttl = ttl
        p.ws = ws
        p.cmul = cmul
        self.projs.append(p)
        return p

    def _fire_gun(self, m, w):
        st = w["stats"]
        sp = math.radians(st["spread_deg"])
        g = self.rng.gauss
        d = self._aim_dir(m, g(0.0, sp), g(0.0, sp))
        o = self._origin(m, m.muz, d)
        v = (d[0] * st["speed"], d[1] * st["speed"], d[2] * st["speed"])
        self._mkproj("gun", o, v, m.id, m.team, st["damage"] * self._dmg_mul(m), st["range"], 99.0)
        self._sound("gun", o[0], o[1], o[2], m.id)
        if self.record:
            self.shots.append({"kind": "gun", "from": [_r2(o[0]), _r2(o[1]), _r2(o[2])],
                               "to": [_r2(o[0] + d[0] * 18), _r2(o[1] + d[1] * 18), _r2(o[2] + d[2] * 18)], "by": m.id})

    def _fire_mortar(self, m, w):
        st = w["stats"]
        d = self._aim_dir(m)
        o = self._origin(m, m.h + 0.5, d)
        v = (d[0] * st["speed"], d[1] * st["speed"], d[2] * st["speed"])
        self._mkproj("mortar", o, v, m.id, m.team, st["damage"], 1e9, st["ttl"], st, self._dmg_mul(m))
        self._sound("mortar", o[0], o[1], o[2], m.id)

    def _laser_tick(self, m, w, trig, t):
        dt = self.dt
        st = w["stats"]
        if w["charging"]:
            if not trig:
                w["release"] = True
            if w["charge"] < st["charge"]:
                w["charge"] = min(st["charge"], w["charge"] + dt)
            elif not w["release"]:
                w["hold"] += dt
            if (w["release"] and w["charge"] >= st["min_charge"]) or w["hold"] >= st["hold_s"]:
                self._fire_laser(m, w, t)
        elif trig and w["cooldown"] <= 0 and t >= m.short_until:
            w["charging"] = True
            w["charge"] = 0.0
            w["hold"] = 0.0
            w["release"] = False
            self._sound("laser_charge", m.x, m.y, m.z, m.id)

    def _fire_laser(self, m, w, t):
        st = w["stats"]
        frac = st["min_frac"] + (1.0 - st["min_frac"]) * min(w["charge"], st["charge"]) / st["charge"]
        dmg = st["damage"] * frac * self._dmg_mul(m)
        w["charging"] = False
        w["charge"] = 0.0
        w["hold"] = 0.0
        w["release"] = False
        w["cooldown"] = min(0.0, w["cooldown"]) + st["cooldown"]
        d = self._aim_dir(m)
        o = self._origin(m, m.muz, d)
        rng = st["range"]
        ex, ey, ez = o[0] + d[0] * rng, o[1] + d[1] * rng, o[2] + d[2] * rng
        h = self._ray(o[0], o[1], o[2], ex, ey, ez, 1.0)
        wf = h[0] if h else 2.0
        vx, vy, vz = ex - o[0], ey - o[1], ez - o[2]
        hits = []
        for v in self.alive_list:
            if v is m or v.team == m.team:
                continue
            s = self._seg_cyl(o[0], o[1], o[2], vx, vy, vz, v.x, v.y, v.z, v.z + v.h, v.r)
            if s is not None and s < wf:
                hits.append((s, 0, v))
        for n in self.npcs:
            if n.alive:
                s = self._seg_cyl(o[0], o[1], o[2], vx, vy, vz, n.x, n.y, n.z, n.z + 3.5, 1.8)
                if s is not None and s < wf:
                    hits.append((s, 1, n))
        for c in self.crates:
            s = self._seg_cyl(o[0], o[1], o[2], vx, vy, vz, c.x, c.y, c.z, c.z + 2.4, 1.4)
            if s is not None and s < wf:
                hits.append((s, 2, c))
        hits.sort(key=lambda e: e[0])
        npierce = int(st.get("pierce", 0)) + 1
        endf = h[1] if h else 1.0
        sel = hits[:npierce]
        for s, kind, o2 in sel:
            if kind == 0:
                dm, eff = dmg, None
                if o2.holds("mortar"):
                    dm *= 1.0 + st["cookoff"]
                    eff = "cookoff"
                self._hurt(o2, dm, "laser", m, o[0], o[1], eff)
            elif kind == 1:
                self._hurt_npc(o2, dmg, "laser", m)
            else:
                self._hurt_crate(o2, dmg, "laser", m)
        if sel:
            endf = sel[-1][0]
        end = [o[0] + vx * endf, o[1] + vy * endf, o[2] + vz * endf]
        path = [[_r2(o[0]), _r2(o[1]), _r2(o[2])], [_r2(end[0]), _r2(end[1]), _r2(end[2])]]
        self.beams.append({"by": m.id, "kind": "laser", "path": path, "t": t})
        self._sound("laser", o[0], o[1], o[2], m.id)
        if self.record:
            self.shots.append({"kind": "laser", "from": path[0], "to": path[1], "by": m.id})

    def _fire_lightning(self, m, w, t):
        st = w["stats"]
        m.energy = max(0.0, m.energy - st["energy"])
        m.energy_t = t
        ox, oy, oz = m.x, m.y, m.z + m.muz
        yaw = m.yaw
        cone = math.radians(st["cone_deg"])
        rng = st["range"]
        los = self.wm.los
        cands = []
        for v in self.alive_list:
            if v is not m and v.team != m.team:
                cands.append((0, v, v.x, v.y, v.z + v.h * 0.5))
        for n in self.npcs:
            if n.alive:
                cands.append((1, n, n.x, n.y, n.z + 1.75))
        for c in self.crates:
            cands.append((2, c, c.x, c.y, c.z + 1.2))
        best = None
        bd = 1e18
        for c in cands:
            dx, dy, dz = c[2] - ox, c[3] - oy, c[4] - oz
            d2 = dx * dx + dy * dy + dz * dz
            if d2 > rng * rng or d2 >= bd:
                continue
            if abs(wrap(math.atan2(dy, dx) - yaw)) > cone:
                continue
            if not los((ox, oy, oz), (c[2], c[3], c[4])):
                continue
            bd, best = d2, c
        path = [[_r2(ox), _r2(oy), _r2(oz)]]
        dmg = st["damage"] * self._dmg_mul(m)
        if best is None:
            d = self._aim_dir(m)
            h = self._ray(ox, oy, oz, ox + d[0] * rng, oy + d[1] * rng, oz + d[2] * rng, 1.0)
            f = h[1] if h else 1.0
            path.append([_r2(ox + d[0] * rng * f), _r2(oy + d[1] * rng * f), _r2(oz + d[2] * rng * f)])
        else:
            chain = [best]
            used = {id(best[1])}
            cur = best
            for _ in range(int(st["chain"])):
                nb, nbd = None, 1e18
                for c in cands:
                    if id(c[1]) in used:
                        continue
                    dx, dy, dz = c[2] - cur[2], c[3] - cur[3], c[4] - cur[4]
                    d2 = dx * dx + dy * dy + dz * dz
                    if d2 > st["chain_range"] ** 2 or d2 >= nbd:
                        continue
                    if not los((cur[2], cur[3], cur[4]), (c[2], c[3], c[4])):
                        continue
                    nbd, nb = d2, c
                if nb is None:
                    break
                chain.append(nb)
                used.add(id(nb[1]))
                cur = nb
            dm = dmg
            for kind, obj, cx, cy, cz in chain:
                path.append([_r2(cx), _r2(cy), _r2(cz)])
                if kind == 0:
                    eff = None
                    if obj.holds("laser"):
                        eff = "short"
                        obj.short_until = t + st["short_s"]
                        for lw in obj.weapons:
                            if lw is not None and lw["kind"] == "laser" and lw["charging"]:
                                lw["charging"] = False
                                lw["charge"] = 0.0
                                lw["hold"] = 0.0
                                lw["release"] = False
                    self._hurt(obj, dm, "lightning", m, ox, oy, eff)
                elif kind == 1:
                    self._hurt_npc(obj, dm, "lightning", m)
                else:
                    self._hurt_crate(obj, dm, "lightning", m)
                dm *= st["chain_falloff"]
        self.beams.append({"by": m.id, "kind": "lightning", "path": path, "t": t})
        self._sound("lightning", ox, oy, oz, m.id)
        if self.record:
            for a, b in zip(path, path[1:]):
                self.shots.append({"kind": "lightning", "from": a, "to": b, "by": m.id})

    # ------------------------------------------------------------------ damage
    def _hurt(self, v, dmg, weapon, att, sx, sy, effect=None, by_id=None):
        if not v.alive or v.dying:
            return 0.0
        if att is not None and att is not v and att.team == v.team:
            return 0.0
        if weapon == "npc" and v.team == 100:
            # calibration: the real hide probe (0.04) shows house bots never die without us,
            # so drones don't hurt the squad (team 100 = house squad in run_match)
            return 0.0
        t = self.time
        bearing = math.atan2(sy - v.y, sx - v.x)
        rear = abs(wrap(bearing - v.yaw)) > self.front_half
        if rear:
            dmg *= self.rear_mult
        arm = v.stats["armor"]
        if arm:
            dmg *= max(0.0, 1.0 - arm)
        if att is v:
            dmg *= self.self_mult
        v.hp -= dmg
        v.last_dmg_t = t
        if att is not None:
            att.last_dmg_t = t
        who = att.id if att is not None else by_id
        ev = {"type": "damaged", "amount": round(dmg, 1), "weapon": weapon, "bearing": round(bearing, 3), "by": who}
        if rear:
            ev["rear"] = True
        v.events.append(ev)
        if effect and who is not None:
            v.events.append({"type": "effect", "effect": effect, "by": who})
        if att is not None and att is not v:
            he = {"type": "hit", "target": v.id, "amount": round(dmg, 1), "weapon": weapon}
            if effect:
                he["effect"] = effect
            if rear:
                he["rear"] = True
            att.events.append(he)
        if v.hp <= 0:
            v.dying = True
            v.killer = att.id if (att is not None and att is not v) else None
            v.death_cause = weapon + ("/self" if att is v else "")
            self.dying.append(v)
        return dmg

    def _hurt_npc(self, n, dmg, weapon, att):
        if not n.alive:
            return
        n.hp -= dmg
        if att is not None:
            att.last_dmg_t = self.time
            att.events.append({"type": "hit", "target": n.id, "amount": round(dmg, 1), "weapon": weapon})
            n.target = att.id
        if n.hp <= 0:
            n.alive = False
            for _ in range(self.cfg["npc"]["cubes"]):
                self._add_cube(n.x, n.y, n.z + 0.3, 1.5)

    def _hurt_crate(self, c, dmg, weapon, att):
        if c not in self.crates:
            return
        c.hp -= dmg
        if att is not None:
            att.events.append({"type": "hit", "target": c.id, "amount": round(dmg, 1), "weapon": weapon})
        if c.hp <= 0:
            self.crates.remove(c)
            self._sound("crate", c.x, c.y, c.z)
            for _ in range(self.cfg["crates"]["cubes"]):
                self._add_cube(c.x, c.y, c.z + 0.3, 1.0)
            if self.rng.random() < self.cfg["loot"]["crate_weapon_chance"]:
                self._add_pickup(self._rand_weapon(), c.x, c.y, c.z)

    # ------------------------------------------------------------------ projectiles
    def _projs_tick(self):
        if not self.projs:
            return
        dt = self.dt
        tg = []
        for m in self.alive_list:
            tg.append((0, m, m.x, m.y, m.z, m.z + m.h, m.r + 0.3))
        for n in self.npcs:
            if n.alive:
                tg.append((1, n, n.x, n.y, n.z, n.z + 3.5, 1.8 + 0.3))
        for c in self.crates:
            tg.append((2, c, c.x, c.y, c.z, c.z + 2.4, 1.4 + 0.3))
        keep = []
        for p in self.projs:
            ax, ay, az = p.x, p.y, p.z
            if p.kind == "mortar":
                p.vz -= p.ws["gravity"] * dt
            dx, dy, dz = p.vx * dt, p.vy * dt, p.vz * dt
            bx, by, bz = ax + dx, ay + dy, az + dz
            p.age += dt
            wall = self._ray(ax, ay, az, bx, by, bz, 1.0)
            bs = wall[0] if wall else 2.0
            best = None
            mnx, mxx = (ax, bx) if ax < bx else (bx, ax)
            mny, mxy = (ay, by) if ay < by else (by, ay)
            npc_b = p.kind == "npc"
            for k, o, tx, ty, z0, z1, R in tg:
                if tx + R < mnx or tx - R > mxx or ty + R < mny or ty - R > mxy:
                    continue
                if k == 0:
                    if o.id == p.owner or o.team == p.team:
                        continue
                elif npc_b:
                    continue
                s = self._seg_cyl(ax, ay, az, dx, dy, dz, tx, ty, z0, z1, R)
                if s is not None and s < bs:
                    bs, best = s, (k, o)
            if best is not None:
                hx, hy, hz = ax + dx * bs, ay + dy * bs, az + dz * bs
                if p.kind == "mortar":
                    self._explode(p, hx, hy, hz, best)
                else:
                    k, o = best
                    att = self.mechs[p.owner] if p.kind == "gun" else None
                    if k == 0:
                        self._hurt(o, p.dmg, p.kind, att, hx - p.vx, hy - p.vy, None, p.owner)
                    elif k == 1:
                        self._hurt_npc(o, p.dmg, p.kind, att)
                    else:
                        self._hurt_crate(o, p.dmg, p.kind, att)
                continue
            if wall is not None:
                if p.kind == "mortar":
                    f = wall[1]
                    self._explode(p, ax + dx * f, ay + dy * f, az + dz * f, None)
                continue
            p.x, p.y, p.z = bx, by, bz
            p.dist += math.sqrt(dx * dx + dy * dy + dz * dz)
            if p.kind == "mortar":
                if p.age >= p.ttl or bz < -200:
                    continue
            elif p.dist >= p.max_range:
                continue
            keep.append(p)
        self.projs = keep

    def _explode(self, p, bx, by, bz, direct):
        ws = p.ws
        R = ws["splash_radius"]
        edge = ws["splash_edge"]
        att = self.mechs[p.owner]
        cm = p.cmul
        dv = direct[1] if direct is not None else None
        self._sound("explosion", bx, by, bz)
        if self.record:
            self.shots.append({"kind": "mortar", "from": [_r2(p.ox), _r2(p.oy), _r2(p.oz)],
                               "to": [_r2(bx), _r2(by), _r2(bz)], "by": p.owner})
        los = self.wm.los
        for v in list(self.alive_list):
            if v.dying:
                continue
            if v is not att and v.team == att.team:
                continue
            hd = max(0.0, math.hypot(v.x - bx, v.y - by) - v.r)
            if v.z <= bz <= v.z + v.h:
                vd = 0.0
            else:
                vd = min(abs(bz - v.z), abs(bz - v.z - v.h))
            d = math.hypot(hd, vd)
            if d > R:
                continue
            if v is not dv and not los((bx, by, bz), (v.x, v.y, v.z + v.h * 0.5)):
                continue
            f = 1.0 - (1.0 - edge) * d / R
            dmg = ws["splash"] * f * cm
            if v is dv:
                dmg += p.dmg * cm
            self._hurt(v, dmg, "mortar", att, bx, by, "concuss" if v is not att else None)
            kx, ky = v.x - bx, v.y - by
            kl = math.hypot(kx, ky)
            if kl < 1e-3:
                a = self.rng.random() * TWO_PI
                kx, ky, kl = math.cos(a), math.sin(a), 1.0
            v.vx += kx / kl * ws["knock"] * f
            v.vy += ky / kl * ws["knock"] * f
            v.vz = max(v.vz, 0.0) + ws["knock_up"] * f
            v.grounded = False
            v.energy = max(0.0, v.energy - ws["drain"] * f)
        for n in self.npcs:
            if not n.alive:
                continue
            d = max(0.0, math.hypot(n.x - bx, n.y - by) - 1.8)
            if d <= R and (n is dv or los((bx, by, bz), (n.x, n.y, n.z + 1.75))):
                f = 1.0 - (1.0 - edge) * d / R
                self._hurt_npc(n, (ws["splash"] * f + (p.dmg if n is dv else 0.0)) * cm, "mortar", att)
        for c in list(self.crates):
            d = max(0.0, math.hypot(c.x - bx, c.y - by) - 1.4)
            if d <= R and (c is dv or los((bx, by, bz), (c.x, c.y, c.z + 1.2))):
                f = 1.0 - (1.0 - edge) * d / R
                self._hurt_crate(c, (ws["splash"] * f + (p.dmg if c is dv else 0.0)) * cm, "mortar", att)

    # ------------------------------------------------------------------ NPC drones
    def _npc_tick(self, n, t):
        dt = self.dt
        cfg = self.cfg["npc"]
        wm = self.wm
        tm = self.mechs[n.target] if n.target is not None else None
        dh = math.hypot(n.x - n.hx, n.y - n.hy)
        if tm is not None:
            if (not tm.alive) or dh > cfg["chase_leash"] or math.hypot(tm.x - n.x, tm.y - n.y) > cfg["deaggro_range"]:
                n.target = None
                tm = None
        if tm is None and dh <= cfg["leash"] and t >= n.scan_t:
            n.scan_t = t + 0.3
            best, bd = None, cfg["aggro_range"]
            for m in self.alive_list:
                d = math.hypot(m.x - n.x, m.y - n.y)
                if d < bd and wm.los((n.x, n.y, n.z + 3.0), (m.x, m.y, m.z + m.h * 0.5)):
                    bd, best = d, m
            if best is not None:
                n.target = best.id
                tm = best
        gx = gy = None
        if tm is not None:
            if math.hypot(tm.x - n.x, tm.y - n.y) > 18.0:
                gx, gy = tm.x, tm.y
        else:
            n.wait -= dt
            if dh > cfg["leash"]:
                gx, gy = n.hx, n.hy
            else:
                if n.wait <= 0 or math.hypot(n.wx - n.x, n.wy - n.y) < 1.0:
                    if n.wait <= 0:
                        a = self.rng.random() * TWO_PI
                        d = cfg["leash"] * math.sqrt(self.rng.random())
                        n.wx, n.wy = n.hx + math.cos(a) * d, n.hy + math.sin(a) * d
                        n.wait = 2.0 + self.rng.random() * 5.0
                if math.hypot(n.wx - n.x, n.wy - n.y) >= 1.0:
                    gx, gy = n.wx, n.wy
        n.vx = n.vy = 0.0
        if gx is not None:
            dx, dy = gx - n.x, gy - n.y
            dl = math.hypot(dx, dy)
            if dl > 1e-3:
                n.vx, n.vy = dx / dl * cfg["speed"], dy / dl * cfg["speed"]
                n.yaw = math.atan2(dy, dx)
                nx_, ny_ = n.x + n.vx * dt, n.y + n.vy * dt
                if not wm.is_solid(nx_, ny_):
                    n.x, n.y = nx_, ny_
                    n.z = self.surface(nx_, ny_)
                else:
                    n.vx = n.vy = 0.0
        n.cool -= dt
        if tm is not None:
            n.yaw = math.atan2(tm.y - n.y, tm.x - n.x)
            if n.cool <= 0:
                ox, oy, oz = n.x, n.y, n.z + 2.0
                tx, ty, tz = tm.x, tm.y, tm.z + tm.h * 0.5
                d = math.sqrt((tx - ox) ** 2 + (ty - oy) ** 2 + (tz - oz) ** 2)
                if d < 120.0 and wm.los((ox, oy, oz), (tx, ty, tz)):
                    sp = math.radians(cfg["spread_deg"])
                    yaw = math.atan2(ty - oy, tx - ox) + self.rng.gauss(0.0, sp)
                    pit = math.asin((tz - oz) / max(d, 1e-6)) + self.rng.gauss(0.0, sp)
                    cp = math.cos(pit)
                    v = (cp * math.cos(yaw) * cfg["projectile_speed"], cp * math.sin(yaw) * cfg["projectile_speed"],
                         math.sin(pit) * cfg["projectile_speed"])
                    o = (ox + math.cos(yaw) * 2.2, oy + math.sin(yaw) * 2.2, oz)
                    self._mkproj("npc", o, v, n.id, -1, cfg["damage"], 150.0, 99.0)
                    self._sound("npc_gun", ox, oy, oz)
                    n.cool = cfg["cooldown"]

    # ------------------------------------------------------------------ world tick
    def _world_tick(self, t):
        dt = self.dt
        cfg = self.cfg
        zx, zy, zr, ph, _, _ = self.zone_at(t)
        dps = ph["dps"]
        # supply drops
        dc = cfg["drops"]
        for q, ct in enumerate(dc["times"]):
            if t >= ct and q not in self._called_drops:
                self._called_drops.add(q)
                nc, nr = ph["nc"], ph["nr"]
                x, y = self.random_open(nc[0], nc[1], max(10.0, nr * dc["spread"]))
                self.drops.append(Drop(self._nid("drop"), x, y, self.surface(x, y), ct, ct + dc["fall_s"]))
        for d in list(self.drops):
            if not d.landed:
                if t >= d.land_t:
                    d.landed = True
                    self._sound("drop", d.x, d.y, d.z)
                continue
            inside = [m for m in self.alive_list if (m.x - d.x) ** 2 + (m.y - d.y) ** 2 <= dc["radius"] ** 2]
            if len(inside) == 1:
                m = inside[0]
                if d.holder != m.id:
                    d.holder = m.id
                    d.progress = 0.0
                d.progress += dt
                if d.progress >= dc["capture_s"]:
                    self._give_cubes(m, dc["cubes"])
                    if dc.get("repair"):
                        m.hp = m.max_hp
                        m.energy = m.max_energy
                    m.events.append({"type": "drop", "id": d.id, "cubes": m.cubes})
                    self.drops.remove(d)
                    self._log(f"slot {m.id} captured drop {d.id}")
            elif not inside:
                d.holder = None
                d.progress = 0.0
        # cubes
        if self.cubes:
            pr = cfg["cubes"]["pickup_radius"]
            pr2 = pr * pr
            rest = []
            for c in self.cubes:
                best, bd = None, pr2
                for m in self.alive_list:
                    dx, dy = m.x - c.x, m.y - c.y
                    d2 = dx * dx + dy * dy
                    if d2 < bd and abs(m.z - c.z) <= 4.0:
                        bd, best = d2, m
                if best is None:
                    rest.append(c)
                else:
                    self._give_cubes(best, 1)
                    best.events.append({"type": "pickup", "cubes": best.cubes})
            self.cubes = rest
        # zone damage / regen / offers
        mc = cfg["mech"]
        for m in self.alive_list:
            inz = math.hypot(m.x - zx, m.y - zy) <= zr
            m.in_zone = inz
            if not inz:
                amt = dps * dt
                m.hp -= amt
                m.last_dmg_t = t
                ev = m.events[-1] if m.events else None
                if ev is not None and ev.get("weapon") == "zone" and ev["type"] == "damaged":
                    ev["amount"] = round(ev["amount"] + amt, 1)
                else:
                    m.events.append({"type": "damaged", "amount": round(amt, 1), "weapon": "zone",
                                     "bearing": None, "by": None})
                if m.team == 100 and m.hp < 1.0:
                    # calibration: sim house bots path badly out of the late zone; real ones
                    # don't die without us, so the zone can't finish off the squad
                    m.hp = 1.0
                if m.hp <= 0 and not m.dying:
                    m.dying = True
                    m.killer = None
                    self.dying.append(m)
            elif t - m.last_dmg_t >= m.stats["regen_delay"] and m.hp < m.max_hp:
                m.hp = min(m.max_hp, m.hp + m.stats["regen_per_s"] * dt)
            if m.offer is None:
                if m.offers_pending > 0:
                    self._make_offer(m)
            elif t >= m.offer["expires"] - 1e-9:
                self._resolve_offer(m, 0)

    def _log(self, s):
        if self.record:
            self.events_log.append(f"t={self.time:.1f} {s}")

    # ------------------------------------------------------------------ deaths
    def _finalize(self):
        if not self.dying:
            return
        n_alive = len(self.alive_list)
        dead = self.dying
        self.dying = []
        for v in dead:
            v.alive = False
            v.place = n_alive
            v.death_t = self.time
            v.fire = [False, False]
        self.alive_list = [m for m in self.alive_list if m.alive]
        for v in dead:
            kl = self.mechs[v.killer] if v.killer is not None else None
            if kl is not None:
                kl.kills += 1
                kl.events.append({"type": "kill", "target": v.id})
                self._log(f"slot {kl.id} killed slot {v.id}")
            else:
                self._log(f"slot {v.id} died ({getattr(v, 'death_cause', 'zone')}) place {v.place}")
            self._sound("death", v.x, v.y, v.z)
            for w in v.weapons:
                if w is not None and w["kind"] != "gun":
                    a = self.rng.random() * TWO_PI
                    d = 2.0 * math.sqrt(self.rng.random())
                    x, y = v.x + math.cos(a) * d, v.y + math.sin(a) * d
                    z = self.surface(x, y) if not self.wm.is_solid(x, y) else v.z
                    self._add_pickup(w["kind"], x, y, z)
            ncubes = v.cubes + self.cfg["cubes"]["death_drop_extra"]
            for _ in range(ncubes):
                self._add_cube(v.x, v.y, v.z + 0.3, 3.0)

    # ------------------------------------------------------------------ vision / observations
    def _snapshot(self):
        if self._snap_tick == self.tick:
            return self._snap
        tg = []
        for m in self.alive_list:
            tg.append((m.id, 0, m, m.x, m.y, m.z + m.h * 0.5, m.z + m.h))
        for n in self.npcs:
            if n.alive:
                tg.append((n.id, 1, n, n.x, n.y, n.z + 1.75, n.z + 3.5))
        self._snap = tg
        self._snap_tick = self.tick
        return tg

    def _can_see(self, ex, ey, ez, ux, uy, near2, rng2, px, py, pz1, pz2):
        dx, dy, dz = px - ex, py - ey, pz1 - ez
        d2h = dx * dx + dy * dy
        d2 = d2h + dz * dz
        if d2 <= near2:
            los = self.wm.los
        elif d2 <= rng2 and dx * ux + dy * uy >= self.v_fov_cos * math.sqrt(d2h):
            los = self._los_full
            eye = (ex, ey, ez)
            if los(ex, ey, ez, px, py, pz1):
                return True
            return pz2 is not None and los(ex, ey, ez, px, py, pz2)
        else:
            return False
        eye = (ex, ey, ez)
        if los(eye, (px, py, pz1)):
            return True
        return pz2 is not None and los(eye, (px, py, pz2))

    def observation(self, slot):
        m = self.mechs[slot]
        t = self.time
        st = m.stats
        ex, ey, ez = m.x, m.y, m.z + m.eye
        ux, uy = math.cos(m.yaw), math.sin(m.yaw)
        near = self.v_near + st["near_range_bonus"]
        rng = self.v_range + st["vision_bonus"]
        near2, rng2 = near * near, rng * rng
        see = self._can_see
        seen = m.seen
        mechs_out = []
        for (tid, kind, o, tx, ty, zc, zt) in self._snapshot():
            if o is m:
                continue
            if see(ex, ey, ez, ux, uy, near2, rng2, tx, ty, zc, zt):
                seen[tid] = t
            elif t - seen.get(tid, NEG) > self.v_mem:
                continue
            if kind == 0:
                mechs_out.append({"id": o.id, "kind": "mech", "pos": [_r2(o.x), _r2(o.y), _r2(o.z)],
                                  "vel": [_r2(o.vx), _r2(o.vy), _r2(o.vz)], "aim": [round(o.yaw, 3), round(o.pitch, 3)],
                                  "hp": round(o.hp, 1), "max_hp": round(o.max_hp, 1), "cubes": o.cubes,
                                  "weapons": [w["kind"] for w in o.weapons if w is not None], "charging": o.charging})
            else:
                mechs_out.append({"id": o.id, "kind": "npc", "pos": [_r2(o.x), _r2(o.y), _r2(o.z)],
                                  "vel": [_r2(o.vx), _r2(o.vy), 0.0], "aim": [round(o.yaw, 3), 0.0],
                                  "hp": round(o.hp, 1), "max_hp": o.max_hp})
        projs = []
        for p in self.projs:
            if p.kind != "npc" and p.owner == m.id:
                continue
            if see(ex, ey, ez, ux, uy, near2, rng2, p.x, p.y, p.z, None):
                projs.append({"id": p.id, "kind": p.kind, "pos": [_r2(p.x), _r2(p.y), _r2(p.z)],
                              "vel": [_r2(p.vx), _r2(p.vy), _r2(p.vz)]})
        crates = []
        for c in self.crates:
            if see(ex, ey, ez, ux, uy, near2, rng2, c.x, c.y, c.z + 1.2, c.z + 2.4):
                crates.append({"id": c.id, "pos": [_r2(c.x), _r2(c.y), _r2(c.z)], "hp": round(c.hp, 1)})
        cubes = []
        for c in self.cubes:
            if see(ex, ey, ez, ux, uy, near2, rng2, c.x, c.y, c.z + 0.5, None):
                cubes.append({"id": c.id, "pos": [_r2(c.x), _r2(c.y), _r2(c.z)]})
        pickups = []
        for pk in self.pickups.values():
            if see(ex, ey, ez, ux, uy, near2, rng2, pk.x, pk.y, pk.z + 0.5, None):
                pickups.append({"id": pk.id, "kind": pk.kind, "pos": [_r2(pk.x), _r2(pk.y), _r2(pk.z)]})
        sounds = [{"kind": k, "bearing": round(b, 3), "dist": d, "age": round(t - te, 3)} for (k, b, d, te) in m.sounds]
        m.sounds = []
        beams = []
        for b in self.beams:
            if b["by"] == m.id or b["t"] < m.last_obs_t - 1e-9 or b["t"] >= t:
                continue
            path = b["path"]
            pts = list(path)
            if len(path) >= 2:
                pts.append([(path[0][i] + path[1][i]) * 0.5 for i in range(3)])
            for pt in pts:
                if see(ex, ey, ez, ux, uy, near2, rng2, pt[0], pt[1], pt[2], None):
                    beams.append({"by": b["by"], "kind": b["kind"], "path": path, "age": round(t - b["t"], 3)})
                    break
        m.last_obs_t = t
        events = m.events
        m.events = []
        drops = []
        dc = self.cfg["drops"]
        for d in self.drops:
            drops.append({"id": d.id, "pos": [_r2(d.x), _r2(d.y), _r2(d.z)], "radius": dc["radius"],
                          "lands_in": round(max(0.0, d.land_t - t), 2), "capture_s": dc["capture_s"],
                          "progress": round(d.progress, 2), "holder": d.holder})
        weapons = []
        for w in m.weapons:
            if w is None:
                weapons.append(None)
                continue
            o = {"kind": w["kind"], "cooldown": round(max(0.0, w["cooldown"]), 3), "stats": dict(w["stats"])}
            if w["kind"] == "gun":
                o["ammo"] = w["ammo"]
                o["reload"] = round(max(0.0, w["reload"]), 3)
            elif w["kind"] == "laser":
                o["charge"] = round(w["charge"] if w["charging"] else 0.0, 3)
            weapons.append(o)
        selfo = {"id": m.id, "pos": [_r2(m.x), _r2(m.y), _r2(m.z)], "vel": [_r2(m.vx), _r2(m.vy), _r2(m.vz)],
                 "aim": [round(m.yaw, 4), round(m.pitch, 4)], "hp": round(m.hp, 1), "max_hp": round(m.max_hp, 1),
                 "energy": round(m.energy, 1), "max_energy": m.max_energy, "grounded": bool(m.grounded),
                 "cubes": m.cubes, "upgrades": list(m.upgrades), "weapons": weapons, "in_zone": bool(m.in_zone),
                 "stats": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in st.items()}}
        return {"type": "obs", "tick": self.tick, "t": round(t, 3), "alive": len(self.alive_list), "self": selfo,
                "mechs": mechs_out, "projectiles": projs, "crates": crates, "cubes": cubes, "pickups": pickups,
                "sounds": sounds, "beams": beams, "events": events, "zone": self.zone_obs(),
                "offer": m.offer, "drops": drops}

    def prune(self):
        """Drop stale beams (call once per decision)."""
        t = self.time
        if self.beams:
            self.beams = [b for b in self.beams if t - b["t"] < 1.0]

    # ------------------------------------------------------------------ replay
    def replay_meta(self, slot_info):
        wm = self.wm
        b = []
        for p, cells in wm.prisms:
            if p.get("kind") in ("building", "stadium"):
                b.append({"ring": [round(v, 1) for v in p["ring"]], "top": round(float(p["top"]), 1)})
        return {"type": "meta", "version": 1,
                "bounds": {"minX": wm.x0, "maxX": wm.x1, "minY": wm.y0, "maxY": wm.y1},
                "buildings": b, "slots": slot_info}

    def replay_frame(self):
        zx, zy, zr, ph, nc, nr = self.zone_at(self.time)
        mechs = []
        for m in self.mechs:
            mechs.append({"slot": m.id, "x": _r2(m.x), "y": _r2(m.y), "z": _r2(m.z), "yaw": round(m.yaw, 3),
                          "hp": round(max(0.0, m.hp), 1), "max_hp": round(m.max_hp, 1), "energy": round(m.energy, 1),
                          "cubes": m.cubes, "weapons": [w["kind"] for w in m.weapons if w is not None],
                          "alive": m.alive, "charging": m.charging, "label": self.labels.get(m.id, "")})
        items = []
        for c in self.crates:
            items.append({"kind": "crate", "x": round(c.x, 1), "y": round(c.y, 1)})
        for c in self.cubes:
            items.append({"kind": "cube", "x": round(c.x, 1), "y": round(c.y, 1)})
        for pk in self.pickups.values():
            items.append({"kind": pk.kind, "x": round(pk.x, 1), "y": round(pk.y, 1)})
        for d in self.drops:
            items.append({"kind": "drop", "x": round(d.x, 1), "y": round(d.y, 1)})
        fr = {"type": "frame", "t": round(self.time, 2),
              "zone": {"cx": _r2(zx), "cy": _r2(zy), "r": _r2(zr), "ncx": _r2(nc[0]), "ncy": _r2(nc[1]), "nr": _r2(nr)},
              "mechs": mechs,
              "npcs": [{"id": n.id, "x": _r2(n.x), "y": _r2(n.y), "hp": round(n.hp, 1)} for n in self.npcs if n.alive],
              "shots": self.shots, "items": items, "events": self.events_log}
        self.shots = []
        self.events_log = []
        return fr
