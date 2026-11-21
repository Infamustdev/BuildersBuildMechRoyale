"""House bots ("opponent squad") for the offline Mech Royale simulator (protocol mechfight/1).

`Squad` drives all 15 house bots in-process. One squad blackboard merges every member's sightings
of the prey (our bot), remembered positions, loot, drops and sound hints. A hunt schedule releases
hunters weakest-first (easy -> expert); everybody else loots, stays in the zone and avoids the prey
(but defends itself if the prey comes close). Squad members are never targeted.

Interface: see sim/SPEC.md section 2.
    Squad(cfg, wm, member_slots, prey_slots, difficulty, seed)
    Squad.act_all(obs_by_slot, t) -> {slot: action}

Standard library only. Only worldmap.WorldMap (passed in) is used for map queries.
"""
import math
import os
import random
import time

# --------------------------------------------------------------------------------------- knobs
# aim_err_deg  : std-dev of the (slowly varying) aim error added to every solution
# react_s      : delay between acquiring a target and the first shot
# lead         : 0..1 fraction of the target velocity used for leading gun / mortar shots
# range        : preferred engagement distance (m); loadout can override it (see _pref_range)
# cover        : hides from the target (breaks line of sight) while reloading / laser cooling down
# strafe       : circle-strafes at range, jumps now and then
# laser        : probability that the bot uses a laser when it has one (fixed per bot)
# mortar       : probability that the bot uses a mortar when it has one
# retreat_hp   : retreat to cover below this HP fraction (0 = never)
# flank        : spreads hunters around the target (0 = all come straight at it)
# slew         : fraction of the torso turn rate the bot commands (sluggish aim for weak bots)
# fire_range   : never opens fire with the gun beyond this distance
# dodge        : sidesteps/jumps when the prey is charging a laser at it
# counter      : picks its range by weapon triangle (lightning vs laser, etc.)
# speed        : fraction of walk speed used
# boost        : boosts on long travel (not only when the zone is closing in)
# blind        : lobs mortar shells at remembered positions it cannot see
DIFFICULTY = {
    "easy":   dict(aim_err_deg=7.0, react_s=0.9, lead=0.15, range=55.0, cover=False, strafe=False,
                   laser=0.0, mortar=0.2, retreat_hp=0.0, flank=0.0, slew=0.5, fire_range=100.0,
                   dodge=False, counter=False, speed=0.8, boost=False, blind=False),
    "medium": dict(aim_err_deg=3.0, react_s=0.45, lead=0.55, range=75.0, cover=False, strafe=True,
                   laser=0.4, mortar=0.4, retreat_hp=0.2, flank=0.0, slew=0.8, fire_range=160.0,
                   dodge=False, counter=False, speed=0.95, boost=False, blind=False),
    "hard":   dict(aim_err_deg=1.2, react_s=0.22, lead=0.85, range=95.0, cover=True, strafe=True,
                   laser=0.75, mortar=0.6, retreat_hp=0.3, flank=0.6, slew=1.0, fire_range=210.0,
                   dodge=True, counter=True, speed=1.0, boost=True, blind=False),
    "expert": dict(aim_err_deg=0.35, react_s=0.1, lead=1.0, range=120.0, cover=True, strafe=True,
                   laser=1.0, mortar=0.8, retreat_hp=0.4, flank=1.0, slew=1.0, fire_range=225.0,
                   dodge=True, counter=True, speed=1.0, boost=True, blind=True),
}
RANK = {"easy": 0, "medium": 1, "hard": 2, "expert": 3}

# Calibration knob: multiplies every level's aim error. Tuned so our exp-4 bot's sim score
# matches its real practice score (2.88). Override with the HB_AIM_SCALE env var.
AIM_SCALE = float(os.environ.get("HB_AIM_SCALE", "1.6"))

# Hunt schedule. hunters = initial + per_loss * (squad members lost) + per_step * int(t / time_step_s);
# everybody hunts once `all_after_phase` zone phases have completed (default 3 -> after ~265 s).
HUNT = dict(initial=1, per_loss=1, per_step=1, time_step_s=55.0, all_after_phase=3)

# Weapon values for looting; laser > lightning > mortar (a mortar is cooked off by lasers).
WEAPON_VALUE = {"laser": 3.0, "lightning": 2.4, "mortar": 2.0, "gun": 1.0}

# Upgrade scores (higher = better) for medium and up.
UPGRADE_SCORE = {
    "armor": 9, "plating": 8.5, "laser_damage": 8.2, "gun_damage": 7.5, "nanites": 7, "gyros": 6.8,
    "laser_charge": 6.5, "gun_rate": 6.2, "mortar_splash": 5.5, "lightning_efficiency": 5.4,
    "gun_mag": 5.3, "servos": 5.2, "capacitor": 5, "mortar_reload": 5, "gun_velocity": 4.8,
    "lightning_chain": 4.5, "reactor": 4.4, "sensors": 4.2, "laser_pierce": 4, "jets": 3.8,
    "mortar_velocity": 3.5, "lightning_range": 3.5,
}

TAU = 2 * math.pi
PI = math.pi


def angdiff(a, b):
    return (a - b + PI) % TAU - PI


