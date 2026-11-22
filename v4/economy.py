"""Economy: upgrades, weapon pickups, and what loot is worth walking to.

Owns the action fields `pick`, `take`, `drop`, and publishes a loot goal the legs read next tick.

- Upgrades: fixed order, laser line first (plating, armor, laser damage/charge, nanites, gun...).
- Pickups: take a weapon we are standing on if we want it. Ranking laser > lightning > mortar,
  never mortar; the gun only into an empty slot 0. Never drop a laser mid-charge.
- Loot (no fight on): utility = value / (detour + 18), where detour is the extra walk versus
  heading straight to the hunt goal (plain distance when there is none), divided again by
  1 + 0.5 x enemies that can see the item's cell. Values: weapon 130 (60 if slot 1 is full,
  150 for a laser up to 240 m), cube 75 (25 at the cube cap, within 105 m), crate 45 and a drone
  85 (only above 50% HP, no danger, within 150 m; no drones while a mech was seen within 150 m
  in the last 10 s). Same item held 30 s without reaching it ->
  banned 60 s. Items off the ground (roofs) or near the zone edge are skipped.
- Fight scoop: during a fight, the nearest cube within 70 m (40 m below half HP), unless a
  lightning carrier stands next to it and we have no lightning.
- Heat: never walk to loot in a red cell; when pressed (retreating, or our cell amber/red) only
  to loot in a cooler cell than ours (judge.loot_ok). Safety before loot.
"""
import math

from judge import Judge

UPGRADE_RANK = {
    "sensors": 0.98, "gyros": 0.95, "jets": 0.92, "mortar_splash": 0.90, "mortar_reload": 0.88,
    "mortar_velocity": 0.85, "capacitor": 0.82, "reactor": 0.80, "laser_pierce": 0.78,
    "lightning_chain": 0.75, "servos": 0.68, "nanites": 0.62, "gun_mag": 0.55,
    "gun_velocity": 0.52, "laser_charge": 0.48, "gun_rate": 0.45, "gun_damage": 0.42,
    "laser_damage": 0.40, "lightning_range": 0.38, "lightning_efficiency": 0.35,
    "armor": 0.30, "plating": 0.28,
}
UPGRADE_ORDER = (
    "plating", "armor", "laser_damage", "laser_charge", "nanites",
    "gun_damage", "gun_rate", "lightning_efficiency", "lightning_range",
    "servos", "gun_mag", "gun_velocity", "gyros", "lightning_chain",
    "reactor", "capacitor", "mortar_reload", "mortar_splash",
    "mortar_velocity", "sensors", "jets", "laser_pierce",
)
RANK = {"laser": 4, "lightning": 3, "mortar": 1}
WVAL = {"laser": 6.0, "lightning": 2.6, "gun": 2.0}
GO_K = 6.8          # loot value scale: a cube (11) is worth 75 on our utility scale
WALK_M = 7.2        # metres of walking per second of handling time
WANT_CUBES = 26     # keep collecting cubes up to this many
LOOT_R = 250.0
LASER_SEEK = 250.0
FIGHT_LOOT = 70.0
FARM_HP = 0.50


