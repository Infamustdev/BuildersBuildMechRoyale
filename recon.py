"""What we have and haven't looked at lately.

Items are the whole economy -- cubes are +4% HP and damage each, weapons decide fights -- and
the server lists a crate, cube or pickup *only while it is in our line of sight*. Nothing about
where they are can be precomputed: they spawn randomly every match. So ground we haven't looked
at recently is the only place new items can come from, and its value grows the longer we ignore
it. That is the positive reason to keep moving, and it is why standing on a "safe" spot can
never be the best option for long: everything we can see from there goes stale except the little
circle we are sitting in.

The grid is coarse (40 m) and refreshed a slice at a time, so it costs a fraction of a ms a tick.
"""
import math

from tracker import angdiff


class Recon:
    def __init__(self, wm, cell=40.0):
        self.wm = wm
        self.C = cell
        self.nx = max(1, int(math.ceil((wm.x1 - wm.x0) / cell)))
        self.ny = max(1, int(math.ceil((wm.y1 - wm.y0) / cell)))
        n = self.nx * self.ny
        self.seen = [-1e9] * n          # last time we had line of sight to this cell
        self.cx = [0.0] * n             # cell centres, precomputed
        self.cy = [0.0] * n
        self.cz = [0.0] * n
        for K in range(n):
            x = wm.x0 + (K % self.nx + 0.5) * cell
            y = wm.y0 + (K // self.nx + 0.5) * cell
            self.cx[K], self.cy[K] = x, y
            self.cz[K] = wm.surface(x, y) + 2.0
        self.cur = 0

    def update(self, t, pos, yaw, rng, fov_deg, budget=16):
        """Refresh a slice of the grid: cells we can see right now are marked seen at t.

        Round-robin rather than all at once -- a slice is cheap, and because we spin while
        scanning, every direction comes round soon enough.
        """
        n = self.nx * self.ny
        eye = (pos[0], pos[1], pos[2] + 8.0)
        half = math.radians(fov_deg) * 0.5
        rng2 = rng * rng
        los = self.wm.los
        for _ in range(budget):
            K = self.cur
            self.cur = (self.cur + 1) % n
            dx, dy = self.cx[K] - eye[0], self.cy[K] - eye[1]
            if dx * dx + dy * dy > rng2:
                continue
            if abs(angdiff(math.atan2(dy, dx), yaw)) > half:
                continue
            if los(eye, (self.cx[K], self.cy[K], self.cz[K]), step=4.0):
                self.seen[K] = t

    def stale(self, t, px, py, tau, want=6, max_d=320.0):
        """The `want` cells most worth going to look at: stalest first, nearest to break ties.

        Returns (x, y, freshness-deficit in 0..1). A cell we are standing in is fresh, so it
        scores 0 -- which is exactly why arriving somewhere stops being a reason to stay.
        """
        out = []
        for K in range(self.nx * self.ny):
            d = math.hypot(self.cx[K] - px, self.cy[K] - py)
            if d > max_d:
                continue
            frac = min(1.0, (t - self.seen[K]) / tau)
            if frac < 0.05:
                continue
            out.append((frac - 0.0012 * d, self.cx[K], self.cy[K], frac))
        out.sort(key=lambda q: -q[0])
        return [(x, y, f) for (_, x, y, f) in out[:want]]
