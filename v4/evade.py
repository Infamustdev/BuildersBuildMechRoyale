"""Evasion moves the legs take ahead of (or blend into) everything else.

- escape : trapped in a walled pocket off the main walkable area -> walk the route to a launch
           cell, wait for 70% energy, jump and jet over the wall to the landing cell; still in the
           pocket after 12 s -> give up for 30 s and let normal movement drive
- shell  : a mortar shell will land within splash + our radius + 2 m in under 2.5 s -> step away
           (first walkable heading of 0, +-0.7, +-1.4 rad off "straight away")
- kite   : no lightning of our own and a lightning carrier inside its range + 6 m -> score seven
           headings 8 m out: 25 x cos(turn) - zone penalty - 35 per other enemy that can see the
           landing cell if it is charging a laser, 18 otherwise (seen_by layer); boost inside its
           range + 1 m
- dodge  : a round that passes within our radius + 1.1 m in the next 0.7 s -> sidestep across its
           path; a mech charging a laser aimed within 0.15 rad of us -> strafe; blended into the
           chosen move as 0.4 x move + 0.9 x dodge
"""
import math
from collections import deque

ESC_GIVE_UP_S = 12.0      # still in the pocket after this long: stop trying...
ESC_BAN_S = 30.0          # ...for this long

from memory import angdiff


