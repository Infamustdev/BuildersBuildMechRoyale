"""Judge: who counts as a fight, which one we shoot, and the firing solution on it.

Runs once per tick before the controllers, so torso and triggers read the same solution and agree
without either waiting on the other.

Engage (squad stance): hit us in the last 6 s, or seen this tick and any of: within 60 m, closing
faster than 3 m/s inside 180 m, below 40% HP inside 160 m, aiming at us (0.22 rad) inside 190 m,
the endgame (next zone radius <= 70 m or 3 mechs left), or - when we are above 60% HP - any mech
we have a shot line to inside 95 m (120 m with a laser), if we hold a laser or it has under 75%
of our HP, so we take the fights we should win early.
Retreat (held 1.5 s after it clears, never in the endgame):
  outnumbered - (counting only mechs within 160 m that are engaged, closing in or inside 80 m)
                2+ such mechs and HP < 55%, or 3+ and HP < 80%, or 2+ coming
                at us (closing > 1 m/s or inside 40 m) and HP < 80%, or 3+ coming at any HP;
                never when every nearby mech is moving away (we chase those); or HP < 35%
                against a target 10 points healthier; cancelled when the target is under 25%,
                we are above 20% and at most two are near (finish it instead)
  red         - the heat route cost of our own cell is red: >= 8 (about three enemies watching,
                or two plus likely-enemy heat), or >= 6 once HP is under 70%
Pressed: retreating, or our own cell's route cost >= 6. The economy then only takes
loot that sits in a cooler cell than ours, and the escape route prefers cool cells with a cube.
While retreating the HP weight in the target score rises to 0.6, so we keep shooting the weakest.
Target override (only in this case): holding lightning with energy and our target out of its
range, switch to the weakest candidate inside it.
Target: lowest `distance + 0.18 hp - 200 (hp <= 30%) - 200 (same target within 2 s) - 40 (hit us
in 4 s) - 15 (closing)` among engaged mechs we have a shot line to, within 200 m.
"""
import math

from memory import angdiff

AIM_LATENCY = 0.075
ENGAGE_D = 60.0
MAX_TARGET = 200.0
HP_W = 0.18
LOW_BONUS = 200.0
STICK = 200.0
STICK_S = 2.0
RETREAT_HOLD_S = 1.5
FLEE_SPEED = 2.0           # moving away from us faster than this counts as fleeing
CHASE_HP = 0.35            # chase fleeing mechs above this HP
ENGAGE_IDLE_D = 95.0       # engage an idle mech we can shoot inside this (healthy only)
ENGAGE_IDLE_LASER = 120.0
ENGAGE_IDLE_HP = 0.6
ENGAGE_IDLE_EDGE = 0.75    # ...without a laser, only if its HP is under this x ours
RED_HURT_HP = 0.7          # the amber (>= 6) retreat needs HP below this
RED_COST, RED_COST_HURT = 8.0, 6.0
RETREAT_HP_W = 0.6


class Solution:
    """Firing solution on the current target."""
    __slots__ = ("dist", "gun_yaw", "gun_pitch", "gun_ok", "hit_yaw", "hit_pitch", "tol_y", "tol_p",
                 "hitscan", "lethal", "is_mech")


