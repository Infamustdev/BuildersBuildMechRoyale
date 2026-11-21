"""Danger map: a per-nav-cell cost of being there, used to plan the safest paths.

hot  = cells a known enemy can see (viewshed by ray casting over building heights),
       stronger close to the enemy and inside its view cone; plus sound heat.
pull = cells near items we want (pickups, cubes) discount that danger, by more the closer
       they are to the item, so we don't walk 80 m for a cube and turn back at the last step.
A* adds cost(K) to each step, so paths bend behind buildings and out of enemies' sight lines.
That's also how flanking reaches an enemy's back unseen.
"""
import math

from tuning import DANGER


class DangerMap:
    def __init__(self, wm, tracker):
        self.wm = wm
        self.tr = tracker
        self.views = {}     # enemy id -> {"pos": (x,y,z), "t": time, "cells": {K: weight}}
        self.pull = {}      # K -> danger discount fraction near wanted items
        self._sten = None
        self._pull_sig = None
        self.exposed = {}   # K -> how long and how hard we have been watched standing there
        self._esten = None
        self._exp_t = None
        self.cost_cache = {}

    # ------------------------------------------------------------ viewshed
    def _viewshed(self, ex, ey, ez, aim_yaw):
        """Cells visible from an enemy eye at (ex,ey,ez): {K: weight}. Horizon method per ray."""
        wm = self.wm
        R = DANGER["view_range"]
        rays = DANGER["rays"]
        step = wm.N
        top, nx, ny, x0, y0, inv = wm.top, wm.nx, wm.ny, wm.x0, wm.y0, wm.inv
        out = {}
        cone = math.radians(DANGER["cone_deg"]) / 2
        for r in range(rays):
            a = 2 * math.pi * r / rays
            ca, sa = math.cos(a), math.sin(a)
            in_cone = abs((a - aim_yaw + math.pi) % (2 * math.pi) - math.pi) <= cone
            horizon = -1e9
            d = step
            while d <= R:
                x, y = ex + ca * d, ey + sa * d
                i = int((x - x0) * inv)
                j = int((y - y0) * inv)
                if not (0 <= i < nx and 0 <= j < ny):
                    break
                h = top[j * nx + i]
                g = wm.ground[j * nx + i]
                # can the enemy see a mech's chest (ground + 6 m) here?
                if (g + 6.0 - ez) / d >= horizon and h - g < 1.2:
                    K = wm.nidx(x, y)
                    if K >= 0:
                        w = DANGER["seen_cost"] * (1.0 - d / (R * 1.15))
                        if in_cone:
                            w *= DANGER["cone_mult"]
                        if w > out.get(K, 0.0):
                            out[K] = w
                ang = (h - ez) / d
                if ang > horizon:
                    horizon = ang
                d += step
        return out

    def mark_exposure(self, t, pos, n_seeing):
        """Where we are seen gets worse to stand in, and keeps getting worse.

        The squad shares vision and converges on whoever they have found, so a spot is not
        dangerous in proportion to who can see us *now* -- it is dangerous in proportion to how
        long they have known we are there. This accumulates while we are watched and fades once
        we are not, which is what makes lingering in a seen spot lose to walking out of it.
        """
        dt = 0.0 if self._exp_t is None else max(0.0, min(1.0, t - self._exp_t))
        self._exp_t = t
        if dt <= 0.0:
            return
        if self.exposed:
            k = math.exp(-dt / DANGER["exposure_fade_s"])
            self.exposed = {K: v * k for K, v in self.exposed.items() if v * k > 0.02}
        if n_seeing > 0:
            K = self.wm.nidx(pos[0], pos[1])
            if K >= 0:
                cap = DANGER["exposure_cap"]
                add = DANGER["exposure_rate"] * n_seeing * dt
                for (ox, oy, w) in self._exp_sten():
                    Q = self.wm.nidx(pos[0] + ox, pos[1] + oy)
                    if Q >= 0:
                        self.exposed[Q] = min(cap, self.exposed.get(Q, 0.0) + add * w)

    def _exp_sten(self):
        """Small falloff patch: they converge on roughly where we were, not the exact cell."""
        if self._esten is None:
            N, R = self.wm.N, DANGER["exposure_radius"]
            rc = int(R / N)
            out = []
            for dx in range(-rc, rc + 1):
                for dy in range(-rc, rc + 1):
                    ox, oy = dx * N, dy * N
                    d = math.hypot(ox, oy)
                    if d <= R:
                        out.append((ox, oy, 1.0 - d / (R + N)))
            self._esten = out
        return self._esten

    def update(self, t, want_items=()):
        """Refresh viewsheds of recently known enemies, and the item cool-spots."""
        self.cost_cache = {}
        live = set()
        budget = DANGER["max_recasts_per_tick"]
        for tr in sorted(self.tr.tracks.values(), key=lambda q: -q.seen_t):
            if tr.kind != "mech" or t - tr.seen_t > DANGER["forget_s"]:
                continue
            live.add(tr.id)
            v = self.views.get(tr.id)
            px, py, pz = tr.pos
            # a mech on its last legs cannot hurt us much and is worth walking at: its sight
            # lines cost proportionally less, so wounding someone opens ground up rather than
            # leaving it as frightening as it was at full health
            mh = max(1.0, getattr(tr, "max_hp", 0.0) or 1.0)
            hpf = max(0.0, min(1.0, (getattr(tr, "hp", mh) or mh) / mh))
            threat = DANGER["threat_floor"] + (1.0 - DANGER["threat_floor"]) * hpf
            if budget > 0 and (v is None or math.hypot(px - v["pos"][0], py - v["pos"][1]) > DANGER["recast_move"]
                               or t - v["t"] > DANGER["recast_s"]):
                budget -= 1
                self.views[tr.id] = {"pos": (px, py, pz), "t": t,
                                     "cells": self._viewshed(px, py, pz + 8.0, tr.aim[0])}
            if tr.id in self.views:
                self.views[tr.id]["threat"] = threat   # refreshed every tick, recast or not
        for k in list(self.views):
            if k not in live:
                del self.views[k]
        # Items do not move, so the wells only change when we learn about one or take one.
        # Rebuilding them every tick was the single most expensive thing the bot did.
        sig = want_items if isinstance(want_items, tuple) else tuple(want_items)
        if sig != self._pull_sig:
            self._pull_sig = sig
            self.pull = {}
            for (x, y) in sig[:DANGER["pull_items_max"]]:
                for (ox, oy, p) in self._stencil():
                    K = self.wm.nidx(x + ox, y + oy)
                    if K >= 0 and p > self.pull.get(K, 0.0):
                        self.pull[K] = p

    def _stencil(self):
        """Offsets around an item with the pull fraction at each: a well that deepens
        toward the item, so danger matters less the closer we already are to the prize."""
        if self._sten is None:
            N, R = self.wm.N, DANGER["pull_radius"]
            pmax, rc = DANGER["pull_max"], int(R / self.wm.N)
            out = []
            for dx in range(-rc, rc + 1):
                for dy in range(-rc, rc + 1):
                    ox, oy = dx * N, dy * N
                    d = math.hypot(ox, oy)
                    if d <= R:
                        out.append((ox, oy, pmax * (1.0 - d / R) ** 2))
            self._sten = out
        return self._sten

    # ------------------------------------------------------------ queries
    def signed(self, K):
        """What this cell is worth standing on: positive is danger, negative is inviting.

        An item with nobody looking at the ground around it should not merely be "not scary",
        it should pull us in, so the pull both discounts whatever hazard is there and pays a
        bonus of its own. A* cannot take negative edges, so `cost` clamps this at zero; the
        planner uses the signed value, which is where going negative actually changes choices.
        """
        c = self.cost_cache.get(K)
        if c is not None:
            return c
        c = 0.0
        for v in self.views.values():
            c += v.get("threat", 1.0) * v["cells"].get(K, 0.0)
        c += DANGER["heat_cost"] * min(3.0, self.tr.heat_at_cell(K))
        if self.exposed:
            c += DANGER["exposure_cost"] * self.exposed.get(K, 0.0)
        p = self.pull.get(K, 0.0)
        c = max(0.0, c) * (1.0 - p) - DANGER["pull_bonus"] * p
        self.cost_cache[K] = c
        return c

    def cost(self, K):
        return max(0.0, self.signed(K))

    def at(self, x, y):
        K = self.wm.nidx(x, y)
        return self.signed(K) if K >= 0 else 0.0

    def any(self):
        return bool(self.views) or bool(self.tr.heat)

    def line_cost(self, ax, ay, bx, by):
        """Danger summed along the straight route, scaled to the whole length.

        Sampled at most `line_samples` times: a 300 m route does not need 75 probes to tell us
        it crosses an enemy's sight line, and the planner scores a dozen options a tick.
        """
        d = math.hypot(bx - ax, by - ay)
        n = int(d / self.wm.N) + 1
        step = max(1, n // DANGER["line_samples"] + 1) if n > DANGER["line_samples"] else 1
        s, k = 0.0, 0
        for q in range(1, n + 1, step):
            f = q / n
            s += self.at(ax + (bx - ax) * f, ay + (by - ay) * f)
            k += 1
        return s * (n / k) if k else 0.0
