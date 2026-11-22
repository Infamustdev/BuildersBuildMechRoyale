"""Economy: upgrades, weapon pickups, and what loot is worth walking to.

Owns the action fields `pick`, `take`, `drop`, and publishes a loot goal the legs read next tick.

- Upgrades: fixed order, laser line first (plating, armor, laser damage/charge, nanites, gun...).
- Pickups: take a weapon we are standing on if we want it. Ranking laser > lightning > mortar,
  never mortar; the gun only into an empty slot 0. Never drop a laser mid-charge.
- Loot (no fight on): utility = value / (detour + 18), where detour is the extra walk versus
  heading straight to the hunt goal (plain distance when there is none), divided again by
  1 + 0.5 x enemies that can see the item's cell. Values: weapon 130 (60 if slot 1 is full,
  150 for a laser up to 240 m), cube 75 (25 at the cube cap, within 105 m), crate 45 and a drone
  85 (only above 50% HP, no danger, within 150 m). Same item held 30 s without reaching it ->
  banned 60 s. Items off the ground (roofs) or near the zone edge are skipped.
- Fight scoop: during a fight, the nearest cube within 70 m (40 m below half HP), unless a
  lightning carrier stands next to it and we have no lightning.
- Heat: never walk to loot in a red cell; when pressed (retreating, or our cell amber/red) only
  to loot in a cooler cell than ours (judge.loot_ok). Safety before loot.
"""
import math

from judge import Judge

UPGRADE_ORDER = (
    "plating", "armor", "laser_damage", "laser_charge", "nanites",
    "gun_damage", "gun_rate", "lightning_efficiency", "lightning_range",
    "servos", "gun_mag", "gun_velocity", "gyros", "lightning_chain",
    "reactor", "capacitor", "mortar_reload", "mortar_splash",
    "mortar_velocity", "sensors", "jets", "laser_pierce",
)
RANK = {"laser": 4, "lightning": 3, "mortar": 1}
LOOT_R = 150.0
LASER_SEEK = 240.0
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
            ranks = [UPGRADE_ORDER.index(c.get("id")) if c.get("id") in UPGRADE_ORDER else 99
                     for c in offer["choices"]]
            out["pick"] = ranks.index(min(ranks))

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
        r = FIGHT_LOOT * (0.57 if hp_frac <= 0.5 else 1.0)
        no_light = Judge.weapon(ctx.me, "lightning")[0] is None
        carriers = [e for e in ctx.mem.recent_mechs(0.01) if "lightning" in (e.weapons or ())] if no_light else []
        for c in ctx.mem.cubes.values():
            p = c["pos"]
            if abs(p[2] - zf) > 3.6:
                continue
            d = math.hypot(p[0] - x, p[1] - y)
            if d >= r:
                continue
            if any(math.hypot(e.pos[0] - p[0], e.pos[1] - p[1]) < self.light_range + 4.0 for e in carriers):
                continue
            if not ctx.judge.loot_ok(ctx.heat, p[0], p[1]):
                continue
            r, self.scoop = d, p

    def _choose(self, ctx, x, y, zf, hp_frac, danger):
        me, mem, t, wm, heat = ctx.me, ctx.mem, ctx.t, self.wm, ctx.heat
        z = ctx.zone
        cx, cy = z["center"]
        cr = z["radius"]
        hunt = ctx.hunt
        best, best_u = None, 0.0

        def consider(kind, iid, pos, value):
            nonlocal best, best_u
            if self.bad.get(iid, -1.0) > t:
                return
            d = math.hypot(pos[0] - x, pos[1] - y)
            if d > 260.0 or math.hypot(pos[0] - cx, pos[1] - cy) > cr - 6.0:
                return
            if pos[2] - wm.ground_at(pos[0], pos[1]) > 3.2:
                return
            if not ctx.judge.loot_ok(heat, pos[0], pos[1]):
                return
            if hunt is not None:
                detour = d + math.hypot(hunt[0] - pos[0], hunt[1] - pos[1]) - math.hypot(hunt[0] - x, hunt[1] - y)
            else:
                detour = d
            u = value / (max(0.0, detour) + 18.0)
            K = wm.nidx(pos[0], pos[1])
            if K >= 0:
                u /= 1.0 + 0.5 * len(heat.seen_by.get(K, ()))
            if iid == self.loot_id:
                u *= 1.35
            if u > best_u:
                best_u, best = u, (kind, iid, pos)

        ws = me.get("weapons") or []
        slot1_empty = len(ws) < 2 or ws[1] is None
        for iid, pk in mem.pickups.items():
            seek = pk["kind"] == "laser"
            rad = LASER_SEEK if seek else LOOT_R
            if self.want_pickup(me, pk["kind"]) and math.hypot(pk["pos"][0] - x, pk["pos"][1] - y) < rad:
                v = 130.0 if slot1_empty else 60.0
                consider("pickup", iid, pk["pos"], max(v, 150.0) if seek else v)
        cube_v = 25.0 if int(me.get("cubes", 0)) >= self.cube_cap else 75.0
        for iid, c in mem.cubes.items():
            if math.hypot(c["pos"][0] - x, c["pos"][1] - y) < max(60.0, LOOT_R * 0.7):
                consider("cube", iid, c["pos"], cube_v)
        if hp_frac > FARM_HP and not danger:
            for iid, c in mem.crates.items():
                if math.hypot(c["pos"][0] - x, c["pos"][1] - y) < LOOT_R:
                    consider("crate", iid, c["pos"], cube_v * 0.6)
            if int(me.get("cubes", 0)) < self.cube_cap:
                for tr in mem.tracks.values():
                    if tr.kind == "npc" and t - tr.seen_t < 12.0 and math.hypot(tr.pos[0] - x, tr.pos[1] - y) < LOOT_R:
                        consider("npc", tr.id, tr.pos, 85.0)
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