class Judge:
    def __init__(self, wm, cfg):
        self.wm = wm
        mc = cfg.get("mech", {})
        cc = cfg.get("cubes", {})
        self.mech_r = float(mc.get("radius", 2.5))
        self.mech_h = float(mc.get("height", 9.0))
        self.muzzle_h = float(mc.get("muzzle_height", 7.0))
        self.rear_mult = max(1.0, float(mc.get("rear_mult", 1.5)))
        self.front_arc = math.radians(mc.get("front_arc_deg", 180.0)) / 2
        self.size_per_cube = float(cc.get("size_per_cube", 0.05))
        self.dmg_per_cube = float(cc.get("damage_per_cube", 0.04))
        self.cube_cap = int(cc.get("stat_cap", 10))
        nc, crc = cfg.get("npc", {}) or {}, cfg.get("crates", {}) or {}
        self.npc_r, self.npc_h = float(nc.get("radius", 1.8)), float(nc.get("height", 3.5))
        self.crate_r, self.crate_h = float(crc.get("radius", 1.4)), float(crc.get("height", 2.4))
        self.tick_s = 1.0 / max(1.0, float(cfg.get("match", {}).get("tick_hz", 20.0) or 20.0))
        self.stick_id, self.stick_at = None, -1e9
        self.last_threat_at = -1e9
        self.lose_at = -1e9
        self.retreat = False
        self.why = ""           # "outnumbered" / "red" while retreating
        self.here_cost = 0.0    # route cost of our cell, ignoring the target
        self.pressed = False
        self.swarm = False      # 3+ mechs coming at us: run even without a cool cell close by
        # per tick outputs
        self.engaged = []       # Tracks that count as a fight
        self.target = None      # Track we shoot
        self.foe = None         # engaged but no shot line (laser pre-charge, torso watch)
        self.sol = None
        self.farm_kind = None   # "npc" / "crate" when the solution is on loot, not a mech
        self.muzzle = (0.0, 0.0, 0.0)
        self.los = {}           # id -> shot line from our muzzle this tick
        self.closing = {}       # id -> closing speed toward us (m/s, negative = moving away)
        self.dist = {}

    @staticmethod
    def weapon(me, kind):
        for i, w in enumerate((me.get("weapons") or [])[:2]):
            if w and w.get("kind") == kind:
                return i, w
        return None, None

    def _scale(self, tr):
        return 1.0 + self.size_per_cube * (tr.cubes or 0)

    def shows_back(self, tr, x, y):
        return abs(angdiff(tr.aim[0], math.atan2(y - tr.pos[1], x - tr.pos[0]))) > self.front_arc + 0.1

    def _lead(self, muzzle, tp, tv, speed, grounded):
        speed = speed if speed > 0 else 252.0
        t = 0.0
        mx, my, mz = muzzle
        px = py = pz = 0.0
        for _ in range(4):
            tt = AIM_LATENCY + t
            px, py = tp[0] + tv[0] * tt, tp[1] + tv[1] * tt
            pz = tp[2] + (0.0 if grounded else tv[2] * tt * 0.5)
            d = math.sqrt((px - mx) ** 2 + (py - my) ** 2 + (pz - mz) ** 2)
            t = max(0.0, d - 3.0) / speed
        return math.atan2(py - my, px - mx), math.atan2(pz - mz, math.hypot(px - mx, py - my))

    def update(self, mem, obs, loot_goal=None, heat=None):
        t = mem.t
        me = mem.me
        x, y, z = me["pos"]
        vx, vy = me["vel"][0], me["vel"][1]
        st = me.get("stats") or {}
        mh = float(st.get("muzzle_height", self.muzzle_h))
        self.muzzle = muzzle = (x + vx * AIM_LATENCY, y + vy * AIM_LATENCY, z + mh)
        zone = obs["zone"]
        late = float(zone.get("next_radius", 999.0)) <= 70.0 or int(obs.get("alive", 16)) <= 3
        _, laser = self.weapon(me, "laser")
        max_t = MAX_TARGET

        hp_frac = me["hp"] / max(1.0, me["max_hp"])
        hp_w = RETREAT_HP_W if self.retreat else HP_W
        self.engaged, self.los, self.dist, self.closing = [], {}, {}, {}
        _, light = self.weapon(me, "lightning")
        light_r = float((light.get("stats") or {}).get("range", 45.0)) if light else 0.0
        light_ok = light is not None and float(me.get("energy", 0.0)) >= float((light.get("stats") or {}).get("energy", 3.0)) * 2
        target, foe, best = None, None, 1e18
        cands = []
        danger = False
        for tr in mem.recent_mechs(2.5):
            dx, dy = tr.pos[0] - x, tr.pos[1] - y
            d = math.hypot(dx, dy)
            self.dist[tr.id] = d
            now_seen = tr.seen_t == t
            los = False
            if now_seen:
                h = self.mech_h * self._scale(tr)
                los = self.wm.los(muzzle, (tr.pos[0], tr.pos[1], tr.pos[2] + h * 0.5), canopy=False) or \
                    self.wm.los(muzzle, (tr.pos[0], tr.pos[1], tr.pos[2] + h * 0.85), canopy=False)
            self.los[tr.id] = los
            closing = -((tr.vel[0] - vx) * dx + (tr.vel[1] - vy) * dy) / d if d > 1.0 else 0.0
            self.closing[tr.id] = closing
            hit_ago = t - tr.last_hit_us_t
            engage = False
            if now_seen:
                aimed = abs(angdiff(tr.aim[0], math.atan2(-dy, -dx))) < 0.22
                engage = (d < ENGAGE_D or hit_ago < 6.0 or late
                          or (closing > 3.0 and d < 180.0)
                          or (tr.hp < 0.4 * tr.max_hp and d < 160.0)
                          or (aimed and d < 190.0)
                          # running away from us: chase it
                          or (closing < -FLEE_SPEED and d < 160.0 and los and hp_frac >= CHASE_HP)
                          # take the fight early: an idle mech we can shoot, close enough that
                          # opening fire does not recruit the far side of the map
                          or (los and hp_frac >= ENGAGE_IDLE_HP
                              and d < (ENGAGE_IDLE_LASER if laser is not None else ENGAGE_IDLE_D)
                              # only with an edge: a laser to trade at range, or it is clearly
                              # weaker than us (counted HP, not a modelled duel)
                              and (laser is not None or tr.hp < ENGAGE_IDLE_EDGE * me["hp"])))
            if engage or hit_ago < 4.0:
                danger = True
            if not engage:
                continue
            self.engaged.append(tr)
            if not los:
                if d < max_t and (foe is None or d < self.dist[foe.id]):
                    foe = tr
                continue
            if d > max_t:
                continue
            s = d + hp_w * tr.hp
            if tr.hp <= 0.3 * tr.max_hp:
                s -= LOW_BONUS
            if tr.id == self.stick_id and t - self.stick_at < STICK_S:
                s -= STICK
            if hit_ago < 4.0:
                s -= 40.0
            if closing > 3.0:
                s -= 15.0
            cands.append((tr, d))
            if s < best:
                best, target = s, tr
        # Narrow overrides on top of the usual pick; they only fire in the two situations seed 10
        # showed, so ordinary fights keep choosing exactly as before.
        target = self._override(target, cands, light_ok, light_r)
        if target is not None:
            self.stick_id, self.stick_at = target.id, t
        if danger or any(t - h[0] < 3.0 and h[3] != "zone" for h in mem.hits):
            self.last_threat_at = t
        self.target = target
        self.foe = target or foe
        self._retreat(mem, me, hp_frac, late, target, heat)
        self.farm_kind = None
        self.sol = None
        if target is not None:
            sc = self._scale(target)
            self.sol = self._solve(me, target.pos, target.vel, self.mech_r * sc, self.mech_h * sc, laser, target)
        else:
            self._farm(mem, me, muzzle, loot_goal, x, y)

    def _override(self, target, cands, light_ok, light_r):
        """Lightning idle: we hold lightning with energy, our target is outside its range, and
        another candidate is inside it -> shoot the weakest one in range. (A "shoot whoever just
        hit us hard" switch was tried too and lowered the 16-seed average.)"""
        if not cands:
            return target
        if light_ok and target is not None and self.dist.get(target.id, 1e9) >= light_r - 2.0:
            close = [c for c in cands if c[1] < light_r - 2.0]
            if close:
                return min(close, key=lambda c: c[0].hp)[0]
        return target

    def _retreat(self, mem, me, hp_frac, late, target, heat):
        t = mem.t
        # a mech counts toward "outnumbered" only if it is part of this fight: engaged with us,
        # coming at us, or inside 80 m (a bystander 150 m away does not make a duel a 2-on-1)
        engaged_ids = {e.id for e in self.engaged}
        near_ids = [e.id for e in mem.recent_mechs(0.01) if self.dist.get(e.id, 1e9) < 160.0
                    and (e.id in engaged_ids or self.dist.get(e.id, 1e9) < 80.0
                         or self.closing.get(e.id, 0.0) > 1.0)]
        near = len(near_ids)
        # who is coming for us: closing in, or already inside 40 m
        coming = sum(1 for i in near_ids if self.closing.get(i, 0.0) > 1.0 or self.dist.get(i, 1e9) < 40.0)
        fleeing = sum(1 for i in near_ids if self.closing.get(i, 0.0) < -FLEE_SPEED)
        losing = ((near >= 2 and hp_frac < 0.55) or (near >= 3 and hp_frac < 0.80)
                  or (coming >= 2 and hp_frac < 0.80) or coming >= 3)
        if near and fleeing == near:
            losing = False        # they are running from us: hunt them instead
        self.swarm = coming >= 3
        if target is not None:
            tf = target.hp / max(1.0, target.max_hp)
            if hp_frac < 0.35 and tf > hp_frac + 0.1:
                losing = True
            if tf < 0.25 and hp_frac > 0.20 and near <= 2:
                losing = False
        red = False
        c = 0.0
        if heat is not None:
            K = self.wm.nidx(me["pos"][0], me["pos"][1])
            c = heat.route_cost(K) if K >= 0 else 0.0
            red = c >= RED_COST or (c >= RED_COST_HURT and hp_frac < RED_HURT_HP)
            self.here_cost = heat.route_cost(K, target.id if target is not None else None) if K >= 0 else 0.0
        if (losing or red) and not late:
            self.lose_at = t
            self.why = "outnumbered" if losing else "red"
        self.retreat = t - self.lose_at < RETREAT_HOLD_S
        self.pressed = self.retreat or c >= RED_COST_HURT

    def loot_ok(self, heat, x, y):
        """May we walk to loot at (x, y)? Never into red; when pressed, only into a cooler cell
        (only the target sees it, or at least 3 cooler than where we stand)."""
        K = self.wm.nidx(x, y)
        if K < 0:
            return False
        c = heat.route_cost(K, self.target.id if self.target is not None else None)
        if self.pressed:
            return c <= 2.0 or c <= self.here_cost - 3.0
        return c < RED_COST_HURT

    def _farm(self, mem, me, muzzle, loot_goal, x, y):
        """No mech to shoot: a drone that is hurting us, else the loot the economy picked."""
        t = mem.t
        npcs = []
        for tr in mem.tracks.values():
            if tr.kind != "npc" or t - tr.seen_t > 0.5:
                continue
            d = math.hypot(tr.pos[0] - x, tr.pos[1] - y)
            self.dist[tr.id] = d
            los = d <= 150.0 and self.wm.los(muzzle, (tr.pos[0], tr.pos[1], tr.pos[2] + self.npc_h * 0.5), canopy=False)
            self.los[tr.id] = los
            if los:
                npcs.append((tr, d))
        pick = None
        nh = mem.npc_hit
        if nh and t - nh[0] < 3.0:            # defend: the drone lined up with the hit
            best_off = 0.7
            for tr, d in npcs:
                off = 0.0 if t - tr.last_hit_us_t < 3.0 else abs(angdiff(math.atan2(tr.pos[1] - y, tr.pos[0] - x), nh[1]))
                if off < best_off:
                    best_off, pick = off, tr
        hp_frac = me["hp"] / max(1.0, me["max_hp"])
        if pick is None and loot_goal is not None and hp_frac >= 0.5:
            kind, iid, pos = loot_goal
            if kind == "npc":
                pick = next((tr for tr, d in npcs if tr.id == iid and d < 75.0), None)
            elif kind == "crate" and math.hypot(pos[0] - x, pos[1] - y) < 50.0:
                cp = (pos[0], pos[1], pos[2] + self.crate_h * 0.5)
                if self.wm.los(muzzle, cp, canopy=False):
                    self.sol = self._solve(me, pos, (0.0, 0.0, 0.0), self.crate_r, self.crate_h, None, None)
                    self.farm_kind = "crate"
                    return
        if pick is not None:
            self.sol = self._solve(me, pick.pos, pick.vel, self.npc_r, self.npc_h, None, None)
            self.farm_kind = "npc"

    def _solve(self, me, pos, tv, r, h, laser, tr):
        """Firing solution on a body with feet at pos, radius r, height h. `tr` is the mech
        track (None for drones and crates: no laser on those)."""
        x, y = me["pos"][0], me["pos"][1]
        muzzle = self.muzzle
        tp = (pos[0], pos[1], pos[2] + h * 0.5)
        if not self.wm.los(muzzle, tp, canopy=False):
            tp = (pos[0], pos[1], pos[2] + h * 0.85)
        s = Solution()
        s.is_mech = tr is not None
        s.dist = dist = math.hypot(tp[0] - x, tp[1] - y)
        _, gun = self.weapon(me, "gun")
        s.gun_ok = False
        if gun is not None:
            gst = gun.get("stats") or {}
            s.gun_yaw, s.gun_pitch = self._lead(muzzle, tp, tv, float(gst.get("speed", 252.0)), abs(tv[2]) < 0.5)
            s.gun_ok = dist <= float(gst.get("range", 180.0)) * 0.96
        lat = AIM_LATENCY + self.tick_s
        dp = (tp[0] + tv[0] * lat, tp[1] + tv[1] * lat, tp[2])
        s.hit_yaw = math.atan2(dp[1] - muzzle[1], dp[0] - muzzle[0])
        s.hit_pitch = math.atan2(dp[2] - muzzle[2], math.hypot(dp[0] - muzzle[0], dp[1] - muzzle[1]))
        if gun is None:
            s.gun_yaw, s.gun_pitch = s.hit_yaw, s.hit_pitch
        s.tol_y = math.atan2(r * 0.75, max(dist, 1.0))
        s.tol_p = math.atan2(h * 0.40, max(dist, 1.0))
        # Laser: the torso switches to the hitscan aim once the charge is past 45%, or when an
        # early release would already kill (damage x 0.70 >= target HP).
        s.hitscan, s.lethal = False, False
        if laser is not None and tr is not None:
            wst = laser.get("stats") or {}
            full = float(wst.get("charge", 1.4))
            chg = float(laser.get("charge", 0.0))
            if dist <= float(wst.get("range", 200.0)) * 0.97:
                if chg >= float(wst.get("min_charge", 0.3)) and chg < full - 0.051:
                    mf = float(wst.get("min_frac", 0.35))
                    dmg = (float(wst.get("damage", 315.0)) * (mf + (1.0 - mf) * chg / max(0.01, full))
                           * (1.0 + self.dmg_per_cube * min(int(me.get("cubes", 0)), self.cube_cap)))
                    if "mortar" in (tr.weapons or ()):
                        dmg *= 1.0 + float(wst.get("cookoff", 0.6))
                    s.lethal = dmg * 0.70 >= tr.hp
                if s.lethal or chg > full * 0.45:
                    s.hitscan = True
        return s
