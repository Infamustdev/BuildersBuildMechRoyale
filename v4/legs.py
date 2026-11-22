"""Legs: which cell to head for, and the heat-weighted route there.

Priority each tick (evade.py holds the evasion moves):
  0. courtyard escape (jump-jet out of a walled pocket), then unstick (0.5 s after a stall),
     then a mortar shell about to land on us
  1. zone must: outside the circle, or the shrinking edge reaches us within our walk time + 3 s
  2. lightning kite when a carrier is close and we have no lightning (unless finishing a mech)
  2b. retreat (judge: outnumbered, or our cell is red): boost along the heat route to the best
      cooler cell within 60 m of route: route cost (ignoring our target) <= 2, i.e. only the
      target sees it, or at least 3 below where we stand; scored 10 x cost + 0.5 x route metres
      - 0.25 x distance from the nearest enemy (capped 80) - 15 if a cube lies there, inside the
      zone. None that close -> stand and fight. Torso and triggers keep
      shooting the whole time.
  3. fight (fight.py) against the judge's foe; a cube the economy flags mid-fight is grabbed first
  4. zone should: the shrinking edge reaches us within our walk time + 14 s
  5. the economy's loot goal: walk to items; a crate in sight within 50 m is shot from where we
     stand (backing off inside 12 m); a drone in sight within 75 m is fought like a mech
  6. hunt: the shared hunt goal (recent enemy cell, else likely-enemy hotspot)
  Calm goals (5-8) are held for 2 s once chosen (dropped early on arrival, ban, or the item
  going), so near-equal choices do not flip every tick; a reached hunt spot is banned.
  7. explore: walk to the ground we have gone longest without seeing (heat.explore_goal), so
     items and enemies we cannot see from here turn up; at any HP, so we never stand idle
  8. hold the zone goal (stay put unless outside next_radius - margin)
Every route is A* on the nav grid with heat.route_cost added per step, so it bends around cells
enemies can see, likely-enemy heat and where we were watched. Hunt goals that make no progress
are banned. Incoming rounds and laser aim on us are sidestepped by blending a dodge into
whatever move was chosen (not during escape or unstick).
"""
import heapq
import math


def unit(dx, dy):
    d = math.hypot(dx, dy)
    return (dx / d, dy / d) if d > 1e-6 else (0.0, 0.0)


REPLAN_S = 1.5
ARRIVE_M = 3.0
PROGRESS_S = 8.0       # must get PROGRESS_M closer to a hunt goal within this...
PROGRESS_M = 5.0
BAN_S = 40.0           # ...or it is banned this long
STUCK_WIN_S = 1.0      # moved less than STUCK_M over this window while walking -> unstick
STUCK_M = 1.2
UNSTICK_S = 0.5
COOL_MAX_M = 60.0      # a cool cell further than this along the route is not worth running for
SWARM_MAX_M = 90.0     # ...unless 3+ are coming at us
CHASE_STAND_M = 30.0   # an engaged mech this close and not backing off: fight, do not run
KITE_MIN, KITE_MAX = 130.0, 190.0
LIGHT_RESERVE = 35.0   # with lightning, only boost-retreat above this much energy
COOL_EVERY_S = 0.4
COOL_COST = 2.0        # cool: no enemy but our target sees it (route cost ignoring the target)...
COOL_DROP = 3.0        # ...or at least this much cooler than where we stand
CALM_HOLD_S = 2.0      # with no threat, a loot/hunt/explore/hold goal is kept this long
HUNT_ARRIVE_M = 15.0   # reaching a hunt spot this close bans it, so we move on


