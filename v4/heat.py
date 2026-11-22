"""Heatmap layers on the 4 m nav grid. Each layer answers one question; nothing sums them here.

- seen_by[K]   : ids of enemies that have a clear shot line to cell K (buildings and terrain
                 block, tree crowns do not). Line of sight is symmetric, so these are also the
                 cells we can shoot each enemy from. Computed by a ray sweep over the full 200 m.
- likely(K)    : an unseen enemy is probably here (sounds, lost tracks, hits from unseen
                 shooters). Decays.
- expo(K)      : we were watched or hit while standing here. Decays slowly.
- enemy_cells  : coarse 64 m grid of where enemies were last seen.

Every viewshed is budgeted in ray samples, not milliseconds, so a seed replays the same.
"""
import math

from memory import angdiff

VIEW_R = 200.0        # sight range
N_RAYS = 320          # rays per viewshed (gap at 200 m is under one 4 m cell)
KEEP_R = 350.0        # keep viewsheds of mechs this close to us
EYE = 8.0             # eye / muzzle height above ground
REFRESH_MOVE = 6.0    # enemy moved this far -> recompute its viewshed
REFRESH_AGE = 3.0     # ...or it is this old
ENEMY_MEMORY_S = 8.0  # keep a viewshed this long after we last saw the mech
SAMPLE_BUDGET = 12000  # ray samples per tick (~5 ms; one full viewshed is ~30k)

LIKELY_TAU = 6.0
EXPO_TAU = 20.0
HUNT_CELL = 64.0
HEARD_MIN = 0.15      # likely-enemy heat a heard hotspot needs before we hunt it
SEEN_CELL = 32.0      # explore grid
SEEN_NEAR = 45.0      # all-round sight
SEEN_FAR = 110.0      # cone sight we credit (crowns thin it out beyond this)
SEEN_EVERY = 0.5
SOUND_W = {"gun": 1.0, "laser": 1.2, "laser_charge": 1.6, "lightning": 1.0, "mortar": 1.0,
           "explosion": 0.25, "step": 1.6, "boost": 1.2, "jet": 1.0, "reload": 0.8,
           "death": 0.2, "crate": 0.6, "npc_gun": 0.1, "drop": 0.0}


class Viewshed:
    __slots__ = ("org", "t", "cells", "job", "job_i", "job_org", "job_xy")

    def __init__(self):
        self.org = None
        self.t = -1e9
        self.cells = set()
        self.job = None      # cells found so far by the running sweep
        self.job_i = 0       # next ray
        self.job_org = None  # (fine i, fine j, eye z)
        self.job_xy = None