class Economy:
    def __init__(self, wm, cfg):
        self.wm = wm
        self.pickup_r = float(cfg.get("loot", {}).get("pickup_radius", 4.0))
        self.cube_cap = int(cfg.get("cubes", {}).get("stat_cap", 10))
        self.light_range = float(cfg.get("weapons", {}).get("lightning", {}).get("range", 45.0))
        self.goal = None          # (kind, id, (x, y, z))
        self.scoop = None         # cube position to grab mid-fight
        self.loot_id, self.loot_since = None, -1e9
        self.bad = {}             # item id -> banned until
        self.mode = "-"

    @staticmethod
    def want_pickup(me, kind):
        ws = me.get("weapons") or []
        if kind == "gun":
            return len(ws) > 0 and ws[0] is None
        if kind == "mortar" or Judge.weapon(me, kind)[0] is not None:
            return False
        cur = RANK.get(ws[1]["kind"], 0) if (len(ws) > 1 and ws[1] is not None) else 0
        return RANK.get(kind, 0) > cur

    def decide(self, ctx):
        """Returns the action fields this controller owns."""
        me, mem, t = ctx.me, ctx.mem, ctx.t
        x, y, zf = me["pos"]
        out = {}
        offer = ctx.obs.get("offer")
        if offer and offer.get("choices"):
            has_light = Judge.weapon(me, "lightning")[0] is not None
            vals = [UPGRADE_RANK.get(c.get("id"), 0.25)
                    + (0.3 if has_light and c.get("id") in ("reactor", "capacitor") else 0.0)
                    for c in offer["choices"]]
            out["pick"] = vals.index(max(vals))

        ws = me.get("weapons") or []
        for pk in ctx.obs.get("pickups") or ():
            pp = pk["pos"]
            if math.hypot(pp[0] - x, pp[1] - y) > self.pickup_r - 0.3 or abs(pp[2] - zf) > 3.6:
                continue
            if not self.want_pickup(me, pk.get("kind", "")):
                continue
            if pk.get("kind") != "gun" and len(ws) > 1 and ws[1] is not None:
                if ws[1].get("kind") == "laser" and ws[1].get("charge", 0.0) > 0.0:
                    break
                out["drop"] = 1
            out["take"] = pk["id"]
            break

        j = ctx.judge
        hp_frac = me["hp"] / max(1.0, me["max_hp"])
        self.scoop = None
        if j.foe is not None:
            self.goal = None
            self.mode = "fight"
            self._fight_scoop(ctx, x, y, zf, hp_frac)
        else:
            danger = t - j.last_threat_at < 3.0
            self.goal = self._choose(ctx, x, y, zf, hp_frac, danger)
            self.mode = self.goal[0] if self.goal else "-"
        return out

    def _fight_scoop(self, ctx, x, y, zf, hp_frac):
        """Cubes worth walking to mid-fight (they are health, damage and upgrades): a pile of 2+
        within 7 m is worth 110 m, a single cube 40 m; nothing past 30 m below 30% HP; never into
        a lightning carrier's arc; never into a red heat cell. Best = cubes / (distance + 12)."""
        if int(ctx.me.get("cubes", 0)) >= WANT_CUBES + 6:
            return
        no_light = Judge.weapon(ctx.me, "lightning")[0] is None
        carriers = [e for e in ctx.mem.recent_mechs(4.0) if "lightning" in (e.weapons or ())] if no_light else []
        cubes = [c["pos"] for c in ctx.mem.cubes.values()]
        bv = 0.0
        for p in cubes:
            if abs(p[2] - zf) > 3.6:
                continue
            d = math.hypot(p[0] - x, p[1] - y)
            n = sum(1 for o in cubes if math.hypot(o[0] - p[0], o[1] - p[1]) < 7.0)
            if d > (110.0 if n >= 2 else 40.0) or (d > 30.0 and hp_frac < 0.3):
                continue
            if any(math.hypot(e.pos[0] - p[0], e.pos[1] - p[1]) < self.light_range + 8.0 for e in carriers):
                continue
            if not ctx.judge.loot_ok(ctx.heat, p[0], p[1]):
                continue
            v = n / (d + 12.0)
            if v > bv:
                bv, self.scoop = v, p

    def _choose(self, ctx, x, y, zf, hp_frac, danger):
        me, mem, t, wm, heat = ctx.me, ctx.mem, ctx.t, self.wm, ctx.heat
        z = ctx.zone
        cx, cy = z["center"]
        cr = z["radius"]
        hunt = ctx.hunt
        best, best_u = None, 0.0

        mechs_now = list(mem.recent_mechs(2.0))

        def consider(kind, iid, pos, value, handle_m=0.0):
            nonlocal best, best_u
            if self.bad.get(iid, -1.0) > t:
                return
            d = math.hypot(pos[0] - x, pos[1] - y)
            if d > (300.0 if kind == "drop" else 260.0) or math.hypot(pos[0] - cx, pos[1] - cy) > cr - 6.0:
                return
            if pos[2] - wm.ground_at(pos[0], pos[1]) > 3.2:
                return
            if not ctx.judge.loot_ok(heat, pos[0], pos[1]):
                return
            if hunt is not None:
                detour = d + math.hypot(hunt[0] - pos[0], hunt[1] - pos[1]) - math.hypot(hunt[0] - x, hunt[1] - y)
            else:
                detour = d
            if kind != "cube" and any(math.hypot(e.pos[0] - pos[0], e.pos[1] - pos[1]) < 45.0 for e in mechs_now):
                value *= 0.35                 # an enemy stands next to it
            u = value / (max(0.0, detour) + 18.0 + handle_m)
            K = wm.nidx(pos[0], pos[1])
            if K >= 0:
                u /= 1.0 + 0.5 * len(heat.seen_by.get(K, ()))
            if iid == self.loot_id:
                u *= 1.25
            if u > best_u:
                best_u, best = u, (kind, iid, pos)

        ws = me.get("weapons") or []
        slot1_empty = len(ws) < 2 or ws[1] is None
        for iid, pk in mem.pickups.items():
            seek = pk["kind"] == "laser"
            rad = LASER_SEEK if seek else LOOT_R
            if self.want_pickup(me, pk["kind"]) and math.hypot(pk["pos"][0] - x, pk["pos"][1] - y) < rad:
                # weapon value x 4 (laser 6.0, lightning 2.6, gun 2.0), or the gain over what it replaces
                v = WVAL.get(pk["kind"], 2.0)
                if pk["kind"] != "gun" and not slot1_empty:
                    v -= WVAL.get(ws[1].get("kind"), 0.0)
                if v > 0:
                    consider("pickup", iid, pk["pos"], v * 4 * GO_K, 0.5 * WALK_M)
        cube_v = 11.0 * GO_K if int(me.get("cubes", 0)) < WANT_CUBES else 0.0
        # supply drops: full repair + 3 cubes; go if we can be there about when it lands
        for dr in ctx.obs.get("drops") or ():
            holder = dr.get("holder")
            if holder is not None and holder != me.get("id"):
                continue
            dpos = dr["pos"]
            dd = math.hypot(dpos[0] - x, dpos[1] - y)
            if float(dr.get("lands_in", 0.0)) > dd / 9.0 * 1.3 + 6.0 and dr["id"] != self.loot_id:
                continue
            v = 30.0 * GO_K * (1.5 if hp_frac < 0.6 else 1.0)
            consider("drop", dr["id"], (dpos[0], dpos[1], wm.ground_at(dpos[0], dpos[1])), v,
                     (4.0 + float(dr.get("lands_in", 0.0)) * 0.3) * WALK_M)
        for iid, c in mem.cubes.items():
            if math.hypot(c["pos"][0] - x, c["pos"][1] - y) < max(60.0, LOOT_R * 0.7):
                if cube_v > 0:
                    consider("cube", iid, c["pos"], cube_v)
        if hp_frac > FARM_HP and not danger:
            for iid, c in mem.crates.items():
                if math.hypot(c["pos"][0] - x, c["pos"][1] - y) < LOOT_R:
                    consider("crate", iid, c["pos"], 7.0 * GO_K, 2.5 * WALK_M)
            mech_near = any(math.hypot(e.pos[0] - x, e.pos[1] - y) < 150.0 for e in mem.recent_mechs(10.0))
            if int(me.get("cubes", 0)) < WANT_CUBES and not mech_near:   # mechs before drones
                for tr in mem.tracks.values():
                    if tr.kind == "npc" and t - tr.seen_t < 12.0 and math.hypot(tr.pos[0] - x, tr.pos[1] - y) < LOOT_R:
                        consider("npc", tr.id, tr.pos, 11.0 * GO_K, 5.0 * WALK_M)
        if best is None:
            self.loot_id = None
            return None
        if best[1] != self.loot_id:
            self.loot_id, self.loot_since = best[1], t
        elif t - self.loot_since > 30.0:
            self.bad[best[1]] = t + 60.0
            self.loot_id = None
            return None
        return best