class Legs:
    def __init__(self, wm, rng, fight, evade, cfg):
        self.wm = wm
        self.fight = fight
        self.evade = evade
        self.rng = rng
        self.walk = float(cfg.get("mech", {}).get("walk_speed", 11.7))
        self.goal = None          # (x, y, kind)
        self.path = []
        self.path_t = -1e9
        self.path_goal = None
        self.prog = (None, 1e9, -1e9)   # (goal key, best distance, time it last improved)
        self.bans = []            # (x, y, until)
        self.hist = []
        self.unstick_until = -1e9
        self.unstick_dir = (0.0, 0.0)
        self.mode = "hold"
        self.cool, self.cool_t = None, -1e9
        self.explore = None
        self.calm = None          # (until, mode, gx, gy, kind, ignore, item id) held calm goal
        self.hunt_pref = None     # current hunt spot: hunt_goal favours it (hysteresis)

    # ------------------------------------------------------------ helpers
    def banned(self, x, y, t):
        self.bans = [b for b in self.bans if b[2] > t]
        return any((x - bx) ** 2 + (y - by) ** 2 < 20.0 ** 2 for bx, by, _ in self.bans)

    @staticmethod
    def zone_goal(z, x, y):
        nx, ny = z["next_center"]
        nr = float(z["next_radius"])
        if nr <= 30.0:
            return nx, ny
        margin = min(30.0, max(3.0, 0.3 * nr))
        dx, dy = x - nx, y - ny
        d = math.hypot(dx, dy)
        r = max(0.0, nr - margin - 5.0)
        if d < 1.0 or d <= r:
            return x, y
        return nx + dx / d * r, ny + dy / d * r

    @staticmethod
    def zone_deadline(z, x, y, t, pad=8.0):
        """When the shrinking circle's edge (minus pad) reaches (x, y): the circle moves linearly
        from its current centre/radius to next_center/next_radius between max(now, shrink_start)
        and shrink_end. 1e9 if it never does this phase."""
        ss, se = float(z.get("shrink_start", 1e9)), float(z.get("shrink_end", 1e9))
        cx, cy = z["center"]
        r = float(z["radius"])
        nx, ny = z["next_center"]
        nr = float(z["next_radius"])
        if math.hypot(x - cx, y - cy) > r - pad:
            return t
        a = max(t, ss)
        if se <= a:
            return 1e9
        for k in range(1, 31):
            f = k / 30.0
            tau = a + (se - a) * f
            ccx, ccy = cx + (nx - cx) * f, cy + (ny - cy) * f
            if math.hypot(x - ccx, y - ccy) > r + (nr - r) * f - pad:
                return tau
        return 1e9

    def _route(self, ctx, px, py, gx, gy, kind, ignore=None):
        """Direction to walk toward (gx, gy) along the heat-weighted route. `ignore`: a mech whose
        sight we do not avoid (the one we are hunting)."""
        t = ctx.t
        self.goal = (gx, gy, kind)
        if math.hypot(gx - px, gy - py) < ARRIVE_M:
            self.path = []
            return (0.0, 0.0)
        moved = self.path_goal is None or math.hypot(gx - self.path_goal[0], gy - self.path_goal[1]) > 5.0
        if not self.path or moved or t - self.path_t > REPLAN_S:
            heat = ctx.heat
            avoid = heat.route_cost if ignore is None else (lambda K: heat.route_cost(K, ignore))
            self.path = self.wm.path(px, py, gx, gy, budget_nodes=1800, avoid=avoid)
            self.path_goal = (gx, gy)
            self.path_t = t
        while len(self.path) > 1 and math.hypot(self.path[0][0] - px, self.path[0][1] - py) < 3.0:
            self.path.pop(0)
        wx, wy = self.path[0] if self.path else (gx, gy)
        return unit(wx - px, wy - py)

    def _cool_cell(self, ctx, px, py):
        """Nearest cool cell by heat-weighted route (Dijkstra, step 1 + route_cost). When swarmed
        (3+ coming) it searches 90 m and, failing a cool cell, takes the least-watched cell at
        least 12 m away, leaning away from the enemies: standing in a crowd is worse than running."""
        wm, heat, mem = self.wm, ctx.heat, ctx.mem
        S = wm.nearest_open(px, py)
        if S < 0:
            return None
        threats = mem.recent_mechs(2.5)
        tid = ctx.judge.target.id if ctx.judge.target is not None else None
        here = heat.route_cost(S, tid)
        cube_cells = {wm.nidx(c["pos"][0], c["pos"][1]) for c in mem.cubes.values()}
        z = ctx.zone
        nnx, nb, ng, N = wm.nnx, wm.nblock, wm.nground, wm.N
        swarm = ctx.judge.swarm
        limit = (SWARM_MAX_M if swarm else COOL_MAX_M) / N
        fb, fbs = None, 1e18
        dist = {S: 0.0}
        q = [(0.0, 0.0, S)]
        best, bs = None, 1e18
        nbrs = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                (1, 1, 1.4142), (1, -1, 1.4142), (-1, 1, 1.4142), (-1, -1, 1.4142))
        while q:
            c, steps, K = heapq.heappop(q)
            if c > dist.get(K, 1e18) + 1e-9 or steps > limit:
                continue
            ck = heat.route_cost(K, tid)
            if swarm and steps * N >= 12.0:
                cx, cy = wm.ncenter(K)
                near = min((math.hypot(e.pos[0] - cx, e.pos[1] - cy) for e in threats), default=80.0)
                f = 10.0 * ck - 0.5 * min(near, 80.0)
                if f < fbs:
                    fbs, fb = f, (cx, cy)
            if ck <= COOL_COST or ck <= here - COOL_DROP:
                cx, cy = wm.ncenter(K)
                if self.fight._zone_pen(z, ctx.t, cx, cy) == 0.0:
                    near = min((math.hypot(e.pos[0] - cx, e.pos[1] - cy) for e in threats), default=80.0)
                    sc = 10.0 * ck + 0.5 * steps * N - 0.25 * min(near, 80.0)
                    if K in cube_cells:
                        sc -= 15.0      # a cube on a cool cell: take it on the way out
                    if sc < bs:
                        bs, best = sc, (cx, cy)
            I, J = K % nnx, K // nnx
            for dI, dJ, w in nbrs:
                K2 = (J + dJ) * nnx + (I + dI)
                if K2 < 0 or K2 >= len(nb) or nb[K2] or abs(ng[K2] - ng[K]) > 4.0:
                    continue
                if dI and dJ and (nb[J * nnx + I + dI] or nb[(J + dJ) * nnx + I]):
                    continue
                c2 = c + w * (1.0 + heat.route_cost(K2))
                if c2 < dist.get(K2, 1e18):
                    dist[K2] = c2
                    heapq.heappush(q, (c2, steps + w, K2))
        return best if best is not None else fb

    def _commit(self, mode, gx, gy, kind, ignore, iid, t):
        """Hold a calm-time goal CALM_HOLD_S; re-choosing the same goal does not restart it."""
        c = self.calm
        if c is None or c[1] != mode or abs(c[2] - gx) > 4.0 or abs(c[3] - gy) > 4.0:
            self.calm = (t + CALM_HOLD_S, mode, gx, gy, kind, ignore, iid)

    def _calm_ok(self, ctx, c, px, py, t):
        _, mode, gx, gy, kind, ignore, iid = c
        d = math.hypot(gx - px, gy - py)
        if d < ARRIVE_M + 3.0 or self.banned(gx, gy, t):
            return False
        if mode.startswith("loot:"):
            mem = ctx.mem
            if not (iid in mem.pickups or iid in mem.cubes or iid in mem.crates or iid in mem.tracks):
                return False
            if kind in ("crate", "npc") and d < 75.0:
                return False          # close enough to shoot it: the loot branch handles that
        return self._progress_ok((mode, round(gx), round(gy)), d, t)

    def _progress_ok(self, key, d, t):
        k, best, bt = self.prog
        if k != key or d < best - PROGRESS_M:
            self.prog = (key, d, t)
            return True
        return t - bt <= PROGRESS_S

    # ------------------------------------------------------------ tick
    def decide(self, ctx):
        me = ctx.me
        px, py = me["pos"][0], me["pos"][1]
        t = ctx.t
        esc = self.evade.escape(ctx)
        if esc is not None:
            self.mode = "escape"
            self.hist = []
            return esc
        if t < self.unstick_until:
            self.mode = "unstick"
            return self.unstick_dir, False, False
        energy = float(me.get("energy", 0.0))
        sh = self.evade.shell(ctx)
        if sh is not None:
            self.mode = "shell"
            mv, boost = sh, energy > 15.0
        else:
            mv, boost = self._choose(ctx, me, px, py, t)
            ddx, ddy, laser = self.evade.dodge(ctx, self.fight.strafe)
            if ddx or ddy:
                mx, my = mv[0] * 0.4 + ddx * 0.9, mv[1] * 0.4 + ddy * 0.9
                n = math.hypot(mx, my)
                mv = (mx / n, my / n) if n > 1.0 else (mx, my)
                if laser and energy > 25.0:
                    boost = True
                self.mode += "+dodge"
        jump = False
        if mv[0] == 0.0 and mv[1] == 0.0:
            self.hist = []
        else:
            self.hist.append((t, px, py))
            while self.hist and t - self.hist[0][0] > STUCK_WIN_S + 0.2:
                self.hist.pop(0)
            old = self.hist[0]
            if t - old[0] >= STUCK_WIN_S and math.hypot(px - old[1], py - old[2]) < STUCK_M:
                a = self.rng.uniform(0, 2 * math.pi)
                self.unstick_dir = (math.cos(a), math.sin(a))
                self.unstick_until = t + UNSTICK_S
                self.hist = []
                self.path = []
                jump = True
        return mv, boost, jump

    def _choose(self, ctx, me, px, py, t):
        z = ctx.zone
        energy = float(me.get("energy", 0.0))
        walk = float((me.get("stats") or {}).get("walk_speed", self.walk)) or self.walk
        zgx, zgy = self.zone_goal(z, px, py)
        zd = math.hypot(zgx - px, zgy - py)
        t_need = zd * 1.45 / walk + 4.0
        deadline = self.zone_deadline(z, px, py, t)
        outside = not me.get("in_zone", True)
        zone_must = outside or (zd > ARRIVE_M and t + t_need + 3.0 >= deadline)
        zone_should = zd > ARRIVE_M and t + t_need + 14.0 >= deadline
        self.fight.cells, self.fight.best = [], None

        if zone_must and zd > ARRIVE_M:
            self.mode = "zone"
            return self._route(ctx, px, py, zgx, zgy, "zone"), outside and energy > 5.0

        j = ctx.judge
        tg = j.target
        finishing = (tg is not None and j.dist.get(tg.id, 1e9) < 120.0
                     and (tg.hp < 200.0 or tg.hp < me["hp"] * 0.5))
        kite = self.evade.kite(ctx, finishing, lambda x, y: self.fight._zone_pen(z, t, x, y))
        if kite is not None:
            self.mode = "kite"
            self.goal = None
            return kite

        # A mech coming at us inside 30 m will run us down: turning away only shows it our back
        # (rear hits x1.5), so we stand and fight it instead (unless 3+ are coming).
        chaser = any(j.dist.get(i, 1e9) < CHASE_STAND_M and j.closing.get(i, 0.0) > -1.0
                     for i in (e.id for e in j.engaged))
        # Range kite (2+ on us, we hold a laser, not brawling with lightning): hold 130-190 m from
        # the nearest engaged mech while the torso keeps shooting.
        if not j.retreat and j.weapon(me, "laser")[0] is not None and not finishing:
            vis = [e for e in j.engaged if j.los.get(e.id, False)]
            if len(vis) >= 2:
                nr = min(vis, key=lambda e: j.dist.get(e.id, 1e9))
                nd = j.dist.get(nr.id, 1e9)
                if nd < KITE_MIN:
                    ux, uy = unit(px - nr.pos[0], py - nr.pos[1])
                    gx, gy = nr.pos[0] + ux * (KITE_MIN + KITE_MAX) / 2, nr.pos[1] + uy * (KITE_MIN + KITE_MAX) / 2
                    self.mode = "kite:range"
                    self.goal = None
                    mv = self._route(ctx, px, py, gx, gy, "kite")
                    return mv, nd < 70.0 and energy > 45.0 and bool(mv[0] or mv[1])
        if j.retreat and (not chaser or j.swarm):
            if self.cool is None or t - self.cool_t > COOL_EVERY_S:
                self.cool, self.cool_t = self._cool_cell(ctx, px, py), t
            if self.cool is not None:
                self.mode = "retreat:" + j.why
                mv = self._route(ctx, px, py, self.cool[0], self.cool[1], "cool")
                # keep energy for lightning shots: boosting drains what the arc needs
                reserve = LIGHT_RESERVE if j.weapon(me, "lightning")[0] is not None else 10.0
                return mv, energy > reserve and bool(mv[0] or mv[1])
            # nowhere cool within reach: run 30 m straight away from whoever is hurting us,
            # boosting above 30 energy
            src = j.target or j.foe
            if src is not None:
                ux, uy = unit(px - src.pos[0], py - src.pos[1])
                gx, gy = px + ux * 30.0, py + uy * 30.0
                if self.wm.walk_clear(px, py, gx, gy):
                    self.mode = "retreat:run"
                    mv = self._route(ctx, px, py, gx, gy, "flee")
                    return mv, energy > 30.0 and bool(mv[0] or mv[1])
        else:
            self.cool = None

        foe = j.foe
        if foe is not None:
            # a wanted weapon within 32 m decides the fight: grab it first
            for iid, pk in ctx.mem.pickups.items():
                if (ctx.economy.want_pickup(me, pk["kind"]) and pk["kind"] in ("laser", "lightning")
                        and math.hypot(pk["pos"][0] - px, pk["pos"][1] - py) < 32.0):
                    self.mode = "fight:arm"
                    return self._route(ctx, px, py, pk["pos"][0], pk["pos"][1], "pickup"), False
            scoop = ctx.economy.scoop
            if scoop is not None:
                self.mode = "fight:cube"
                return self._route(ctx, px, py, scoop[0], scoop[1], "cube"), False
            mv, boost = self.fight.decide(ctx, foe)
            self.mode = "cornered" if j.retreat else self.fight.mode
            self.goal = None
            return mv, boost

        if zone_should:
            self.mode = "zone"
            return self._route(ctx, px, py, zgx, zgy, "zone"), False

        # Calm from here on (no fight, retreat, kite or zone pressure): keep a goal we picked
        # for CALM_HOLD_S instead of flipping between near-equal choices every tick.
        c = self.calm
        if c is not None and t < c[0]:
            if self._calm_ok(ctx, c, px, py, t):
                self.mode = c[1]
                return self._route(ctx, px, py, c[2], c[3], c[4], ignore=c[5]), False
            self.calm = None

        lg = ctx.loot_goal
        if lg is not None:
            kind, iid, pos = lg
            self.mode = "loot:" + kind
            d = math.hypot(pos[0] - px, pos[1] - py)
            j = ctx.judge
            if kind == "crate" and j.farm_kind == "crate" and d < 50.0:
                self.goal = (pos[0], pos[1], "crate")
                if d < 12.0:
                    return unit(px - pos[0], py - pos[1]), False
                # strafe across it while shooting rather than standing still
                ux, uy = unit(pos[0] - px, pos[1] - py)
                s = self.fight.strafe_dir(t)
                sx, sy = -uy * s, ux * s
                if not self.wm.walk_clear(px, py, px + sx * 6.0, py + sy * 6.0):
                    sx, sy = -sx, -sy
                return (sx, sy), False
            if kind == "npc":
                tr = ctx.mem.tracks.get(iid)
                # in reach: fight movement circles it for a shot line (no parking under it)
                if tr is not None and t - tr.seen_t < 3.0 and d < 75.0:
                    self.goal = None
                    mv, _ = self.fight.decide(ctx, tr)
                    return mv, False
            self._commit(self.mode, pos[0], pos[1], kind, None, iid, t)
            mv = self._route(ctx, px, py, pos[0], pos[1], kind)
            # sprint for a laser while energy allows
            pk = ctx.mem.pickups.get(iid) if kind == "pickup" else None
            sprint = pk is not None and pk["kind"] == "laser" and energy > 30.0 and bool(mv[0] or mv[1])
            return mv, sprint

        h = ctx.hunt
        self.hunt_pref = None
        if h is not None and not self.banned(h[0], h[1], t):
            d = math.hypot(h[0] - px, h[1] - py)
            if d < HUNT_ARRIVE_M:
                # arrived and nothing to fight here: move on to the next spot
                self.bans.append((h[0], h[1], t + (15.0 if h[2] == "track" else BAN_S)))
            elif self._progress_ok(("hunt", round(h[0]), round(h[1])), d, t):
                self.mode = "hunt"
                self.hunt_pref = (h[0], h[1])
                self._commit("hunt", h[0], h[1], "hunt:" + h[2], h[3], None, t)
                return self._route(ctx, px, py, h[0], h[1], "hunt:" + h[2], ignore=h[3]), False
            else:
                self.bans.append((h[0], h[1], t + BAN_S))

        if True:                      # explore at any HP: never stand idle
            g = self.explore
            if g is None or math.hypot(g[0] - px, g[1] - py) < 12.0 or self.banned(g[0], g[1], t):
                g = self.explore = ctx.heat.explore_goal(px, py, z, lambda x, y: self.banned(x, y, t))
            if g is not None:
                d = math.hypot(g[0] - px, g[1] - py)
                if self._progress_ok(("explore", round(g[0]), round(g[1])), d, t):
                    self.mode = "explore"
                    self._commit("explore", g[0], g[1], "explore", None, None, t)
                    return self._route(ctx, px, py, g[0], g[1], "explore"), False
                self.bans.append((g[0], g[1], t + BAN_S))
                self.explore = None

        self.mode = "hold"
        return self._route(ctx, px, py, zgx, zgy, "hold"), False