class Heat:
    def __init__(self, wm, cfg):
        self.wm = wm
        snd = cfg.get("sound", {})
        self.noise = math.radians(snd.get("bearing_noise_deg", 7.0))
        self.carry = snd.get("range", {})
        self.near_d = snd.get("near", 60.0)
        self.mid_d = snd.get("mid", 160.0)
        self.t = 0.0
        self._ray_cache = None
        self._memo_t, self._memo = None, {}
        self._rays()                  # build the ray table now: startup has no tick deadline
        self.views = {}               # enemy id -> Viewshed
        self.seen_by = {}             # K -> tuple of enemy ids
        self.likely_map = {}          # K -> (value, t)
        self.expo_map = {}            # K -> (value, t)
        self.enemy_cells = {}         # (i, j) -> (t, x, y)
        self.seen_at = {}             # (i, j) on the 32 m explore grid -> last time it was in view
        self._seen_t = -1e9
        self.work = 0
        self._blob = {}

    # ------------------------------------------------------------ update
    def update(self, mem):
        t = mem.t
        self.t = t
        me = mem.me
        px, py = me["pos"][0], me["pos"][1]
        self.dump_xy = (px, py)
        self._viewsheds(mem, px, py)
        self._likely(mem, px, py)
        self._exposure(mem, px, py)
        if t - self._seen_t >= SEEN_EVERY:
            self._seen_t = t
            self._mark_seen(me)
        for tr in mem.enemies():
            c = (int((tr.pos[0] - self.wm.x0) // HUNT_CELL), int((tr.pos[1] - self.wm.y0) // HUNT_CELL))
            self.enemy_cells[c] = (t, tr.pos[0], tr.pos[1])
        if int(t * 2) != int((t - 0.1) * 2):
            for m, tau in ((self.likely_map, LIKELY_TAU), (self.expo_map, EXPO_TAU)):
                for K in [K for K, (v, ts) in m.items() if v * math.exp(-(t - ts) / tau) < 0.02]:
                    del m[K]

    # ------------------------------------------------------------ viewsheds (ray sweep)
    def _rays(self):
        """Per ray, the fine-grid cells it crosses as (di, dj, d), relative to the origin cell.
        Built once: snapping each enemy to its 2 m fine cell lets every viewshed reuse them."""
        if self._ray_cache is None:
            C = self.wm.C
            rays = []
            for r in range(N_RAYS):
                a = 2 * math.pi * r / N_RAYS
                ca, sa = math.cos(a), math.sin(a)
                out, last = [], None
                k = 1
                while k * C <= VIEW_R:
                    d = k * C
                    di, dj = int(round(ca * d / C)), int(round(sa * d / C))
                    if (di, dj) != last:
                        out.append((di, dj, d))
                        last = (di, dj)
                    k += 1
                rays.append(out)
            self._ray_cache = rays
        return self._ray_cache

    def _sweep(self, v, budget):
        """Advance v's running sweep by up to `budget` samples. Horizon rule along each ray: a
        cell is visible if the slope to a mech's eye there beats every obstacle slope before it.
        Buildings and terrain block; tree crowns are not in `top`, so they do not."""
        wm = self.wm
        top, nx, ny, nnx = wm.top, wm.nx, wm.ny, wm.nnx
        nblock, nground = wm.nblock, wm.nground
        oi, oj, oz = v.job_org
        rays = self._rays()
        cells = v.job
        used = 0
        r = v.job_i
        while r < N_RAYS and used < budget:
            hmax = -1e9
            for di, dj, d in rays[r]:
                i, j = oi + di, oj + dj
                if i < 0 or j < 0 or i >= nx or j >= ny:
                    break
                K = (j >> 1) * nnx + (i >> 1)
                if not nblock[K] and (nground[K] + EYE - oz) / d >= hmax:
                    cells.add(K)
                s = (top[j * nx + i] - oz) / d
                if s > hmax:
                    hmax = s
            used += len(rays[r])
            r += 1
        v.job_i = r
        return used

    def _viewsheds(self, mem, px, py):
        t = self.t
        wm = self.wm
        live = {}
        for tr in mem.recent_mechs(ENEMY_MEMORY_S):
            d = math.hypot(tr.pos[0] - px, tr.pos[1] - py)
            if d < KEEP_R:
                live[tr.id] = (d, tr)
        for tid in [k for k in self.views if k not in live]:
            self._swap_cells(tid, self.views[tid].cells, set())
            del self.views[tid]
        budget = SAMPLE_BUDGET
        # nearest first: the mech closest to us matters most when the budget runs out
        for tid, (d, tr) in sorted(live.items(), key=lambda kv: kv[1][0]):
            if budget <= 0:
                break
            v = self.views.get(tid)
            if v is None:
                v = self.views[tid] = Viewshed()
            ex, ey = tr.pos[0], tr.pos[1]
            if v.job is None:
                if not (v.org is None or t - v.t > REFRESH_AGE
                        or math.hypot(ex - v.org[0], ey - v.org[1]) > REFRESH_MOVE):
                    continue
                oi, oj = int((ex - wm.x0) * wm.inv), int((ey - wm.y0) * wm.inv)
                v.job, v.job_i, v.job_org = set(), 0, (oi, oj, tr.pos[2] + EYE)
                v.job_xy = (ex, ey)
            budget -= self._sweep(v, budget)
            if v.job_i >= N_RAYS:
                self._swap_cells(tid, v.cells, v.job)
                v.cells, v.org, v.t = v.job, v.job_xy, t
                v.job = None
        self.work = SAMPLE_BUDGET - budget

    def _swap_cells(self, tid, old, new):
        """Update seen_by for one enemy's viewshed changing from `old` to `new`: only the cells
        that differ are touched (a 6 m move changes a few hundred, not thousands)."""
        sb = self.seen_by
        for K in old - new:
            ids = tuple(i for i in sb.get(K, ()) if i != tid)
            if ids:
                sb[K] = ids
            else:
                sb.pop(K, None)
        for K in new - old:
            sb[K] = sb.get(K, ()) + (tid,)

    # ------------------------------------------------------------ decaying layers
    def _blob_offsets(self, R):
        b = self._blob.get(R)
        if b is None:
            N = self.wm.N
            rc = int(R / N)
            b = []
            for dx in range(-rc, rc + 1):
                for dy in range(-rc, rc + 1):
                    d = math.hypot(dx * N, dy * N)
                    if d <= R:
                        b.append((dx * N, dy * N, (1.0 - d / R) ** 2))
            self._blob[R] = b
        return b

    def _deposit(self, m, tau, x, y, w, R=12.0, cap=5.0):
        for ox, oy, g in self._blob_offsets(R):
            K = self.wm.nidx(x + ox, y + oy)
            if K < 0:
                continue
            e = m.get(K)
            v = e[0] * math.exp(-(self.t - e[1]) / tau) if e else 0.0
            m[K] = (min(cap, v + w * g), self.t)

    @staticmethod
    def _read(m, tau, K, t):
        e = m.get(K)
        return e[0] * math.exp(-(t - e[1]) / tau) if e else 0.0

    def likely(self, K):
        return self._read(self.likely_map, LIKELY_TAU, K, self.t)

    def expo(self, K):
        return self._read(self.expo_map, EXPO_TAU, K, self.t)

    def _likely(self, mem, px, py):
        for tr in mem.lost:
            self._deposit(self.likely_map, LIKELY_TAU, tr.pos[0] + tr.vel[0], tr.pos[1] + tr.vel[1], 2.0)
        for h in mem.hits:
            t, by, b, weapon, _ = h
            if t != self.t or b is None or weapon in ("zone", "npc"):
                continue
            tr = mem.tracks.get(by)
            if tr is not None and tr.visible:
                continue
            rng = {"gun": 120.0, "laser": 150.0, "mortar": 140.0, "lightning": 30.0}.get(weapon, 100.0)
            for d in (0.5 * rng, rng, 1.5 * rng):
                self._deposit(self.likely_map, LIKELY_TAU, px + math.cos(b) * d, py + math.sin(b) * d, 0.8)
        visible = [tr for tr in mem.enemies() if tr.visible]
        for s in mem.sounds:
            self._sound(s, px, py, visible)

    def _sound(self, s, px, py, visible):
        kind = s.get("kind", "")
        w = SOUND_W.get(kind, 0.5)
        if w <= 0:
            return
        b = s["bearing"]
        carry = self.carry.get(kind, 300.0)
        band = s.get("dist", "far")
        if band == "near":
            lo, hi = 4.0, min(self.near_d, carry)
        elif band == "mid":
            lo, hi = self.near_d, min(self.mid_d, carry)
        else:
            lo, hi = self.mid_d, carry
        hi = max(hi, lo + 10.0)
        for tr in visible:          # a mech we can see explains it: nothing new
            dx, dy = tr.pos[0] - px, tr.pos[1] - py
            d = math.hypot(dx, dy)
            if lo - 15 <= d <= hi + 15 and abs(angdiff(math.atan2(dy, dx), b)) < 2.5 * self.noise + 0.05:
                return
        pts = []
        step = max(6.0, (hi - lo) / 10.0)
        d = lo + step * 0.5
        while d < hi:
            for k, gw in ((-1.5, 0.32), (-0.75, 0.75), (0.0, 1.0), (0.75, 0.75), (1.5, 0.32)):
                a = b + k * self.noise
                pts.append((px + math.cos(a) * d, py + math.sin(a) * d, gw))
            d += step
        tot = sum(p[2] for p in pts) or 1.0
        scale = w * max(1.0, len(pts) / 12.0) / tot * 0.35
        for x, y, gw in pts:
            self._deposit(self.likely_map, LIKELY_TAU, x, y, gw * scale)

    def _exposure(self, mem, px, py):
        K = self.wm.nidx(px, py)
        watched = len(self.seen_by.get(K, ()))
        hit = any(h[0] == self.t and h[3] not in ("zone",) for h in mem.hits)
        if watched or hit:
            self._deposit(self.expo_map, EXPO_TAU, px, py, 0.08 * watched + (0.5 if hit else 0.0), R=8.0)

    # ------------------------------------------------------------ queries
    def route_cost(self, K, ignore=None):
        """Extra cost of stepping into K (a plain step costs 1). `ignore`: an enemy whose sight
        we accept (the one we are fighting). Memoised per tick: A* asks for the same cells often."""
        if self._memo_t != self.t:
            self._memo_t, self._memo = self.t, {}
        key = (K, ignore)
        c = self._memo.get(key)
        if c is None:
            c = self._memo[key] = self._route_cost(K, ignore)
        return c

    def _route_cost(self, K, ignore):
        ids = self.seen_by.get(K, ())
        n = len(ids) - (1 if ignore is not None and ignore in ids else 0)
        return 3.0 * n + 1.2 * min(self.likely(K), 3.0) + 0.6 * min(self.expo(K), 3.0)

    def hotspot(self, x, y, r=150.0, min_v=0.35):
        """Hottest likely-enemy cell within r of (x, y): (cx, cy, value) or None."""
        best, bv = None, min_v
        r2 = r * r
        for K in self.likely_map:
            v = self.likely(K)
            if v > bv:
                cx, cy = self.wm.ncenter(K)
                if (cx - x) ** 2 + (cy - y) ** 2 < r2:
                    best, bv = (cx, cy, v), v
        return best

    def _mark_seen(self, me):
        """Credit explore cells we can see now: everything within 45 m, plus cells in the 120 deg
        view cone out to 110 m whose centre has a clear sight line (crowns count)."""
        wm = self.wm
        x, y, z = me["pos"]
        eye = (x, y, z + EYE)
        yaw = me["aim"][0]
        C = SEEN_CELL
        i0, j0 = int((x - wm.x0) // C), int((y - wm.y0) // C)
        r = int(SEEN_FAR // C) + 1
        for i in range(i0 - r, i0 + r + 1):
            for j in range(j0 - r, j0 + r + 1):
                cx, cy = wm.x0 + (i + 0.5) * C, wm.y0 + (j + 0.5) * C
                d = math.hypot(cx - x, cy - y)
                if d <= SEEN_NEAR:
                    self.seen_at[(i, j)] = self.t
                elif d <= SEEN_FAR and abs(angdiff(math.atan2(cy - y, cx - x), yaw)) < math.radians(60):
                    if wm.los(eye, (cx, cy, wm.ground_at(cx, cy) + 2.0)):
                        self.seen_at[(i, j)] = self.t

    def explore_goal(self, x, y, zone, banned):
        """Ground we have gone longest without seeing: lowest distance - 0.15 x age (age capped
        at 120 s, never seen = 120) + 0.15 x distance from the next zone centre, inside the zone,
        on a walkable cell. (x, y) or None."""
        wm = self.wm
        C = SEEN_CELL
        zx, zy = zone["center"]
        zr = zone["radius"] - 20.0
        nx, ny = zone["next_center"]
        best, bs = None, 1e18
        i0, j0 = int((zx - zr - wm.x0) // C), int((zy - zr - wm.y0) // C)
        n = int(2 * zr // C) + 2
        for i in range(i0, i0 + n):
            for j in range(j0, j0 + n):
                cx, cy = wm.x0 + (i + 0.5) * C, wm.y0 + (j + 0.5) * C
                if math.hypot(cx - zx, cy - zy) > zr:
                    continue
                age = min(120.0, self.t - self.seen_at.get((i, j), -1e9))
                if age < 20.0:
                    continue
                d = math.hypot(cx - x, cy - y)
                s = d - 0.15 * age + 0.15 * math.hypot(cx - nx, cy - ny)
                if s >= bs or banned(cx, cy):
                    continue
                K = wm.nearest_open(cx, cy, 4)
                if K < 0:
                    continue
                bs, best = s, wm.ncenter(K)
        return best

    def hunt_goal(self, x, y, zone, max_age=45.0):
        """Where to go looking for the next fight: the best recent enemy cell (distance + 3 m per
        second of age, 40-300 m away, inside the zone), else the likely-enemy hotspot within
        200 m. (x, y, kind) or None."""
        zx, zy = zone["center"]
        zr = zone["radius"] - 10.0
        best, bs = None, 1e18
        for cx, cy, age in self.enemy_cell_list(max_age):
            d = math.hypot(cx - x, cy - y)
            if not 40.0 <= d <= 300.0 or math.hypot(cx - zx, cy - zy) > zr:
                continue
            s = d + 3.0 * age
            if s < bs:
                bs, best = s, (cx, cy, "seen")
        if best is None:
            h = self.hotspot(x, y, 200.0, HEARD_MIN)
            if h is not None and math.hypot(h[0] - x, h[1] - y) > 25.0:
                best = (h[0], h[1], "heard")
        return best

    def enemy_cell_list(self, max_age):
        out = []
        for (i, j), (ts, _, _) in list(self.enemy_cells.items()):
            age = self.t - ts
            if age <= max_age:
                out.append((self.wm.x0 + (i + 0.5) * HUNT_CELL, self.wm.y0 + (j + 0.5) * HUNT_CELL, age))
        return out

    def _near_ok(self, K, r=170.0):
        x, y = self.wm.ncenter(K)
        return (x - self.dump_xy[0]) ** 2 + (y - self.dump_xy[1]) ** 2 < r * r

    def dump(self):
        t = self.t
        near = self._near_ok
        seen = [[K, len(ids)] for K, ids in self.seen_by.items() if near(K)]
        likely = [[K, round(v, 2)] for K in self.likely_map if (v := self.likely(K)) > 0.05]
        expo = [[K, round(v, 2)] for K in self.expo_map if (v := self.expo(K)) > 0.05]
        cost = [[K, round(c, 1)] for K in set(self.seen_by) | set(self.likely_map) | set(self.expo_map)
                if near(K) and (c := self.route_cost(K)) > 0.1]
        cells = [[round(x), round(y), round(a)] for x, y, a in self.enemy_cell_list(120.0)]
        return {"seen": seen, "likely": likely, "expo": expo, "cost": cost, "ecells": cells,
                "cell_m": HUNT_CELL, "work": self.work}
