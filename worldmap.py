"""Top-down model of the static map: ground height, building heights, line of sight, paths, cover.

Built once from /arena/v2/world.json + terrain.i16 during the hello budget (20 s).
Two grids:
  fine (2 m): ground height and "block height" (max of ground and any building/stadium top)
              -> used for line of sight of shots and vision.
  nav (4 m):  walkable or not, canopy (tree crowns), roof height -> used for paths, cover, heat.
Bridges are ignored (2 m slabs you can walk and shoot under).
"""
import array
import heapq
import json
import math
import os
import time

SQRT2 = math.sqrt(2.0)


class WorldMap:
    def __init__(self, map_dir, step_up=1.2, fine=2.0, nav=4.0):
        t0 = time.time()
        with open(os.path.join(map_dir, "v2", "world.json")) as f:
            w = json.load(f)
        b = w["bounds"]
        self.x0, self.y0, self.x1, self.y1 = b["minX"], b["minY"], b["maxX"], b["maxY"]
        self._load_terrain(os.path.join(map_dir, "v2", w["terrain"]["file"]), w["terrain"])

        # ---- fine grid
        self.C = C = fine
        self.inv = 1.0 / C
        self.nx = nx = int(math.ceil((self.x1 - self.x0) / C))
        self.ny = ny = int(math.ceil((self.y1 - self.y0) / C))
        n = nx * ny
        ground = array.array("f", bytes(4 * n))
        for j in range(ny):
            y = self.y0 + (j + 0.5) * C
            row = j * nx
            for i in range(nx):
                ground[row + i] = self.ground_at(self.x0 + (i + 0.5) * C, y)
        self.ground = ground
        self.top = array.array("f", ground)
        self.prisms = []
        for p in w.get("prisms", []):
            if p.get("kind") == "bridge":
                continue
            rings = [p["ring"]] + list(p.get("holes") or [])
            cells = self._raster(rings)
            top = float(p["top"])
            for idx in cells:
                if top > self.top[idx]:
                    self.top[idx] = top
            self.prisms.append((p, cells))
        solid = bytearray(n)
        for idx in range(n):
            if self.top[idx] - ground[idx] > step_up:
                solid[idx] = 1
        self.solid = solid

        # ---- nav grid (each nav cell = 2x2 fine cells)
        self.N = N = nav
        self.ninv = 1.0 / N
        self.nnx = nnx = int(math.ceil((self.x1 - self.x0) / N))
        self.nny = nny = int(math.ceil((self.y1 - self.y0) / N))
        k = int(round(N / C))
        nblock = bytearray(nnx * nny)
        nground = array.array("f", bytes(4 * nnx * nny))
        nroof = array.array("f", bytes(4 * nnx * nny))
        for J in range(nny):
            for I in range(nnx):
                blocked = 0
                g = 0.0
                cnt = 0
                rt = -1e9
                for dj in range(k):
                    j = J * k + dj
                    if j >= ny:
                        continue
                    for di in range(k):
                        i = I * k + di
                        if i >= nx:
                            continue
                        idx = j * nx + i
                        g += ground[idx]
                        cnt += 1
                        if solid[idx]:
                            blocked = 1
                            if self.top[idx] > rt:
                                rt = self.top[idx]
                K = J * nnx + I
                nground[K] = g / cnt if cnt else 0.0
                nblock[K] = blocked
                nroof[K] = rt if blocked else 0.0
        # map edge is a wall: keep a 1-cell border blocked
        for I in range(nnx):
            nblock[I] = 1
            nblock[(nny - 1) * nnx + I] = 1
        for J in range(nny):
            nblock[J * nnx] = 1
            nblock[J * nnx + nnx - 1] = 1
        self.nblock, self.nground, self.nroof = nblock, nground, nroof

        # ---- canopy (tree crowns) on the nav grid: concealment only, never blocks shots
        canopy = bytearray(nnx * nny)
        tr = w.get("trees") or {}
        fields = tr.get("fields") or []
        data = tr.get("data") or []
        if fields and data:
            F = len(fields)
            fi = {name: q for q, name in enumerate(fields)}
            for s in range(0, len(data) - F + 1, F):
                cx = data[s + fi.get("crownX", fi["x"])]
                cy = data[s + fi.get("crownY", fi["y"])]
                r = data[s + fi["crownR"]] if "crownR" in fi else 3.0
                for J in range(int((cy - r - self.y0) * self.ninv), int((cy + r - self.y0) * self.ninv) + 1):
                    for I in range(int((cx - r - self.x0) * self.ninv), int((cx + r - self.x0) * self.ninv) + 1):
                        if 0 <= I < nnx and 0 <= J < nny:
                            px = self.x0 + (I + 0.5) * N
                            py = self.y0 + (J + 0.5) * N
                            if (px - cx) ** 2 + (py - cy) ** 2 <= r * r:
                                canopy[J * nnx + I] = 1
        self.canopy = canopy

        self.roof_spots = self._find_roof_spots()
        self.cover_cache = {}
        self.build_s = time.time() - t0

    # ------------------------------------------------------------ terrain
    def _load_terrain(self, path, t):
        self.tx0, self.ty0, self.tstep = t["minX"], t["minY"], t["step"]
        self.tnx, self.tny, self.tscale = t["nx"], t["ny"], t["scale"]
        a = array.array("h")
        with open(path, "rb") as f:
            a.frombytes(f.read())
        if array.array("h", [1]).tobytes() != b"\x01\x00":
            a.byteswap()
        self.terrain = a

    def ground_at(self, x, y):
        fx = (x - self.tx0) / self.tstep
        fy = (y - self.ty0) / self.tstep
        i = int(math.floor(fx))
        j = int(math.floor(fy))
        nx, ny = self.tnx, self.tny
        i = 0 if i < 0 else (nx - 2 if i > nx - 2 else i)
        j = 0 if j < 0 else (ny - 2 if j > ny - 2 else j)
        u = min(1.0, max(0.0, fx - i))
        v = min(1.0, max(0.0, fy - j))
        T = self.terrain
        k = j * nx + i
        h = (T[k] * (1 - u) + T[k + 1] * u) * (1 - v) + (T[k + nx] * (1 - u) + T[k + nx + 1] * u) * v
        return h * self.tscale

    # ------------------------------------------------------------ rasterize
    def _raster(self, rings):
        edges = []
        ymin, ymax, xmin, xmax = 1e9, -1e9, 1e9, -1e9
        for ring in rings:
            pts = [(ring[q], ring[q + 1]) for q in range(0, len(ring) - 1, 2)]
            for q in range(len(pts)):
                a = pts[q]
                c = pts[(q + 1) % len(pts)]
                edges.append((a[0], a[1], c[0], c[1]))
                ymin, ymax = min(ymin, a[1]), max(ymax, a[1])
                xmin, xmax = min(xmin, a[0]), max(xmax, a[0])
        C, x0, y0, nx, ny = self.C, self.x0, self.y0, self.nx, self.ny
        out = []
        j0 = max(0, int((ymin - y0) / C) - 1)
        j1 = min(ny - 1, int((ymax - y0) / C) + 1)
        for j in range(j0, j1 + 1):
            yc = y0 + (j + 0.5) * C
            xs = []
            for (xa, ya, xb, yb) in edges:
                if (ya <= yc < yb) or (yb <= yc < ya):
                    xs.append(xa + (yc - ya) * (xb - xa) / (yb - ya))
            xs.sort()
            for q in range(0, len(xs) - 1, 2):
                i0 = max(0, int(math.ceil((xs[q] - x0) / C - 0.5)))
                i1 = min(nx - 1, int(math.floor((xs[q + 1] - x0) / C - 0.5)))
                row = j * nx
                for i in range(i0, i1 + 1):
                    out.append(row + i)
        return out

    def _find_roof_spots(self):
        """One standing spot per building prism top (centre-most cell) plus its height."""
        spots = []
        for p, cells in self.prisms:
            if p.get("kind") != "building" or len(cells) < 12:
                continue
            top = float(p["top"])
            sx = sy = 0.0
            for idx in cells:
                sx += idx % self.nx
                sy += idx // self.nx
            ci, cj = sx / len(cells), sy / len(cells)
            best, bd = None, 1e18
            for idx in cells:
                i, j = idx % self.nx, idx // self.nx
                if abs(self.top[idx] - top) > 0.5:
                    continue  # covered by a taller prism
                d = (i - ci) ** 2 + (j - cj) ** 2
                if d < bd:
                    bd, best = d, idx
            if best is None:
                continue
            x = self.x0 + (best % self.nx + 0.5) * self.C
            y = self.y0 + (best // self.nx + 0.5) * self.C
            h = top - self.ground[best]
            spots.append((x, y, top, h))
        return spots

    # ------------------------------------------------------------ queries
    def fidx(self, x, y):
        i = int((x - self.x0) * self.inv)
        j = int((y - self.y0) * self.inv)
        if 0 <= i < self.nx and 0 <= j < self.ny:
            return j * self.nx + i
        return -1

    def nidx(self, x, y):
        I = int((x - self.x0) * self.ninv)
        J = int((y - self.y0) * self.ninv)
        if 0 <= I < self.nnx and 0 <= J < self.nny:
            return J * self.nnx + I
        return -1

    def ncenter(self, K):
        return (self.x0 + (K % self.nnx + 0.5) * self.N, self.y0 + (K // self.nnx + 0.5) * self.N)

    def surface(self, x, y):
        k = self.fidx(x, y)
        return self.top[k] if k >= 0 else 0.0

    def is_solid(self, x, y):
        k = self.fidx(x, y)
        return k < 0 or self.solid[k] == 1

    def los(self, a, b, step=1.5):
        """True if nothing static (terrain, buildings) blocks the segment a->b (3D points)."""
        ax, ay, az = a
        dx, dy, dz = b[0] - ax, b[1] - ay, b[2] - az
        d = math.hypot(dx, dy)
        if d < 0.5:
            return True
        n = int(d / step) + 1
        top, nx, ny, x0, y0, inv = self.top, self.nx, self.ny, self.x0, self.y0, self.inv
        for q in range(1, n):
            f = q / n
            i = int((ax + dx * f - x0) * inv)
            j = int((ay + dy * f - y0) * inv)
            if 0 <= i < nx and 0 <= j < ny and top[j * nx + i] > az + dz * f:
                return False
        return True

    def walk_clear(self, ax, ay, bx, by):
        """Straight ground walk a->b crosses no blocked nav cell."""
        dx, dy = bx - ax, by - ay
        d = math.hypot(dx, dy)
        n = int(d / (self.N * 0.5)) + 1
        nb = self.nblock
        for q in range(1, n + 1):
            f = q / n
            K = self.nidx(ax + dx * f, ay + dy * f)
            if K < 0 or nb[K]:
                return False
        return True

    def cover(self, x, y):
        """0..1: fraction of 16 directions with a wall within ~10 m (cached per nav cell)."""
        K = self.nidx(x, y)
        c = self.cover_cache.get(K)
        if c is not None:
            return c
        hits = 0
        for q in range(16):
            a = q * math.pi / 8
            ca, sa = math.cos(a), math.sin(a)
            for r in (3.5, 6.5, 10.0):
                if self.is_solid(x + ca * r, y + sa * r):
                    hits += 1
                    break
        c = hits / 16.0
        self.cover_cache[K] = c
        return c

    def nearest_open(self, x, y, max_r=6):
        K = self.nidx(x, y)
        if K >= 0 and not self.nblock[K]:
            return K
        I0, J0 = int((x - self.x0) * self.ninv), int((y - self.y0) * self.ninv)
        for r in range(1, max_r + 1):
            best, bd = -1, 1e18
            for J in range(J0 - r, J0 + r + 1):
                for I in range(I0 - r, I0 + r + 1):
                    if 0 <= I < self.nnx and 0 <= J < self.nny:
                        K = J * self.nnx + I
                        if not self.nblock[K]:
                            d = (I - I0) ** 2 + (J - J0) ** 2
                            if d < bd:
                                bd, best = d, K
            if best >= 0:
                return best
        return -1

    def path(self, sx, sy, gx, gy, budget_ms=8.0, avoid=None):
        """Weighted A* on the nav grid. Returns world waypoints (smoothed), possibly partial.
        `avoid(K) -> extra cost` lets callers steer around exposed cells."""
        S = self.nearest_open(sx, sy)
        G = self.nearest_open(gx, gy)
        if S < 0 or G < 0:
            return [(gx, gy)]
        if S == G:
            return [(gx, gy)]
        nnx, nb, ng = self.nnx, self.nblock, self.nground
        gxI, gyJ = G % nnx, G // nnx
        W = 1.6
        deadline = time.perf_counter() + budget_ms / 1000.0

        def h(K):
            dx = abs(K % nnx - gxI)
            dy = abs(K // nnx - gyJ)
            return (dx + dy) + (SQRT2 - 2) * min(dx, dy)

        openh = [(W * h(S), 0.0, S)]
        gcost = {S: 0.0}
        came = {S: -1}
        best, besth = S, h(S)
        nbrs = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                (1, 1, SQRT2), (1, -1, SQRT2), (-1, 1, SQRT2), (-1, -1, SQRT2))
        found = False
        it = 0
        while openh:
            f, g, K = heapq.heappop(openh)
            if K == G:
                found = True
                break
            if g > gcost.get(K, 1e18):
                continue
            it += 1
            if (it & 63) == 0 and time.perf_counter() > deadline:
                break
            I, J = K % nnx, K // nnx
            for dI, dJ, c in nbrs:
                I2, J2 = I + dI, J + dJ
                K2 = J2 * nnx + I2
                if I2 < 0 or J2 < 0 or I2 >= nnx or K2 >= len(nb) or nb[K2]:
                    continue
                if dI and dJ and (nb[J * nnx + I2] or nb[J2 * nnx + I]):
                    continue  # no corner cutting
                dz = abs(ng[K2] - ng[K])
                if dz > 4.0:
                    continue  # too steep
                g2 = g + c + dz * 0.3
                if avoid is not None:
                    g2 += avoid(K2)
                if g2 < gcost.get(K2, 1e18):
                    gcost[K2] = g2
                    came[K2] = K
                    hh = h(K2)
                    if hh < besth:
                        besth, best = hh, K2
                    heapq.heappush(openh, (g2 + W * hh, g2, K2))
        end = G if found else best
        cells = []
        K = end
        while K != -1:
            cells.append(K)
            K = came.get(K, -1)
        cells.reverse()
        pts = [self.ncenter(K) for K in cells]
        if found:
            pts[-1] = (gx, gy)
        # string pulling
        out = []
        cur = (sx, sy)
        q = 0
        while q < len(pts):
            far = q
            for r in range(len(pts) - 1, q, -1):
                if r - q > 40:
                    continue
                if self.walk_clear(cur[0], cur[1], pts[r][0], pts[r][1]):
                    far = r
                    break
            out.append(pts[far])
            cur = pts[far]
            q = far + 1
        return out or [(gx, gy)]