class _Bot:
    def __init__(self, slot, diff, rng):
        self.slot = slot
        self.diff = diff
        self.K = DIFFICULTY[diff]
        self.rng = rng
        K = self.K
        self.laser_pref = rng.random() < K["laser"]
        self.mortar_pref = rng.random() < K["mortar"]
        self.rjit = rng.uniform(0.9, 1.12)
        self.wvals = dict(WEAPON_VALUE)
        if diff == "easy":  # easy bots grab whatever and value it randomly
            for k in ("laser", "lightning", "mortar"):
                self.wvals[k] = rng.uniform(1.5, 3.0)
        self.ny = self.np = 0.0
        self.path = None
        self.wp = 0
        self.path_goal = (1e9, 1e9)
        self.path_t = -99.0
        self.strafe = rng.choice((-1, 1))
        self.strafe_until = 0.0
        self.tgt_id = None
        self.acq_t = None
        self.last_vis_t = -99.0
        self.hide = None
        self.hide_t = -99.0
        self.retreat_until = 0.0
        self.roam = None
        self.roam_t = -99.0
        self.loot = None
        self.loot_t = -99.0
        self.pos_hist = []
        self.stuck_until = 0.0
        self.boosting = False
        self.blacklist = {}
        self.short_until = 0.0
        self.last_dmg_t = -99.0
        self.dodge_until = 0.0
        self.flank_ang = 0.0
        self.last_los = False
        self.last_los_t = -99.0
        self.last_cmd = None


