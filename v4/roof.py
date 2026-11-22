"""Roof play: a mech standing back from the edge of a roof cannot be seen or hit from the ground
around the building (the edge hides its body), and getting up there costs a third of an energy bar.
Measured in real matches: 9 damage a second taken on roofs against 32 anywhere else.

Used here as the place to retreat to and heal: when the judge says retreat and a roof we can climb
is close, we walk to its climb point, jump and jet over the edge, then stand on the roof cell that
no recent threat can see (or the deepest cell, farthest from every edge) until the retreat ends.
Leaving is ordinary walking: no fall damage, so the legs just walk off the edge.

Regions are built once from the nav grid: connected blocked cells with the same top height,
at least MIN_H above the ground around them, with the distance of every cell to the region edge
and the ground cells next to the wall where we can climb.
"""
import math

MIN_H = 6.0            # lowest roof worth climbing (lower ones do not hide us)
MAX_CLIMB = 40.0       # tallest wall we try
MIN_CELLS = 6          # smallest roof (4 m cells)
ACCESS_MAX_M = 45.0    # retreat to a roof only if its climb point is this close
CLIMB_RISE = 8.9       # m/s of rise while jetting
COVER_HOLD_S = 6.0     # stay ducked this long after the last watcher
BAN_S = 90.0


class Roof:
    __slots__ = ("id", "z", "cells", "inner", "access")


