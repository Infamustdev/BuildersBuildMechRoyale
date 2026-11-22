"""Fight movement: pick the best cell 3-9 m around us (or stay) against the judge's foe.

Score per cell (sight from other enemies comes from the seen_by heat layer):
  -1.1 x |distance to foe - preferred range| - 0.4 x step length
  +45 x want_los if the cell has a shot line to the foe (want_los = +1 loaded, -1 reloading)
  -30 per other enemy that can see the cell (seen_by layer; -15 if we have not seen it in 2.5 s)
  -8 x likely-enemy heat (capped at 3)
  -4.2 per metre inside a lightning carrier's range + 16 m (when we carry no lightning)
  zone penalty; +1.5 per metre closer to a cube within 40 m; -3 per blocked neighbour (walls)
  +9 for stepping the strafe way (when loaded); staying put -8, or +14 if reloading and hidden
Preferred range: lightning 28 m, laser 120 m, gun 85 m; never inside lightning range + 19 m of a
carrier without lightning of our own, never under 80 m from a mortar; 20 m to finish a mech
under 38% HP when we are above 42% and at most two enemies are on us.
"""
import math

from judge import Judge

RING_MIN, RING_MAX = 3.0, 9.0
GUN_R, LASER_R, LIGHT_R, FINISH_R = 85.0, 120.0, 28.0, 20.0


def unit(dx, dy):
    d = math.hypot(dx, dy)
    return (dx / d, dy / d) if d > 1e-6 else (0.0, 0.0)