class Squad:
    def __init__(self, cfg, wm, member_slots, prey_slots, difficulty, seed):
        self.cfg = cfg
        self.wm = wm
        self.members = list(member_slots)
        self.prey = set(prey_slots)
        self.rng = random.Random(seed)
        self.bots = {}
        for s in self.members:
            self.bots[s] = _Bot(s, difficulty.get(s, "medium"), random.Random(self.rng.random()))
        self.W = cfg.get("weapons", {})
        self.M = cfg.get("mech", {})
        self.size_per_cube = cfg.get("cubes", {}).get("size_per_cube", 0.05)
        zc = cfg.get("zone", {})
        self.zone_ends = []
        acc = 0.0
        for ph in zc.get("phases", []):
            acc += ph.get("hold_s", 0) + ph.get("shrink_s", 0)
            self.zone_ends.append(acc)
        self.hunt = dict(HUNT)
        self.prev_live = set(self.members)
        self.losses = 0
        self.bb = {}            # prey id -> sighting
        self.items = {}         # id -> (typ, x, y, z, kind, t_seen)
        self.claims = {}        # id -> (slot, expires)
        self.drops = []
        self.hints = []         # (x, y, t)
        self.mpos = {}
        self.hunters = set()
        self.n_hunt = 0
        self.tick_n = 0
        self.time_total = 0.0
        self.calls = 0
        self.raise_errors = False
        self._replans = 0

    # ---------------------------------------------------------------- squad level
    def phase_done(self, t):
        return sum(1 for e in self.zone_ends if t >= e)

    def hunt_count(self, t):
        H = self.hunt
        if self.phase_done(t) >= H["all_after_phase"]:
            return 99
        return H["initial"] + H["per_loss"] * self.losses + H["per_step"] * int(t / H["time_step_s"])

    def act_all(self, obs_by_slot, t):
        t0 = time.perf_counter()
        self._replans = 0
        self.tick_n += 1
        live = set(s for s in obs_by_slot if s in self.bots)
        for s in self.prev_live - live:
            self.losses += 1
        self.prev_live = live
        self._update_bb(obs_by_slot, t)
        n = self.hunt_count(t)
        self.n_hunt = n
        order = sorted(live, key=lambda s: (RANK[self.bots[s].diff], s))
        self.hunters = set(order[:n])
        hidx = {s: i for i, s in enumerate(order[:n])}
        out = {}
        for s in live:
            obs = obs_by_slot[s]
            try:
                out[s] = self._act(self.bots[s], obs, t, s in self.hunters, hidx.get(s, 0))
            except Exception:
                if self.raise_errors:
                    raise
                out[s] = {"tick": obs.get("tick", 0)}
        self.time_total += time.perf_counter() - t0
        self.calls += 1
        return out

    def avg_ms(self):
        return 1000.0 * self.time_total / max(1, self.calls)

    def _update_bb(self, obs_by_slot, t):
        prey, bb, items = self.prey, self.bb, self.items
        mpos = self.mpos
        tick_n = self.tick_n
        for s, obs in obs_by_slot.items():
            if s not in self.bots:
                continue
            me = obs["self"]
            mpos[s] = me["pos"]
            for m in obs.get("mechs", ()):
                i = m["id"]
                if i in prey and m.get("kind", "mech") == "mech":
                    bb[i] = dict(pos=m["pos"], vel=m.get("vel") or [0, 0, 0], hp=m.get("hp", 1400),
                                 max_hp=m.get("max_hp", 1400), cubes=m.get("cubes", 0),
                                 weapons=m.get("weapons") or [], aim=m.get("aim") or [0, 0],
                                 charging=bool(m.get("charging")), t=t, by=s)
            seen = set()
            for p in obs.get("pickups", ()):
                seen.add(p["id"])
                items[p["id"]] = ("pick", p["pos"][0], p["pos"][1], p["pos"][2], p["kind"], t)
            for c in obs.get("cubes", ()):
                seen.add(c["id"])
                items[c["id"]] = ("cube", c["pos"][0], c["pos"][1], c["pos"][2], "cube", t)
            for c in obs.get("crates", ()):
                seen.add(c["id"])
                items[c["id"]] = ("crate", c["pos"][0], c["pos"][1], c["pos"][2], "crate", t)
            if (tick_n + s) % 3 == 0:
                px, py = me["pos"][0], me["pos"][1]
                gone = [i for i, it in items.items()
                        if i not in seen and abs(it[1] - px) < 38 and abs(it[2] - py) < 38
                        and (it[1] - px) ** 2 + (it[2] - py) ** 2 < 1300 and it[5] < t - 0.05]
                for i in gone:
                    del items[i]
            for snd in obs.get("sounds", ()):
                k = snd.get("kind")
                if k in ("gun", "laser", "laser_charge", "lightning", "mortar", "step", "boost"):
                    d = {"near": 35.0, "mid": 100.0}.get(snd.get("dist"), 200.0)
                    a = snd.get("bearing", 0.0)
                    hx = me["pos"][0] + math.cos(a) * d
                    hy = me["pos"][1] + math.sin(a) * d
                    self.hints.append((hx, hy, t, s))
            if obs.get("drops"):
                self.drops = obs["drops"]
        if len(self.hints) > 24:
            self.hints = self.hints[-24:]
        # forget stale items (ttl) and old claims
        if tick_n % 50 == 0:
            for i in [i for i, it in items.items() if it[5] < t - 90]:
                del items[i]
            for i in [i for i, c in self.claims.items() if c[1] < t]:
                del self.claims[i]

    def _hint_for(self, s, t):
        best = None
        for hx, hy, ht, hs in reversed(self.hints):
            if t - ht > 8.0:
                break
            if hs == s:
                continue
            near_mate = False
            for ms, mp in self.mpos.items():
                if (mp[0] - hx) ** 2 + (mp[1] - hy) ** 2 < 2500:
                    near_mate = True
                    break
            if not near_mate:
                best = (hx, hy)
                break
        return best

    # ---------------------------------------------------------------- zone helpers
    def _safe_circle(self, z, t):
        cx, cy = z["center"]
        r = z["radius"]
        nc = z.get("next_center") or (cx, cy)
        nr = z.get("next_radius", r)
        if t >= z.get("shrink_start", 1e9) - 12.0:
            return nc[0], nc[1], max(nr - 14.0, 6.0), cx, cy, r
        return cx, cy, max(r - 18.0, 6.0), cx, cy, r

    @staticmethod
    def _clamp_goal(gx, gy, cx, cy, R):
        dx, dy = gx - cx, gy - cy
        d = math.hypot(dx, dy)
        if d > R:
            return cx + dx * R / d, cy + dy * R / d
        return gx, gy

    # ---------------------------------------------------------------- navigation
    def _goto(self, b, x, y, gx, gy, t):
        """Unit-ish direction to walk from (x,y) toward (gx,gy), using cached paths."""
        wm = self.wm
        d = math.hypot(gx - x, gy - y)
        if d < 1.5:
            return 0.0, 0.0
        if d <= 30.0 and wm.walk_clear(x, y, gx, gy):
            b.path = None
            return (gx - x) / d, (gy - y) / d
        need = (b.path is None or math.hypot(gx - b.path_goal[0], gy - b.path_goal[1]) > 18.0
                or t - b.path_t > 9.0 or t < b.stuck_until - 0.6)
        if need and (self._replans < 1 or b.path is None) and t - b.path_t > 1.2:
            self._replans += 1
            b.path = wm.path(x, y, gx, gy, budget_ms=2.5)
            b.wp = 0
            b.path_goal = (gx, gy)
            b.path_t = t
        if not b.path:
            return (gx - x) / d, (gy - y) / d
        pts = b.path
        while b.wp < len(pts) - 1 and (pts[b.wp][0] - x) ** 2 + (pts[b.wp][1] - y) ** 2 < 16.0:
            b.wp += 1
        wx, wy = pts[b.wp]
        dd = math.hypot(wx - x, wy - y)
        if dd < 0.5:
            return 0.0, 0.0
        return (wx - x) / dd, (wy - y) / dd

    def _find_hide(self, b, x, y, tx, ty, tz, t):
        """A nearby open spot with no line of sight from the threat."""
        wm = self.wm
        eye = (tx, ty, tz + 8.0)
        base = b.rng.random() * TAU
        best, bs = None, 1e9
        for q in range(8):
            a = base + q * PI / 4
            ca, sa = math.cos(a), math.sin(a)
            for r in (7.0, 13.0):
                cx, cy = x + ca * r, y + sa * r
                K = wm.nidx(cx, cy)
                if K < 0 or wm.nblock[K]:
                    continue
                if not wm.walk_clear(x, y, cx, cy):
                    continue
                cz = wm.nground[K]
                if wm.los(eye, (cx, cy, cz + 4.5)) or wm.los(eye, (cx, cy, cz + 8.0)):
                    continue
                # prefer close spots that are not toward the threat
                dt = math.hypot(tx - cx, ty - cy)
                sc = r - 0.1 * dt
                if sc < bs:
                    bs, best = sc, (cx, cy)
        b.hide = best
        b.hide_t = t
        return best

    # ---------------------------------------------------------------- decisions
    def _wanted(self, b, me, kind):
        """Return (take?, drop_slot or None) for a floor weapon of `kind`."""
        if kind == "gun":
            return False, None
        ws = me["weapons"]
        held = [w["kind"] for w in ws if w]
        if kind in held:
            return False, None
        for i, w in enumerate(ws):
            if w is None:
                return True, None
        worst, wi = 1e9, None
        for i, w in enumerate(ws):
            if w and w["kind"] != "gun" and not w.get("charge"):
                v = b.wvals.get(w["kind"], 1.0)
                if v < worst:
                    worst, wi = v, i
        if wi is not None and b.wvals.get(kind, 1.0) > worst + 0.25 and b.diff != "easy":
            return True, wi
        return False, None

    def _pick(self, b, me, offer):
        ch = offer.get("choices") or []
        if not ch or b.diff == "easy":
            return 0
        best, bi = -1, 0
        held = [w["kind"] for w in me["weapons"] if w]
        for i, c in enumerate(ch):
            sc = UPGRADE_SCORE.get(c.get("id"), 3.0)
            cid = c.get("id", "")
            if cid.startswith("laser") and "laser" not in held:
                sc -= 5
            if cid.startswith("mortar") and "mortar" not in held:
                sc -= 5
            if cid.startswith("lightning") and "lightning" not in held:
                sc -= 5
            if sc > best:
                best, bi = sc, i
        return bi

    def _choose_target(self, b, pos, t, hunter):
        best, bs = None, 1e18
        for i, e in self.bb.items():
            age = t - e["t"]
            if age > (25.0 if hunter else 10.0):
                continue
            p = e["pos"]
            d = math.hypot(p[0] - pos[0], p[1] - pos[1])
            sc = e["hp"] + 1.5 * d + 6.0 * age
            if i == b.tgt_id:
                sc *= 0.8
            if sc < bs:
                bs, best = sc, i
        if best is None:
            return None, None
        return best, self.bb[best]

    def _pref_range(self, b, held, tw):
        K = b.K
        R = K["range"]
        if K["counter"]:
            if "lightning" in held and "laser" in tw:
                R = 30.0
            elif "lightning" in held and "mortar" in tw and b.laser_pref and "laser" in held:
                R = max(R, 110.0)
            elif "laser" in held and b.laser_pref:
                R = max(R, 105.0) if "mortar" not in tw else max(R, 130.0)
            elif "lightning" in held:
                R = 32.0
            elif "mortar" in held and b.mortar_pref:
                R = max(R, 100.0)
        elif b.diff == "medium":
            if "lightning" in held:
                R = 36.0
            elif "laser" in held and b.laser_pref:
                R = 100.0
        return R * b.rjit

    def _lead(self, o, c, v, speed, lead):
        vx, vy = v[0] * lead, v[1] * lead
        tt = 0.0
        px, py, pz = c
        for _ in range(3):
            px, py, pz = c[0] + vx * tt, c[1] + vy * tt, c[2]
            tt = math.sqrt((px - o[0]) ** 2 + (py - o[1]) ** 2 + (pz - o[2]) ** 2) / speed
        return px, py, pz, tt

    @staticmethod
    def _yp(o, p):
        dx, dy, dz = p[0] - o[0], p[1] - o[1], p[2] - o[2]
        return math.atan2(dy, dx), math.atan2(dz, math.hypot(dx, dy))

    def _mortar_solve(self, o, c, v, lead, mstats, blind_ok):
        sp = mstats.get("speed", 72.0)
        g = mstats.get("gravity", 22.0)
        T = 1.5
        wm = self.wm
        res = None
        for _ in range(3):
            tx, ty, tz = c[0] + v[0] * lead * T, c[1] + v[1] * lead * T, c[2] - 3.0
            D = math.hypot(tx - o[0], ty - o[1])
            H = tz - o[2]
            disc = sp ** 4 - g * (g * D * D + 2 * H * sp * sp)
            if D < 16.0 or disc < 0:
                return None
            sq = math.sqrt(disc)
            yaw = math.atan2(ty - o[1], tx - o[0])
            res = None
            for sgn in (-1, 1):
                th = math.atan((sp * sp + sgn * sq) / (g * D))
                if not (self.M.get("min_pitch", -0.7) <= th <= self.M.get("max_pitch", 1.3)):
                    continue
                T2 = D / (sp * math.cos(th))
                clear = True
                cy_, sy_ = math.cos(yaw), math.sin(yaw)
                vh, vz = sp * math.cos(th), sp * math.sin(th)
                prev = o
                for q in range(1, 9):
                    tt = T2 * q / 9
                    p = (o[0] + cy_ * vh * tt, o[1] + sy_ * vh * tt, o[2] + vz * tt - 0.5 * g * tt * tt)
                    if not wm.los(prev, p, step=3.0):
                        clear = False
                        break
                    prev = p
                if clear or (blind_ok and res is None):
                    res = (yaw, th, T2, clear, D)
                    if clear:
                        break
            if res is None:
                return None
            T = res[2]
        return res

    def _act(self, b, obs, t, hunter, hidx):
        wm = self.wm
        K = b.K
        rng = b.rng
        me = obs["self"]
        tick = obs["tick"]
        pos = me["pos"]
        x, y, z = pos
        vel = me.get("vel") or [0, 0, 0]
        aim = me["aim"]
        st = me.get("stats", {})
        ws = me["weapons"]
        hpf = me["hp"] / max(1.0, me["max_hp"])
        energy = me.get("energy", 100)
        grounded = me.get("grounded", True)
        act = {"tick": tick, "move": [0.0, 0.0], "boost": False, "jump": False,
               "aim": [aim[0], aim[1]], "fire": [False, False], "reload": False}
        off = obs.get("offer")
        if off:
            act["pick"] = self._pick(b, me, off)
        for ev in obs.get("events", ()):
            et = ev.get("type")
            if et == "take_failed":
                b.blacklist[ev.get("id")] = t + 20.0
            elif et == "damaged":
                b.last_dmg_t = t
            elif et == "effect" and ev.get("effect") == "short":
                b.short_until = t + 2.0
        # aim noise: slowly varying, std = aim_err_deg
        sig = math.radians(K["aim_err_deg"] * AIM_SCALE)
        b.ny = 0.8 * b.ny + 0.6 * sig * rng.gauss(0, 1)
        b.np = 0.8 * b.np + 0.6 * sig * rng.gauss(0, 1)

        zone = obs["zone"]
        sx, sy, SR, zcx, zcy, zr = self._safe_circle(zone, t)
        dc = math.hypot(x - sx, y - sy)
        in_zone = me.get("in_zone", True)

        held = [w["kind"] for w in ws if w]
        eye_h = st.get("eye_height", 8.0)
        mz = st.get("muzzle_height", 7.0)
        o = (x + vel[0] * 0.05, y + vel[1] * 0.05, z + mz)
        eye = (x, y, z + eye_h)

        # ---- target
        tid, tg = self._choose_target(b, pos, t, hunter)
        if tid != b.tgt_id:
            b.tgt_id = tid
            b.acq_t = None
        tpos = tgt_c = None
        td = 1e9
        los = False
        age = 99.0
        if tg is not None:
            tpos = tg["pos"]
            age = t - tg["t"]
            size = 1.0 + self.size_per_cube * tg.get("cubes", 0)
            tgt_c = (tpos[0], tpos[1], tpos[2] + 4.5 * size)
            td = math.sqrt((tgt_c[0] - o[0]) ** 2 + (tgt_c[1] - o[1]) ** 2 + (tgt_c[2] - o[2]) ** 2)
            if td < 235.0 and age < 1.0:
                los = wm.los(eye, tgt_c) or wm.los(eye, (tgt_c[0], tgt_c[1], tgt_c[2] + 3.5 * size))
            if los:
                b.last_vis_t = t
                if b.acq_t is None or t - b.last_los_t > 2.0:
                    b.acq_t = t
                b.last_los_t = t
            elif b.acq_t is None and age < 1.0:
                b.acq_t = t
        react_ok = b.acq_t is not None and t - b.acq_t >= K["react_s"]
        b.last_los = los

        # ---- mode
        mode = "loot"
        if tg is not None:
            if hunter:
                mode = "hunt"
            elif td <= 105.0 and (los or age < 2.0):
                mode = "defend"
            elif age < 8.0 and td < 150.0:
                mode = "avoid"
        elif hunter:
            mode = "search"
        if K["retreat_hp"] > 0 and tg is not None and hpf < K["retreat_hp"] and td < 170.0 and (
                t - b.last_dmg_t < 3.0 or b.retreat_until > t):
            b.retreat_until = max(b.retreat_until, t + 4.0)
        if b.retreat_until > t and hpf > K["retreat_hp"] + 0.35:
            b.retreat_until = 0.0
        retreating = b.retreat_until > t and tg is not None

        mvx = mvy = 0.0
        speed = K["speed"]
        want_boost = False
        aim_override = None   # (yaw, pitch) desired before noise
        goal_far = 0.0

        # ---- shooting setup (when we have an actual target to shoot at)
        fight = tg is not None and (mode in ("hunt", "defend") or retreating)
        tw = tg["weapons"] if tg else []
        R = self._pref_range(b, held, tw) if tg else K["range"]
        if mode == "defend":
            R = max(R, 70.0)

        # laser / reload driven hiding
        li = next((i for i, w in enumerate(ws) if w and w["kind"] == "laser"), None)
        gi = next((i for i, w in enumerate(ws) if w and w["kind"] == "gun"), None)
        lw = ws[li] if li is not None else None
        gw = ws[gi] if gi is not None else None
        busy = False
        if K["cover"] and fight and td > 38.0:
            if lw and b.laser_pref and lw.get("cooldown", 0) > 0.45 and not lw.get("charge"):
                busy = True
            elif gw and gw.get("reload", 0) > 0.3:
                busy = True

        if fight:
            dh = math.hypot(tpos[0] - x, tpos[1] - y)
            ux, uy = ((tpos[0] - x) / dh, (tpos[1] - y) / dh) if dh > 0.1 else (1.0, 0.0)
            if retreating or busy:
                if b.hide is None or t - b.hide_t > 2.5 or (retreating and t - b.hide_t > 1.2 and los):
                    self._find_hide(b, x, y, tpos[0], tpos[1], tpos[2], t)
                if b.hide is not None and (los or retreating) and math.hypot(b.hide[0] - x, b.hide[1] - y) > 1.5:
                    mvx, mvy = self._goto(b, x, y, b.hide[0], b.hide[1], t)
                elif retreating and los:
                    mvx, mvy = -ux, -uy
                elif retreating:
                    mvx = mvy = 0.0
                else:
                    mvx = mvy = 0.0
                if retreating and not los and dh < 120.0:
                    mvx, mvy = -ux * 0.5, -uy * 0.5
            elif dh > R * 1.2 or not los:
                # approach: aim for the ring around the target at preferred range
                ang = math.atan2(y - tpos[1], x - tpos[0])
                if K["flank"] > 0:
                    b.flank_ang = ((hidx % 3) - 1) * 0.9 * K["flank"]
                    ang += b.flank_ang
                rr = R if los or dh > R * 1.5 else max(R * 0.6, 10.0)
                gx = tpos[0] + math.cos(ang) * rr
                gy = tpos[1] + math.sin(ang) * rr
                gx, gy = self._clamp_goal(gx, gy, sx, sy, SR)
                if not los and mode == "hunt" and dh <= R * 1.2:
                    gx, gy = tpos[0], tpos[1]
                    gx, gy = self._clamp_goal(gx, gy, sx, sy, SR)
                mvx, mvy = self._goto(b, x, y, gx, gy, t)
                goal_far = math.hypot(gx - x, gy - y)
            else:
                radial = max(-1.0, min(1.0, (dh - R) / (0.3 * R)))
                tang = 0.0
                if K["strafe"]:
                    if t > b.strafe_until:
                        b.strafe = -b.strafe if rng.random() < 0.7 else b.strafe
                        b.strafe_until = t + rng.uniform(0.8, 2.2)
                    tang = b.strafe
                    if tg["charging"] and K["dodge"] and abs(angdiff(tg["aim"][0], math.atan2(y - tpos[1], x - tpos[0]))) < 0.12:
                        b.dodge_until = t + 0.6
                        b.strafe = -b.strafe if rng.random() < 0.5 else b.strafe
                        tang = b.strafe
                        if grounded and rng.random() < 0.6:
                            act["jump"] = True
                    if grounded and rng.random() < 0.03 and energy > 40:
                        act["jump"] = True
                mvx = ux * radial * 0.8 - uy * tang * 0.9
                mvy = uy * radial * 0.8 + ux * tang * 0.9
                if tang:
                    nrm = math.hypot(mvx, mvy)
                    if nrm > 1e-3 and not wm.walk_clear(x, y, x + mvx / nrm * 8.0, y + mvy / nrm * 8.0):
                        b.strafe = -b.strafe
                        tang = b.strafe
                        mvx = ux * radial * 0.8 - uy * tang * 0.9
                        mvy = uy * radial * 0.8 + ux * tang * 0.9
                        nrm = math.hypot(mvx, mvy)
                        if nrm > 1e-3 and not wm.walk_clear(x, y, x + mvx / nrm * 8.0, y + mvy / nrm * 8.0):
                            mvx, mvy = ux * radial * 0.8, uy * radial * 0.8
        elif mode == "avoid":
            dh = math.hypot(tpos[0] - x, tpos[1] - y) or 1.0
            gx, gy = x - (tpos[0] - x) / dh * 50.0, y - (tpos[1] - y) / dh * 50.0
            gx, gy = self._clamp_goal(gx, gy, sx, sy, SR)
            mvx, mvy = self._goto(b, x, y, gx, gy, t)
        else:
            # loot / search / roam
            mvx, mvy, goal_far = self._loot_or_roam(b, obs, me, t, hunter, mode, sx, sy, SR, act)

        # ---- zone: stay inside (current / next circle)
        if dc > SR:
            pull = math.hypot(sx - x, sy - y) or 1.0
            if not in_zone or dc > SR + 25.0:
                mvx, mvy = self._goto(b, x, y, sx, sy, t)
                speed = 1.0
                want_boost = not in_zone
            else:
                w_ = min(1.0, (dc - SR) / 25.0)
                mvx = mvx * (1 - w_) + (sx - x) / pull * w_
                mvy = mvy * (1 - w_) + (sy - y) / pull * w_

        # ---- teammate separation
        for ms, mp in self.mpos.items():
            if ms == b.slot:
                continue
            ddx, ddy = x - mp[0], y - mp[1]
            d2 = ddx * ddx + ddy * ddy
            if 0.01 < d2 < 64.0:
                dd = math.sqrt(d2)
                mvx += ddx / dd * (8.0 - dd) / 8.0 * 0.8
                mvy += ddy / dd * (8.0 - dd) / 8.0 * 0.8

        # ---- stuck detection
        b.pos_hist.append((t, x, y))
        if len(b.pos_hist) > 9:
            b.pos_hist.pop(0)
        moving = math.hypot(mvx, mvy) > 0.3
        if moving and len(b.pos_hist) >= 9 and t >= b.stuck_until:
            h0 = b.pos_hist[0]
            if math.hypot(x - h0[1], y - h0[2]) < 2.0 and not (act["fire"][0] and fight):
                b.stuck_until = t + 1.2
                b.path = None
                b.path_t = -99.0
                b.pos_hist = []
        if t < b.stuck_until and moving:
            if grounded:
                act["jump"] = True
            if energy > 15:
                want_boost = True

        # ---- boost policy
        if K["boost"] and goal_far > 70.0 and not (fight and los):
            if energy > 60:
                b.boosting = True
            elif energy < 25:
                b.boosting = False
            want_boost = want_boost or b.boosting
        elif energy < 25:
            b.boosting = False
        if want_boost and energy < 8 and grounded:
            want_boost = False
        act["boost"] = bool(want_boost)

        n = math.hypot(mvx, mvy)
        if n > 0.999:
            mvx, mvy = mvx / n * 0.999, mvy / n * 0.999
        sp = speed if not want_boost else 1.0
        act["move"] = [round(mvx * sp, 3), round(mvy * sp, 3)]

        # ---- weapons
        aimed = None
        if fight:
            aimed = self._shoot_target(b, obs, me, tg, tgt_c, td, los, age, react_ok, o, aim, act, t, ws, li, gi, K)
        elif mode == "loot" or (mode == "search"):
            aimed = self._shoot_crate(b, me, o, aim, act, ws, gi, K, t)
        if aimed is None:
            # look where we are going (keeps the vision wedge forward)
            if tg is not None and age < 4.0 and mode in ("avoid", "defend", "hunt"):
                yaw, pit = self._yp(o, tgt_c)
                aimed = (yaw, 0.0)
            elif math.hypot(mvx, mvy) > 0.2:
                aimed = (math.atan2(mvy, mvx), 0.0)
        if aimed is not None:
            yaw = aimed[0] + b.ny
            pit = aimed[1] + b.np
            pit = max(self.M.get("min_pitch", -0.7), min(self.M.get("max_pitch", 1.3), pit))
            if K["slew"] < 1.0:
                lim = K["slew"] * st.get("turn_rate", 4.0) * 0.1
                dy_ = angdiff(yaw, aim[0])
                if abs(dy_) > lim:
                    yaw = aim[0] + math.copysign(lim, dy_)
                limp = K["slew"] * st.get("pitch_rate", 3.0) * 0.1
                dp_ = pit - aim[1]
                if abs(dp_) > limp:
                    pit = aim[1] + math.copysign(limp, dp_)
            act["aim"] = [round(yaw, 4), round(pit, 4)]

        # ---- reload
        if gw is not None:
            if gw.get("ammo", 1) <= 0 and gw.get("reload", 0) <= 0:
                act["reload"] = True
            elif not (fight and los) and gw.get("ammo", 99) < 0.5 * gw["stats"].get("mag", 12) \
                    and gw.get("reload", 0) <= 0 and mode != "avoid":
                act["reload"] = True

        # ---- take weapons
        self._take(b, obs, me, act, t)
        for k in [k for k, v in b.blacklist.items() if v < t]:
            del b.blacklist[k]
        return act

    # ---------------------------------------------------------------- loot / roam
    def _take(self, b, obs, me, act, t):
        px, py, pz = me["pos"]
        for p in obs.get("pickups", ()):
            if p["id"] in b.blacklist:
                continue
            q = p["pos"]
            if (q[0] - px) ** 2 + (q[1] - py) ** 2 <= 14.0 and abs(q[2] - pz) < 3.8:
                ok, slot = self._wanted(b, me, p["kind"])
                if ok:
                    act["take"] = p["id"]
                    if slot is not None:
                        act["drop"] = slot
                    b.blacklist[p["id"]] = t + 1.0   # one try per second
                    return

    def _loot_or_roam(self, b, obs, me, t, hunter, mode, sx, sy, SR, act):
        wm = self.wm
        x, y, z = me["pos"]
        K = b.K
        # supply drop
        if not hunter or mode == "search":
            for dr in (self.drops or ()):
                did = dr["id"]
                c = self.claims.get(did)
                if c and c[0] != b.slot and c[1] > t:
                    continue
                dx, dy = dr["pos"][0] - x, dr["pos"][1] - y
                d = math.hypot(dx, dy)
                if dr.get("holder") not in (None, b.slot) and dr.get("holder") not in self.bots:
                    continue
                if dr.get("lands_in", 0) <= d / 12.0 + 10.0 and d < 260.0 and \
                        (math.hypot(dr["pos"][0] - sx, dr["pos"][1] - sy) < SR + 20):
                    self.claims[did] = (b.slot, t + 3.0)
                    if d < 3.0:
                        return 0.0, 0.0, 0.0
                    mx, my = self._goto(b, x, y, dr["pos"][0], dr["pos"][1], t)
                    return mx, my, d
        # loot item
        limit = 60.0 if hunter else 230.0
        if b.loot is not None:
            it = self.items.get(b.loot)
            if it is None or b.loot in b.blacklist:
                b.loot = None
        if b.loot is None and t - b.loot_t > 0.6:
            b.loot_t = t
            best, bs = None, 1e9
            for i, it in self.items.items():
                if i in b.blacklist:
                    continue
                c = self.claims.get(i)
                if c and c[0] != b.slot and c[1] > t:
                    continue
                typ = it[0]
                d = math.hypot(it[1] - x, it[2] - y)
                if d > limit or abs(it[3] - z) > 40:
                    continue
                if math.hypot(it[1] - sx, it[2] - sy) > SR:
                    continue
                if typ == "pick":
                    ok, _ = self._wanted(b, me, it[4])
                    if not ok:
                        continue
                    sc = d - 45.0
                elif typ == "cube":
                    sc = d - 30.0
                else:
                    if it[3] - wm.ground_at(it[1], it[2]) > 3.0:
                        continue
                    sc = d + 10.0
                if sc < bs:
                    bs, best = sc, i
            b.loot = best
        if b.loot is not None:
            it = self.items.get(b.loot)
            if it is not None:
                self.claims[b.loot] = (b.slot, t + 2.0)
                d = math.hypot(it[1] - x, it[2] - y)
                if it[0] == "crate":
                    eye = (x, y, z + 8.0)
                    if d < 26.0 and wm.los(eye, (it[1], it[2], it[3] + 1.2)):
                        return 0.0, 0.0, 0.0
                if d < 1.8 and it[0] != "pick":
                    b.loot = None
                    return 0.0, 0.0, 0.0
                if it[0] == "pick" and d < 2.0:
                    return 0.0, 0.0, 0.0
                mx, my = self._goto(b, x, y, it[1], it[2], t)
                return mx, my, d
        # hunters without a lead follow sound hints / last known prey
        if hunter:
            hint = None
            if self.bb:
                last = max(self.bb.values(), key=lambda e: e["t"])
                if t - last["t"] < 40.0:
                    hint = (last["pos"][0], last["pos"][1])
            if hint is None:
                hint = self._hint_for(b.slot, t)
            if hint is not None:
                gx, gy = self._clamp_goal(hint[0], hint[1], sx, sy, SR)
                if math.hypot(gx - x, gy - y) > 12.0:
                    mx, my = self._goto(b, x, y, gx, gy, t)
                    return mx, my, math.hypot(gx - x, gy - y)
        # roam inside the safe circle
        if (b.roam is None or t - b.roam_t > 30.0 or math.hypot(b.roam[0] - x, b.roam[1] - y) < 10.0
                or math.hypot(b.roam[0] - sx, b.roam[1] - sy) > SR):
            a = b.rng.random() * TAU
            r = SR * math.sqrt(b.rng.random()) * 0.9
            b.roam = (sx + math.cos(a) * r, sy + math.sin(a) * r)
            b.roam_t = t
        mx, my = self._goto(b, x, y, b.roam[0], b.roam[1], t)
        return mx, my, math.hypot(b.roam[0] - x, b.roam[1] - y)

    def _shoot_crate(self, b, me, o, aim, act, ws, gi, K, t):
        if gi is None or b.loot is None:
            return None
        it = self.items.get(b.loot)
        if it is None or it[0] != "crate":
            return None
        gw = ws[gi]
        d = math.hypot(it[1] - o[0], it[2] - o[1])
        if d > 32.0 or gw.get("ammo", 1) <= 0 or gw.get("reload", 0) > 0:
            return None
        if not self.wm.los(o, (it[1], it[2], it[3] + 1.2)):
            return None
        yaw, pit = self._yp(o, (it[1], it[2], it[3] + 1.2))
        err = math.hypot(angdiff(yaw, aim[0]) * math.cos(pit), pit - aim[1])
        if err < 0.05 and gw.get("cooldown", 0) < 0.12:
            act["fire"][gi] = True
        return yaw, pit

    # ---------------------------------------------------------------- combat
    def _shoot_target(self, b, obs, me, tg, c, d, los, age, react_ok, o, aim, act, t, ws, li, gi, K):
        wm = self.wm
        st = me.get("stats", {})
        turn = st.get("turn_rate", 4.0)
        prate = st.get("pitch_rate", 3.0)
        v = tg["vel"]
        lead = K["lead"]
        energy = me.get("energy", 100)
        fresh = age < 0.6

        def resid(yaw, pit):
            dy = max(0.0, abs(angdiff(yaw, aim[0])) - turn * 0.05) * math.cos(pit)
            dp = max(0.0, abs(pit - aim[1]) - prate * 0.05)
            return math.hypot(dy, dp)

        # candidate aims
        gw = ws[gi] if gi is not None else None
        gun_sol = None
        if gw is not None:
            gs = gw.get("stats", {})
            px, py, pz, tt = self._lead(o, c, v, gs.get("speed", 252.0), lead)
            y_, p_ = self._yp(o, (px, py, pz))
            gun_sol = (y_, p_, gs)
        yaw0, pit0 = self._yp(o, c)
        tol_body = math.atan2(2.0, max(d, 8.0))

        primary = None   # (slot, yaw, pitch)
        # lightning
        lg = next(((i, w) for i, w in enumerate(ws) if w and w["kind"] == "lightning"), None)
        light_ok = False
        if lg is not None:
            ls = lg[1].get("stats", {})
            lr = ls.get("range", 45.0)
            cone = math.radians(ls.get("cone_deg", 30.0))
            if d <= lr * 0.92 and los and energy >= 6 and abs(angdiff(yaw0, aim[0])) < cone * 0.45:
                light_ok = True
            if d <= lr * 0.92 and los and energy >= 6:
                primary = (lg[0], yaw0, pit0, "lightning")
        # laser
        if primary is None and li is not None and b.laser_pref:
            lw = ws[li]
            ls = lw.get("stats", {})
            charging = lw.get("charge", 0) > 0
            ready = lw.get("cooldown", 0) <= 0.1 and t >= b.short_until
            if (charging or (ready and react_ok and d >= 28.0)) and d <= ls.get("range", 280.0) - 12.0 and (los or charging):
                px, py, pz = c[0] + v[0] * 0.1 * lead, c[1] + v[1] * 0.1 * lead, c[2]
                y_, p_ = self._yp(o, (px, py, pz))
                primary = (li, y_, p_, "laser")
        # mortar
        mi = next((i for i, w in enumerate(ws) if w and w["kind"] == "mortar"), None)
        if primary is None and mi is not None and b.mortar_pref:
            mw = ws[mi]
            if mw.get("cooldown", 0) <= 0.08 and d >= 50.0 and fresh and (los or K["blind"]):
                mo = (o[0], o[1], o[2] + 2.5)
                sol = self._mortar_solve(mo, c, v, lead, mw.get("stats", self.W.get("mortar", {})), K["blind"])
                if sol is not None and (sol[3] or K["blind"]):
                    primary = (mi, sol[0], sol[1], "mortar")
        if primary is None and gun_sol is not None:
            primary = (gi, gun_sol[0], gun_sol[1], "gun")
        if primary is None:
            return None

        slot, yaw, pit, kind = primary
        # --- decide triggers
        if react_ok and fresh:
            if kind == "laser":
                lw = ws[li]
                ch = lw.get("charge", 0)
                full = lw.get("stats", {}).get("charge", 0.75)
                err = resid(yaw, pit)
                tol = max(0.01, math.atan2(1.6, d))
                if ch <= 0:
                    act["fire"][li] = err < 0.4
                else:
                    if (ch >= full * 0.97 and err < tol) or ch >= full + 1.3 or (not los and ch >= full * 0.97):
                        act["fire"][li] = False
                    else:
                        act["fire"][li] = True
            elif kind == "lightning":
                if light_ok:
                    act["fire"][slot] = True
            elif kind == "mortar":
                err = resid(yaw, pit)
                if err < 0.03 and d > 16.0:
                    act["fire"][slot] = True
            # gun can add fire if aligned (independent of the primary)
            if gw is not None and gun_sol is not None and kind != "gun" and los:
                gs = gun_sol[2]
                if (gw.get("ammo", 1) > 0 and gw.get("reload", 0) <= 0 and d <= min(gs.get("range", 230.0), K["fire_range"])
                        and resid(gun_sol[0], gun_sol[1]) < tol_body * 0.8 and kind not in ("laser",)):
                    act["fire"][gi] = True
            if kind == "gun" and los and gw is not None:
                gs = gun_sol[2]
                if (gw.get("ammo", 1) > 0 and gw.get("reload", 0) <= 0 and d <= min(gs.get("range", 230.0), K["fire_range"])
                        and resid(yaw, pit) < tol_body):
                    act["fire"][gi] = True
        elif kind == "laser":
            # release an in-progress charge only if safe; otherwise keep holding (auto-fires)
            lw = ws[li]
            act["fire"][li] = lw.get("charge", 0) > 0 and lw.get("charge", 0) < lw.get("stats", {}).get("charge", 0.75) + 1.3
        return yaw, pit