class Evade:
    def __init__(self, wm, cfg):
        self.wm = wm
        w = cfg.get("weapons", {})
        self.light_range = float(w.get("lightning", {}).get("range", 45.0))
        self.mortar_g = float(w.get("mortar", {}).get("gravity", 22.0))
        self.splash = float(w.get("mortar", {}).get("splash_radius", 7.0))
        mc = cfg.get("mech", {})
        self.mech_r = float(mc.get("radius", 2.5))
        self.mech_h = float(mc.get("height", 9.0))
        self.comp, self.main = self._components()
        self.esc = None           # (launch (x, y), land (x, y)) while escaping
        self.esc_t = -1e9
        self.flying = False
        self.esc_path = []
        self.esc_since = None     # when we first found ourselves in this pocket
        self.esc_ban_until = -1e9

    # ------------------------------------------------------------ walkable components
    def _components(self):
        """Label the nav grid's walkable components (same step rule as A*); startup only."""
        wm = self.wm
        nnx, nny, nb, ng = wm.nnx, wm.nny, wm.nblock, wm.nground
        comp = [-1] * (nnx * nny)
        sizes = []
        for K0 in range(nnx * nny):
            if nb[K0] or comp[K0] >= 0:
                continue
            c = len(sizes)
            comp[K0] = c
            q = deque([K0])
            n = 0
            while q:
                K = q.popleft()
                n += 1
                I, J = K % nnx, K // nnx
                for dI, dJ in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    I2, J2 = I + dI, J + dJ
                    if 0 <= I2 < nnx and 0 <= J2 < nny:
                        K2 = J2 * nnx + I2
                        if not nb[K2] and comp[K2] < 0 and abs(ng[K2] - ng[K]) <= 4.0:
                            comp[K2] = c
                            q.append(K2)
            sizes.append(n)
        main = max(range(len(sizes)), key=lambda i: sizes[i]) if sizes else -1
        return comp, main

    def _plan_escape(self, x, y):
        """Nearest cell of our pocket that has a main-area cell within 12 m: (launch, land)."""
        wm = self.wm
        K0 = wm.nearest_open(x, y)
        if K0 < 0:
            return None
        nnx, nny, comp, main = wm.nnx, wm.nny, self.comp, self.main
        c = comp[K0]
        seen = {K0}
        q = deque([K0])
        while q:
            K = q.popleft()
            I, J = K % nnx, K // nnx
            best, bd = None, 1e9
            for dI in range(-3, 4):
                for dJ in range(-3, 4):
                    I2, J2 = I + dI, J + dJ
                    if 0 <= I2 < nnx and 0 <= J2 < nny and comp[J2 * nnx + I2] == main:
                        d = dI * dI + dJ * dJ
                        if d < bd:
                            bd, best = d, J2 * nnx + I2
            if best is not None:
                return wm.ncenter(K), wm.ncenter(best)
            for dI, dJ in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                K2 = (J + dJ) * nnx + (I + dI)
                if 0 <= K2 < len(comp) and K2 not in seen and comp[K2] == c:
                    seen.add(K2)
                    q.append(K2)
        return None

    def escape(self, ctx):
        """(move, boost, jump) or None."""
        wm, me, t = self.wm, ctx.me, ctx.t
        x, y, z = me["pos"]
        energy, max_e = float(me.get("energy", 0.0)), float(me.get("max_energy", 100.0))
        if t < self.esc_ban_until:
            return None
        if me.get("grounded", True):
            self.flying = False
            K = wm.nidx(x, y)
            if K < 0 or wm.nblock[K] or z - wm.nground[K] > 3.0 or self.comp[K] == self.main or self.comp[K] < 0:
                self.esc = None
                self.esc_since = None
                return None
            if self.esc_since is None:
                self.esc_since = t
            elif t - self.esc_since > ESC_GIVE_UP_S:
                # still in the pocket: it is probably not a pocket (our grid misses a ramp) or
                # the jump keeps failing. Let normal movement have the legs for a while.
                self.esc, self.esc_since = None, None
                self.esc_ban_until = t + ESC_BAN_S
                return None
            if self.esc is None or t - self.esc_t > 6.0:
                self.esc, self.esc_t = self._plan_escape(x, y), t
                if self.esc is None:
                    return None
                lx, ly = self.esc[0]
                self.esc_path = wm.path(x, y, lx, ly, budget_nodes=1800)
            (lx, ly), _ = self.esc
            if math.hypot(lx - x, ly - y) > 3.0:
                # walk the route to the launch cell, not a straight line into a wall
                while len(self.esc_path) > 1 and math.hypot(self.esc_path[0][0] - x, self.esc_path[0][1] - y) < 3.0:
                    self.esc_path.pop(0)
                wx, wy = self.esc_path[0] if self.esc_path else (lx, ly)
                d = math.hypot(wx - x, wy - y) or 1.0
                return ((wx - x) / d, (wy - y) / d), False, False
            if energy < min(70.0, max_e * 0.7):
                return (0.0, 0.0), False, False
            self.flying = True
            jump = True
        elif not self.flying or self.esc is None:
            return None
        else:
            jump = False
        ax, ay = self.esc[1]
        dx, dy = ax - x, ay - y
        d = math.hypot(dx, dy)
        if d < 1.5:
            return (0.0, 0.0), False, jump
        mx, my = dx / d, dy / d
        need = max(wm.surface(x + mx * k, y + my * k) for k in range(int(min(d, 30.0)) + 1))
        return (mx, my), z < need + 2.5 and energy > 1.0, jump

    # ------------------------------------------------------------ mortar shells
    def shell(self, ctx):
        wm, me = self.wm, ctx.me
        x, y = me["pos"][0], me["pos"][1]
        rad = self.splash + float((me.get("stats") or {}).get("radius", self.mech_r)) + 2.0
        g = self.mortar_g
        for pr in ctx.obs.get("projectiles") or ():
            if pr.get("kind") != "mortar":
                continue
            px, py, pz = pr["pos"]
            vx, vy, vz = pr["vel"]
            t = 0.0
            while t < 4.0:
                px, py, pz = px + vx * 0.05, py + vy * 0.05, pz + vz * 0.05
                vz -= g * 0.05
                t += 0.05
                if wm.surface(px, py) >= pz:
                    d = math.hypot(px - x, py - y)
                    if d < rad and t < 2.5:
                        dx, dy = x - px, y - py
                        if d < 0.5:
                            dx, dy = -vy, vx
                            d = math.hypot(dx, dy) + 1e-9
                        dx, dy = dx / d, dy / d
                        for rot in (0.0, 0.7, -0.7, 1.4, -1.4):
                            c, s = math.cos(rot), math.sin(rot)
                            ex, ey = dx * c - dy * s, dx * s + dy * c
                            if wm.walk_clear(x, y, x + ex * 6.0, y + ey * 6.0):
                                return (ex, ey)
                    break
        return None

    # ------------------------------------------------------------ lightning carriers
    def kite(self, ctx, finishing, zone_pen):
        """(move, boost) away from a lightning carrier, or None."""
        me, j, mem, heat, wm = ctx.me, ctx.judge, ctx.mem, ctx.heat, self.wm
        if finishing or j.weapon(me, "lightning")[0] is not None:
            return None
        x, y = me["pos"][0], me["pos"][1]
        close, cd = None, self.light_range + 6.0
        for e in mem.recent_mechs(0.01):
            if "lightning" in (e.weapons or ()):
                d = math.hypot(e.pos[0] - x, e.pos[1] - y)
                if d < cd:
                    close, cd = e, d
        if close is None:
            return None
        dx, dy = x - close.pos[0], y - close.pos[1]
        d = math.hypot(dx, dy)
        if d < 0.1:
            dx, dy, d = 1.0, 0.0, 1.0
        dx, dy = dx / d, dy / d
        charging = {e.id for e in mem.recent_mechs(0.01) if e.charging}
        best, bk = (dx, dy), -1e18
        for rot in (0.0, 0.45, -0.45, 0.9, -0.9, 1.35, -1.35):
            c, s = math.cos(rot), math.sin(rot)
            ex, ey = dx * c - dy * s, dx * s + dy * c
            nx, ny = x + ex * 8.0, y + ey * 8.0
            if not wm.walk_clear(x, y, nx, ny):
                continue
            sc = 25.0 * math.cos(rot) - zone_pen(nx, ny)
            for eid in heat.seen_by.get(wm.nidx(nx, ny), ()):
                if eid != close.id:
                    sc -= 35.0 if eid in charging else 18.0
            if sc > bk:
                bk, best = sc, (ex, ey)
        return best, cd < self.light_range + 1.0 and float(me.get("energy", 0.0)) > 15.0

    # ------------------------------------------------------------ rounds and laser aim
    def dodge(self, ctx, strafe):
        """(dx, dy, laser_threat) sidestep, or (0, 0, False)."""
        me = ctx.me
        st = me.get("stats") or {}
        cx, cy = me["pos"][0], me["pos"][1]
        cz = me["pos"][2] + float(st.get("height", self.mech_h)) * 0.56
        reach = float(st.get("radius", self.mech_r)) + 1.1
        dx = dy = 0.0
        laser = False
        for pr in ctx.obs.get("projectiles") or ():
            if pr.get("kind") == "mortar":
                continue
            pv, pp = pr["vel"], pr["pos"]
            v2 = pv[0] * pv[0] + pv[1] * pv[1] + pv[2] * pv[2]
            if v2 < 1.0:
                continue
            tc = ((cx - pp[0]) * pv[0] + (cy - pp[1]) * pv[1] + (cz - pp[2]) * pv[2]) / v2
            if tc <= 0.0 or tc > 0.7:
                continue
            mx, my = pp[0] + pv[0] * tc - cx, pp[1] + pv[1] * tc - cy
            if math.hypot(mx, my) < reach:
                vh = math.hypot(pv[0], pv[1])
                if vh < 1.0:
                    continue
                px, py = -pv[1] / vh, pv[0] / vh
                if px * mx + py * my > 0.0:
                    px, py = -px, -py
                dx, dy = dx + px, dy + py
        for m in ctx.obs.get("mechs") or ():
            if m.get("kind") != "mech" or not m.get("charging"):
                continue
            mp = m["pos"]
            to_me = math.atan2(cy - mp[1], cx - mp[0])
            if abs(angdiff(float(m["aim"][0]), to_me)) > 0.15:
                continue
            a = to_me + math.pi * 0.5 * strafe
            dx, dy = dx + math.cos(a) * 1.5, dy + math.sin(a) * 1.5
            laser = True
        n = math.hypot(dx, dy)
        return (dx / n, dy / n, laser) if n > 0.01 else (0.0, 0.0, False)