class Fight:
    def __init__(self, wm, cfg, rng, judge):
        self.wm = wm
        self.rng = rng
        self.j = judge
        self.light_range = float(cfg.get("weapons", {}).get("lightning", {}).get("range", 45.0))
        self.strafe = 1
        self.strafe_until = -1e9
        self.cells = []           # (x, y, score) of the last evaluation, for the viewer
        self.best = None
        self.want_los = 1.0
        self.mode = "fight"
        self._ring = None

    def strafe_dir(self, t):
        """Current strafe side (+1/-1), flipped every 0.3-1.0 s like fight movement."""
        if t >= self.strafe_until:
            if self.rng.random() < 0.6:
                self.strafe = -self.strafe
            self.strafe_until = t + 0.3 + self.rng.random() * 0.7
        return self.strafe

    def _ring_offsets(self):
        if self._ring is None:
            N = self.wm.N
            n = int(RING_MAX / N) + 1
            self._ring = [(di * N, dj * N) for di in range(-n, n + 1) for dj in range(-n, n + 1)
                          if RING_MIN <= math.hypot(di * N, dj * N) <= RING_MAX]
        return self._ring

    def pref_range(self, me, foe, crowd):
        _, light = Judge.weapon(me, "lightning")
        _, laser = Judge.weapon(me, "laser")
        hp_frac = me["hp"] / max(1.0, me["max_hp"])
        if foe.kind == "npc":
            return 58.0
        if foe.hp < 0.38 * foe.max_hp and hp_frac > 0.42 and crowd <= 2:
            self.mode = "finish"
            return FINISH_R
        if light is not None and float(me.get("energy", 0.0)) > 12.0:
            r = LIGHT_R
        elif laser is not None:
            r = LASER_R
        else:
            r = GUN_R
        if light is None:
            if "lightning" in (foe.weapons or ()):
                r = max(r, self.light_range + 19.0)
            if "mortar" in (foe.weapons or ()):
                r = max(r, 80.0)
        return r

    def _want_los(self, me, foe_d):
        _, gun = Judge.weapon(me, "gun")
        _, laser = Judge.weapon(me, "laser")
        _, light = Judge.weapon(me, "lightning")
        if light is not None or laser is not None:
            return 1.0           # always a shot to take with a secondary
        if gun is None:
            return -1.0
        gr = float((gun.get("stats") or {}).get("range", 180.0))
        ready = gun.get("ammo", 0) > 0 and gun.get("reload", 0.0) <= 0.25 and foe_d <= gr - 10.0
        return 1.0 if ready else -1.0

    def _zone_pen(self, z, t, x, y):
        cx, cy = z["center"]
        p = 0.0
        d = math.hypot(x - cx, y - cy) - z["radius"]
        if d > -5.0:
            p += 150.0 + 5.0 * (d + 5.0)
        nx, ny = z["next_center"]
        dn = math.hypot(x - nx, y - ny) - z["next_radius"]
        if dn > -5.0:
            soon = min(1.0, max(0.1, 1.0 - (z.get("shrink_start", 0.0) - t) / 25.0))
            p += soon * (25.0 + 0.8 * (dn + 5.0))
        return p

    def decide(self, ctx, foe):
        j, wm, heat, mem = self.j, self.wm, ctx.heat, ctx.mem
        me = ctx.me
        t = ctx.t
        x, y, z = me["pos"]
        self.mode = "fight"
        crowd = sum(1 for e in mem.recent_mechs(0.01)
                    if e in j.engaged or (j.los.get(e.id) and j.dist.get(e.id, 1e9) < 150.0))
        rp = self.pref_range(me, foe, crowd)
        foe_d = j.dist.get(foe.id, math.hypot(foe.pos[0] - x, foe.pos[1] - y))
        want = 1.0 if foe.kind == "npc" else self._want_los(me, foe_d)
        self.want_los = want
        bx, by = unit(foe.pos[0] - x, foe.pos[1] - y)
        if t >= self.strafe_until:
            if self.rng.random() < 0.6:
                self.strafe = -self.strafe
            self.strafe_until = t + 0.3 + self.rng.random() * 0.7
        sx, sy = -by * self.strafe, bx * self.strafe
        st = me.get("stats") or {}
        mh = float(st.get("muzzle_height", 7.0))
        th = j.npc_h if foe.kind == "npc" else j.mech_h * (1.0 + j.size_per_cube * (foe.cubes or 0))
        tc = (foe.pos[0], foe.pos[1], foe.pos[2] + th * 0.5)

        cube, cube_d = None, 40.0
        for c in mem.cubes.values():
            if abs(c["pos"][2] - z) < 4.0:
                d = math.hypot(c["pos"][0] - x, c["pos"][1] - y)
                if d < cube_d:
                    cube, cube_d = c["pos"], d
        _, my_light = Judge.weapon(me, "lightning")
        light_foes = []
        if my_light is None:
            light_foes = [e for e in mem.recent_mechs(2.5) if "lightning" in (e.weapons or ())
                          and math.hypot(e.pos[0] - x, e.pos[1] - y) < self.light_range + 32.0]
        fresh = {e.id for e in mem.recent_mechs(2.5)}
        zone = ctx.zone

        cands = [(x, y, 0.0, 0.0, 0.0)]
        blocked_side = False
        for ox, oy in self._ring_offsets():
            cx, cy = x + ox, y + oy
            K = wm.nidx(cx, cy)
            r = math.hypot(ox, oy)
            if K < 0 or wm.nblock[K] or not wm.walk_clear(x, y, cx, cy):
                if (ox * sx + oy * sy) / r > 0.9:
                    blocked_side = True
                continue
            cands.append((cx, cy, ox / r, oy / r, r))

        best, bs = None, -1e18
        out = []
        nnx, nb = wm.nnx, wm.nblock
        for cx, cy, dx, dy, r in cands:
            K = wm.nidx(cx, cy)
            d = math.hypot(foe.pos[0] - cx, foe.pos[1] - cy)
            s = -1.1 * abs(d - rp) - 0.4 * r
            cz = wm.nground[K] if K >= 0 else z
            los = wm.los((cx, cy, cz + mh), tc, canopy=False)
            if los:
                s += 45.0 * want
            for eid in heat.seen_by.get(K, ()):
                if eid != foe.id:
                    s -= 30.0 if eid in fresh else 15.0
            s -= 8.0 * min(heat.likely(K), 3.0)
            for lf in light_foes:
                ed = math.hypot(lf.pos[0] - cx, lf.pos[1] - cy)
                if ed < self.light_range + 16.0:
                    s -= (self.light_range + 16.0 - ed) * 4.2
            s -= self._zone_pen(zone, t, cx, cy)
            if cube is not None:
                s += 1.5 * (cube_d - math.hypot(cube[0] - cx, cube[1] - cy))
            if K >= 0:
                walls = nb[K - 1] + nb[K + 1] + nb[K - nnx] + nb[K + nnx]
                s -= 3.0 * walls
            if want > 0:
                s += 9.0 * (dx * sx + dy * sy)
            if r == 0.0:
                s -= 8.0
                if want < 0 and not los:
                    s += 14.0
            out.append((round(cx, 1), round(cy, 1), round(s, 1)))
            if s > bs:
                bs, best = s, (cx, cy, dx, dy)
        if blocked_side:
            self.strafe = -self.strafe
            self.strafe_until = t + 0.5
        self.cells, self.best = out, best
        if best is None or (best[2] == 0.0 and best[3] == 0.0):
            return (0.0, 0.0), False
        boost = my_light is not None and foe_d > 50.0 and float(me.get("energy", 0.0)) > 55.0 and want > 0
        return (best[2], best[3]), boost
