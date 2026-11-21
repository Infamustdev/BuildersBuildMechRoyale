"""Danger map: a per-nav-cell cost of being there, used to plan the safest paths.

hot  = cells a known enemy can see (viewshed by ray casting over building heights),
       stronger close to the enemy and inside its view cone; plus sound heat.
cool = cells near items we want (pickups, cubes) are slightly cheaper.
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
        self.cool = {}      # K -> negative cost near wanted items
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
            if budget > 0 and (v is None or math.hypot(px - v["pos"][0], py - v["pos"][1]) > DANGER["recast_move"]
                               or t - v["t"] > DANGER["recast_s"]):
                budget -= 1
                self.views[tr.id] = {"pos": (px, py, pz), "t": t,
                                     "cells": self._viewshed(px, py, pz + 8.0, tr.aim[0])}
        for k in list(self.views):
            if k not in live:
                del self.views[k]
        self.cool = {}
        for (x, y) in want_items:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    K = self.wm.nidx(x + dx * self.wm.N, y + dy * self.wm.N)
                    if K >= 0:
                        self.cool[K] = -DANGER["item_cool"]

    # ------------------------------------------------------------ queries
    def cost(self, K):
        c = self.cost_cache.get(K)
        if c is not None:
            return c
        c = 0.0
        for v in self.views.values():
            c += v["cells"].get(K, 0.0)
        c += DANGER["heat_cost"] * min(3.0, self.tr.heat_at_cell(K))
        c += self.cool.get(K, 0.0)
        c = max(0.0, c)
        self.cost_cache[K] = c
        return c

    def at(self, x, y):
        K = self.wm.nidx(x, y)
        return self.cost(K) if K >= 0 else 0.0

    def any(self):
        return bool(self.views) or bool(self.tr.heat)

    def line_cost(self, ax, ay, bx, by):
        d = math.hypot(bx - ax, by - ay)
        n = int(d / self.wm.N) + 1
        s = 0.0
        for q in range(1, n + 1):
            f = q / n
            s += self.at(ax + (bx - ax) * f, ay + (by - ay) * f)
        return s