class Roofs:
    def __init__(self, wm):
        self.wm = wm
        nnx, nny = wm.nnx, wm.nny
        nb, ng, nr = wm.nblock, wm.nground, wm.nroof
        n = nnx * nny
        self.of = [-1] * n
        self.dist = [0] * n
        self.list = []
        for K0 in range(n):
            if not nb[K0] or self.of[K0] >= 0:
                continue
            I0, J0 = K0 % nnx, K0 // nnx
            if I0 == 0 or J0 == 0 or I0 == nnx - 1 or J0 == nny - 1:
                continue
            z0 = nr[K0]
            if z0 - ng[K0] < MIN_H:
                continue
            rid = len(self.list)
            cells, stack = [], [K0]
            self.of[K0] = rid
            while stack:
                K = stack.pop()
                cells.append(K)
                I, J = K % nnx, K // nnx
                for K2 in (K - 1, K + 1, K - nnx, K + nnx):
                    I2, J2 = K2 % nnx, K2 // nnx
                    if (0 < I2 < nnx - 1 and 0 < J2 < nny - 1 and abs(I2 - I) + abs(J2 - J) == 1
                            and nb[K2] and self.of[K2] < 0 and abs(nr[K2] - z0) < 0.6):
                        self.of[K2] = rid
                        stack.append(K2)
            r = Roof()
            r.id, r.z, r.cells = rid, z0, cells
            self.list.append(r)
            if len(cells) < MIN_CELLS:
                for K in cells:
                    self.of[K] = -2          # too small: not a roof
                r.cells = []
                r.access = []
                r.inner = -1
                continue
            # distance to the region edge (1 = edge cell)
            cs = set(cells)
            front = []
            for K in cells:
                if any(K2 not in cs for K2 in (K - 1, K + 1, K - nnx, K + nnx)):
                    self.dist[K] = 1
                    front.append(K)
            d = 1
            while front:
                d += 1
                nxt = []
                for K in front:
                    for K2 in (K - 1, K + 1, K - nnx, K + nnx):
                        if K2 in cs and self.dist[K2] == 0:
                            self.dist[K2] = d
                            nxt.append(K2)
                front = nxt
            r.inner = max(cells, key=lambda K: self.dist[K])
            # climb points: walkable ground cell next to an edge cell
            acc = []
            for K in cells:
                if self.dist[K] != 1:
                    continue
                for K2 in (K - 1, K + 1, K - nnx, K + nnx):
                    if not nb[K2]:
                        climb = z0 - ng[K2]
                        if MIN_H <= climb <= MAX_CLIMB:
                            acc.append((K2, K, climb))
            r.access = acc
        for i, v in enumerate(self.of):
            if v == -2:
                self.of[i] = -1
        # state
        self.state = "none"      # none / approach / climb / on
        self.rid = -1
        self.acc = None
        self.t0 = 0.0
        self.ban = {}
        self.cover = -1
        self.cover_t = -1e9
        self.watched_t = -1e9
        self.mode = ""

    def on_roof(self, me):
        K = self.wm.nidx(me["pos"][0], me["pos"][1])
        if K < 0:
            return -1
        rid = self.of[K]
        if rid >= 0 and abs(me["pos"][2] - self.list[rid].z) < 1.6:
            return rid
        return -1

    def max_climb(self, me):
        drain = float((me.get("stats") or {}).get("jet_drain", 15.0)) or 15.0
        return min(MAX_CLIMB, (float(me.get("max_energy", 100.0)) - 28.0) / drain * CLIMB_RISE)

    def _spot_ok(self, ctx, K):
        z = ctx.zone
        x, y = self.wm.ncenter(K)
        nc = z.get("next_center", z["center"])
        return math.hypot(x - nc[0], y - nc[1]) < float(z.get("next_radius", z["radius"])) - 8.0

    def _choose(self, ctx, me):
        x, y = me["pos"][0], me["pos"][1]
        limit = self.max_climb(me)
        best, bs = None, 1e18
        for r in self.list:
            if not r.access or self.ban.get(r.id, -1.0) > ctx.t or not self._spot_ok(ctx, r.inner):
                continue
            if self.dist[r.inner] < 2:
                continue
            for a in r.access:
                if a[2] > limit:
                    continue
                gx, gy = self.wm.ncenter(a[0])
                d = math.hypot(gx - x, gy - y)
                if d < bs and d <= ACCESS_MAX_M:
                    best, bs = (r.id, a), d
        return best

    def _eyes(self, ctx):
        mem, t = ctx.mem, ctx.t
        x, y = ctx.me["pos"][0], ctx.me["pos"][1]
        eyes = []
        for tr in mem.recent_mechs(8.0):
            if math.hypot(tr.pos[0] - x, tr.pos[1] - y) <= 280.0:
                eyes.append((tr.pos[0], tr.pos[1], tr.pos[2] + 8.0))
        return eyes

    def _cover(self, ctx, rid):
        """Nearest cell on our roof (within 50 m, 3+ cells from the edge) that no recent threat can
        see, body or head. -1 when the roof cannot hide us."""
        eyes = self._eyes(ctx)
        if not eyes:
            return -1
        r = self.list[rid]
        me = ctx.me
        x, y = me["pos"][0], me["pos"][1]
        h = 9.0
        best, bs = -1, 1e18
        nnx = self.wm.nnx
        for K in r.cells:
            if ((K % nnx) + (K // nnx)) % 2 or self.dist[K] < 3:
                continue
            cx, cy = self.wm.ncenter(K)
            d = math.hypot(cx - x, cy - y)
            if d > 50.0 or d >= bs:
                continue
            if all(not self.wm.los(e, (cx, cy, r.z + h * 0.5), canopy=False)
                   and not self.wm.los(e, (cx, cy, r.z + h * 0.97), canopy=False) for e in eyes):
                best, bs = K, d
        return best

    def _spot(self, me, rid):
        """Deepest cell of the roof; walk straight there when the way stays on the roof, else
        uphill on the edge-distance field."""
        wm, r = self.wm, self.list[rid]
        x, y = me["pos"][0], me["pos"][1]
        K = wm.nidx(x, y)
        ix, iy = wm.ncenter(r.inner)
        if K < 0 or self.of[K] != rid:
            return ix, iy
        d = math.hypot(ix - x, iy - y)
        steps = int(d / 2) + 1
        clear = True
        for s in range(1, steps + 1):
            f = s / steps
            c = wm.nidx(x + (ix - x) * f, y + (iy - y) * f)
            if c < 0 or self.of[c] != rid or self.dist[c] < 2:
                clear = False
                break
        if clear:
            return ix, iy
        nnx = wm.nnx
        best, bd = K, self.dist[K]
        for dj in range(-3, 4):
            for di in range(-3, 4):
                c = K + dj * nnx + di
                if 0 <= c < len(self.of) and self.of[c] == rid and self.dist[c] > bd:
                    best, bd = c, self.dist[c]
        return wm.ncenter(best)

    def _enemy_up_here(self, ctx):
        me = ctx.me
        x, y, z = me["pos"]
        return any(math.hypot(e.pos[0] - x, e.pos[1] - y) < 70.0 and abs(e.pos[2] - z) < 5.0
                   for e in ctx.mem.recent_mechs(0.5))

    def step(self, ctx, route):
        """(move, boost, jump) while the roof plan has the legs, else None."""
        me, t, j = ctx.me, ctx.t, ctx.judge
        x, y, z = me["pos"]
        z_info = ctx.zone
        late = float(z_info.get("next_radius", 999.0)) < 60.0
        # only under pressure: retreating, or our cell is hot (several watching) and we are hurt
        hp_frac = me["hp"] / max(1.0, me["max_hp"])
        want = not late and (j.retreat or (j.pressed and hp_frac < 0.7))
        under = any(t - h[0] < 1.0 and h[3] != "zone" for h in ctx.mem.hits)
        if want or under:
            self.watched_t = t
        on = self.on_roof(me)
        if on >= 0:
            self.state, self.rid = "on", on
            if self._enemy_up_here(ctx):
                self.mode = "roof:brawl"
                return None
            if not (want or t - self.watched_t < COVER_HOLD_S):
                return None          # nothing to hide from: the legs take over (walk off the edge)
            if t - self.cover_t > 0.4:
                self.cover, self.cover_t = self._cover(ctx, on), t
            if self.cover >= 0:
                gx, gy = self.wm.ncenter(self.cover)
                self.mode = "roof:cover"
            else:
                gx, gy = self._spot(me, on)
                self.mode = "roof:hold"
            d = math.hypot(gx - x, gy - y)
            if d < 2.0:
                return (0.0, 0.0), False, False
            return ((gx - x) / d, (gy - y) / d), False, False
        if self.state == "on":
            self.state = "none"
        if not want:
            if self.state != "climb":
                self.state = "none"
                return None
        if self.state == "none":
            pick = self._choose(ctx, me)
            if pick is None:
                return None
            self.rid, self.acc = pick
            self.state, self.t0 = "approach", t
        r = self.list[self.rid]
        gK, eK, climb = self.acc
        gx, gy = self.wm.ncenter(gK)
        energy = float(me.get("energy", 0.0))
        if self.state == "approach":
            if t - self.t0 > 20.0:
                self.ban[self.rid] = t + BAN_S
                self.state = "none"
                return None
            if math.hypot(gx - x, gy - y) > 3.0:
                self.mode = "roof:approach"
                mv = route(gx, gy)
                return mv, energy > 60.0 and bool(mv[0] or mv[1]), False
            self.state, self.t0 = "climb", t
        # climb: jump, jet past the roof height, drift over the edge
        ex, ey = self.wm.ncenter(eK)
        d = math.hypot(ex - x, ey - y) or 1.0
        mv = ((ex - x) / d, (ey - y) / d)
        grounded = me.get("grounded", True)
        K = self.wm.nidx(x, y)
        over = K >= 0 and self.of[K] == self.rid
        drain = float((me.get("stats") or {}).get("jet_drain", 15.0)) or 15.0
        jump = boost = False
        self.mode = "roof:climb"
        if grounded and z < r.z - 1.0:
            if energy >= min((r.z - z) / CLIMB_RISE * drain + 16.0, float(me.get("max_energy", 100.0)) - 3.0):
                jump = True
            else:
                mv = (0.0, 0.0)       # recharge at the foot of the wall
                self.t0 = t           # the budget starts with the jump
        elif z < r.z + 0.7:
            boost = energy > 0.5
        elif not over:
            boost = float(me["vel"][2]) < 0.5
        if t - self.t0 > climb / 8.0 + 5.0 or (energy < 2.0 and not grounded and z < r.z - 0.5):
            self.ban[self.rid] = t + BAN_S
            self.state = "none"
        return mv, boost, jump
