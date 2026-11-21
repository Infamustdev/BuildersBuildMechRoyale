"""Mech Royale bot: judgement system + four state machines (movement, aiming, shooting, loot).

Movement : EarlySetup, Rotating, Flanking, Fighting, Finishing (+ Retreating for unfair fights)
Aiming   : Scanning, LockingOn, SearchingItems, Fighting
Shooting : Hold, Fighting, ThirdPartying, Finishing, Looting
Loot     : SettingUp, AfterKill, Looting, None

Every tunable number lives in tuning.py.
"""
import json
import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mechfight as mf  # noqa: E402
from combat import Combat  # noqa: E402
from danger import DangerMap  # noqa: E402
from recon import Recon  # noqa: E402
import utility  # noqa: E402
from tracker import Tracker, angdiff  # noqa: E402
from tuning import COVER, DANGER, JUDGE, LOOT, MOVE, PROBE, SCAN, SHOOT, UPGRADE_PRIORITY, UTIL  # noqa: E402
from worldmap import WorldMap  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def find_map_dir(hello):
    cands = [hello.get("map", {}).get("dir", "/arena"), "/arena", os.path.join(HERE, "arena")]
    for c in cands:
        if c and os.path.exists(os.path.join(c, "v2", "world.json")):
            return c
    return cands[0]


def unit(dx, dy):
    d = math.hypot(dx, dy)
    return (dx / d, dy / d) if d > 1e-6 else (0.0, 0.0)


class Situation:
    """Output of the judgement system for one decision."""

    def __init__(self):
        self.threats = []        # list of dicts (see assess)
        self.target = None       # best enemy to shoot at
        self.lock = None         # auto-lock: best visible enemy in line of sight and range
        self.engaged = []        # enemies fighting us
        self.adv = 9.0           # fight advantage vs target
        self.multi_angle = False
        self.unseen_dps = 0.0
        self.n_exposed = 0       # enemies who can see us right now
        self.threat_bearing = None   # where the last shot at us came from
        self.npc_dps = 0.0       # damage a drone is doing to us right now
        self.npc_by = None       # which drone
        self.finish = None
        self.third = None
        self.flank = None
        self.zone_need = 0.0     # speed needed to stay ahead of the zone
        self.outside = False
        self.hp_frac = 1.0


class Bot:
    name = "oval-sentinel"

    def __init__(self, hello):
        self.cfg = cfg = hello["config"]
        self.rng = random.Random(hello.get("seed", 0))
        self.wm = WorldMap(find_map_dir(hello), step_up=cfg.get("mech", {}).get("step_up", 1.2))
        self.tr = Tracker(self.wm, cfg)
        self.cb = Combat(cfg, self.wm)
        self.dm = DangerMap(self.wm, self.tr)
        self.rc = Recon(self.wm)
        vis = cfg.get("vision", {})
        self.vis_range = float(vis.get("range", 200.0))
        self.vis_fov = float(vis.get("fov_deg", 120.0))
        self.pickup_r = cfg.get("loot", {}).get("pickup_radius", 4.0)
        self.step_speed = cfg.get("sound", {}).get("step_speed", 5.0)
        self.front_arc = math.radians(cfg.get("mech", {}).get("front_arc_deg", 180.0)) / 2
        self.move_state = "EarlySetup"
        self.aim_state = "Scanning"
        self.shoot_state = "Hold"
        self.loot_state = "SettingUp"
        self.reached_camp = False
        self.camp = None          # (x, y, z_top or None, score)
        self.camp_t = -1e9
        self.zone_key = None
        self.path = []
        self.path_goal = None
        self.path_t = -1e9
        self.strafe = 1
        self.strafe_until = 0.0
        self.last_pos = None
        self.stuck_t = 0.0
        self.climb = None         # roof spot we're climbing
        self.roof_fail = {}
        self.retreat_pt = None
        self.retreat_t = -1e9
        self.last_danger_t = -1e9
        self.cover = None         # active peek-shoot-hide plan
        self.plan = None          # current utility destination (utility.Option)
        self.plan_t = -1e9
        self.plan_key = None
        self.plan_d0 = 0.0      # how far the current destination was when we chose it
        self.scan_t0 = None     # opening aerial survey: start time, ground level, done flag
        self.scan_z0 = 0.0
        self.scan_done = not SCAN["enabled"]
        self.scan_top = False
        self.land_pt = None     # where the descent is steering for
        self.land_t = -1e9
        self.hold_t0 = None     # when we last started holding still on a camp/cover spot
        self.loot_target = None
        self.last_target_t = -1e9
        self.idle_t = 0.0
        self.slow = False
        self.state_time = {}
        self.max_ms = 0.0
        self.n_ticks = 0
        self.last_log = ""
        mf.log(f"[init] map built in {self.wm.build_s:.2f}s, roofs={len(self.wm.roof_spots)}")
        # Local debugging only: MECH_DUMP=<file> makes the bot write its minimap twice a second.
        # Never set in a real match.
        self.dump = None
        self.dump_t = -1e9
        if os.environ.get("MECH_DUMP"):
            try:
                self.dump = open(os.environ["MECH_DUMP"], "w")
                self.dump.write(json.dumps({"type": "mind_meta", "x0": self.wm.x0, "y0": self.wm.y0,
                                            "N": self.wm.N, "nnx": self.wm.nnx, "nny": self.wm.nny}) + "\n")
            except OSError:
                self.dump = None

    # ================================================================ judgement
    def assess(self, obs):
        me = obs["self"]
        tr = self.tr
        t = obs["t"]
        sit = Situation()
        px, py, pz = me["pos"]
        sit.hp_frac = me["hp"] / max(1.0, me["max_hp"])
        armor = me.get("stats", {}).get("armor", 0.0)
        ehp = me["hp"] / max(0.05, 1.0 - armor)
        eye = (px, py, pz + me.get("stats", {}).get("eye_height", 8.0))
        my_yaw = me["aim"][0]

        threats = []
        for e in tr.enemies():
            ex, ey, ez = e.pos
            dx, dy = ex - px, ey - py
            d = math.hypot(dx, dy)
            if d > 400:
                continue
            b_to_me = math.atan2(-dy, -dx)
            facing = abs(angdiff(e.aim[0], b_to_me)) < math.radians(JUDGE["facing_deg"])
            closing = -(dx * (e.vel[0] - me["vel"][0]) + dy * (e.vel[1] - me["vel"][1])) / max(d, 1.0)
            attacking = (t - e.last_hit_us_t) <= JUDGE["attack_memory_s"]
            los = self.wm.los(eye, (ex, ey, ez + 8.0))
            we_in_rear = abs(angdiff(b_to_me, e.aim[0])) > self.front_arc
            they_in_rear = abs(angdiff(math.atan2(dy, dx), my_yaw)) > self.front_arc
            their = self.cb.enemy_dps(e.weapons, d, e.cubes) * (1.0 - armor)
            if they_in_rear and los:
                their *= JUDGE["rear_mult"]
            ours = self.cb.our_dps(me, d, e.vel, e.cubes)
            if we_in_rear:
                ours *= JUDGE["rear_mult"]
            engaged = attacking or (los and d < JUDGE["engage_range"] and (facing or (closing > JUDGE["approach_speed"] and d < 140)))
            # engagement memory: a fight doesn't end because the enemy looked away for a tick
            if engaged:
                e.engaged_until = t + JUDGE["engage_memory_s"]
            elif t < e.engaged_until and d < JUDGE["engage_range"]:
                engaged = True
            threats.append({
                "tr": e, "d": d, "bearing": math.atan2(dy, dx), "facing": facing, "closing": closing,
                "attacking": attacking, "los": los, "engaged": engaged, "their": their, "ours": ours,
                "we_rear": we_in_rear, "hp": e.hp, "hp_frac": e.hp / max(1.0, e.max_hp),
                "fighting_other": (not attacking) and e.losing_hp(t) > 90,
            })
        sit.threats = threats
        sit.engaged = [h for h in threats if h["engaged"]]

        # damage from attackers we can't see
        recent = tr.recent_attackers()
        visible_ids = {h["tr"].id for h in threats}
        unseen = [a for a in recent if a[1] not in visible_ids and a[3] not in ("npc",)]
        sit.unseen_dps = sum(a[4] for a in unseen) / JUDGE["attack_memory_s"]
        # Drone fire is kept out of unseen_dps on purpose -- a drone must not make us flee a
        # mech fight -- but it was then invisible to everything else too, and we would walk
        # across the map being shot by one without ever looking at it. It damages only us
        # (the squad is immune), so every point of it is a straight loss, and the drone is
        # two cubes if we turn around and kill it.
        npc_hits = [a for a in recent if a[3] == "npc"]
        sit.npc_dps = sum(a[4] for a in npc_hits) / JUDGE["attack_memory_s"]
        sit.npc_by = npc_hits[-1][1] if npc_hits else None
        bearings = [a[2] for a in recent if a[2] is not None]
        if len(bearings) >= 2:
            spread = max(abs(angdiff(a, b)) for a in bearings for b in bearings)
            ids = {a[1] for a in recent if a[1] is not None}
            sit.multi_angle = len(ids) >= 2 and spread > math.radians(JUDGE["multi_angle_deg"])

        incoming = sum(h["their"] for h in sit.engaged) + sit.unseen_dps
        n_exposed = sum(1 for h in threats if h["los"] and h["d"] < 220 and (h["facing"] or h["engaged"]))
        sit.n_exposed = n_exposed
        sit.threat_bearing = bearings[-1] if bearings else None

        # pick the target and its advantage
        best, bscore = None, -1e9
        for h in threats:
            if not h["los"] and not h["engaged"]:
                continue
            ttk_us = h["hp"] / max(1.0, h["ours"])
            ttk_them = ehp / max(1.0, incoming if h["engaged"] else incoming + h["their"])
            adv = ttk_them / ttk_us
            adv *= max(0.2, 1.0 - JUDGE["exposure_penalty"] * max(0, n_exposed - 1))
            h["adv"] = adv
            score = (3.0 if h["attacking"] else 0.0) + (2.0 if h["engaged"] else 0.0) \
                + (2.5 if h["hp_frac"] < 0.3 else 0.0) - h["d"] / 80.0 + (1.0 if h["los"] else -2.0)
            if score > bscore:
                bscore, best = score, h
        sit.target = best
        # auto-lock: any enemy mech we can see and hit; prefer attackers, low HP, then closest
        lk, lscore = None, -1e9
        for h in threats:
            if not h["los"] or h["d"] > SHOOT["lock_range"]:
                continue
            sc = (3.0 if h["attacking"] else 0.0) + 2.0 * (1.0 - h["hp_frac"]) - h["d"] / 60.0
            if sc > lscore:
                lscore, lk = sc, h
        sit.lock = lk
        if best is not None:
            sit.adv = best.get("adv", 1.0)
            fin = (best["hp"] <= JUDGE["finish_hp_abs"] or best["hp_frac"] <= JUDGE["finish_hp_frac"])
            if fin and best["d"] <= JUDGE["finish_max_dist"] and best["los"] and (len(sit.engaged) <= 1 or best["engaged"]):
                sit.finish = best
        for h in threats:
            if h["fighting_other"] and h["los"] and h["hp_frac"] <= JUDGE["third_party_hp_frac"] and h["d"] < 200:
                if sit.third is None or h["hp"] < sit.third["hp"]:
                    sit.third = h
            if (not h["engaged"]) and h["los"] and h.get("adv", 0) >= JUDGE["flank_min_adv"] \
                    and h["d"] < MOVE["flank_max_dist"] and not h["facing"]:
                if sit.flank is None or h["d"] < sit.flank["d"]:
                    sit.flank = h

        # zone pressure
        z = obs["zone"]
        ncx, ncy = z["next_center"]
        dn = math.hypot(px - ncx, py - ncy)
        out_next = dn - z["next_radius"] + 3.0
        sit.outside = not me.get("in_zone", True)
        tleft = max(1.0, z["shrink_end"] - t)
        sit.zone_need = max(0.0, out_next) / tleft
        if sit.outside:
            sit.zone_need = 99.0
        return sit

    # ================================================================ state selection
    def choose_states(self, obs, sit):
        t = obs["t"]
        me = obs["self"]
        walk = me.get("stats", {}).get("walk_speed", 11.7)
        prev = self.move_state
        st = None
        if sit.outside:
            st = "Rotating"
        retreat_now = False
        low = sit.hp_frac < JUDGE["low_hp_frac"]
        danger = bool(sit.engaged) or sit.unseen_dps > 0
        # fighting from cover takes far less damage: count that in the retreat decision
        adv = sit.adv
        if self.cover is not None and sit.target is not None and self.cover["tid"] == sit.target["tr"].id:
            adv *= COVER["adv_bonus"]
        if danger:
            self.last_danger_t = t
            if sit.finish is None and (adv < JUDGE["flee_adv"] or (low and adv < JUDGE["low_hp_flee_adv"])
                                       or (sit.multi_angle and len(sit.engaged) + (1 if sit.unseen_dps > 0 else 0) >= 2)):
                retreat_now = True
        if prev == "Retreating" and not retreat_now:
            # hysteresis: stay hidden until healed, unless the fight turned clearly winnable
            healed = sit.hp_frac >= MOVE["retreat_done_hp"] and (t - self.last_danger_t) > 2.0
            can_win = (sit.target is not None and sit.target["engaged"] and not low
                       and adv > JUDGE["flee_adv"] + JUDGE["flee_hysteresis"])
            if not healed and not can_win and sit.finish is None:
                retreat_now = True
        if st is None:
            if retreat_now:
                st = "Retreating"
            elif sit.finish is not None:
                st = "Finishing"
            elif sit.engaged and sit.target is not None and sit.target["engaged"]:
                st = "Fighting"
            elif sit.third is not None and sit.third["d"] < JUDGE["finish_max_dist"]:
                st = "Finishing"
            elif sit.flank is not None and sit.hp_frac > 0.6:
                st = "Flanking"
            elif t < MOVE["early_phase_until"] and not self.reached_camp:
                st = "EarlySetup"
            else:
                st = "Rotating"
        if sit.zone_need > walk * MOVE["zone_boost_ratio"] and st not in ("Fighting", "Finishing"):
            st = "Rotating"
        if st == "Retreating" and prev != "Retreating":
            self.retreat_pt = None
            self.retreat_t = -1e9
        if st != "Fighting":
            self.cover = None
        self.move_state = st
        self.shoot_aim_states(sit)

    def shoot_aim_states(self, sit):
        st = self.move_state
        # shooting state
        if sit.finish is not None:
            self.shoot_state = "Finishing"
        elif sit.target is not None and sit.target["engaged"]:
            self.shoot_state = "Fighting"
        elif sit.third is not None:
            self.shoot_state = "ThirdPartying"
        elif sit.lock is not None:
            self.shoot_state = "Fighting"  # auto-lock: shoot any enemy we can see and hit
        elif self.loot_target is not None and self.loot_target[0] in ("crate", "npc"):
            self.shoot_state = "Looting"
        else:
            self.shoot_state = "Hold"

        # aiming state
        if self.shoot_state in ("Fighting", "Finishing", "ThirdPartying"):
            self.aim_state = "Fighting"
        elif self.shoot_state == "Looting" and self.loot_target is not None and self.loot_target[3] < 70:
            self.aim_state = "Fighting"
        elif sit.target is not None or st == "Retreating" or st == "Flanking":
            self.aim_state = "LockingOn"
        elif (sit.unseen_dps > 0 or sit.npc_dps > 0) and sit.threat_bearing is not None:
            # being shot at by someone we cannot see outranks both looking for loot and the
            # idle sweep: turn to where it came from, so the next tick can shoot back
            self.aim_state = "Checking"
        elif self.loot_target is not None:
            self.aim_state = "SearchingItems"
        else:
            self.aim_state = "Scanning"

    # ================================================================ movement helpers
    def speed_mode(self, obs, sit):
        """Returns (move_scale, boost) following the silent-movement rule."""
        me = obs["self"]
        walk = me.get("stats", {}).get("walk_speed", 11.7)
        creep = max(0.15, (self.step_speed - MOVE["creep_margin"]) / walk)
        st = self.move_state
        energy = me.get("energy", 0)
        if sit.outside or sit.zone_need > walk * MOVE["zone_boost_ratio"]:
            return 1.0, energy > 5
        if st == "Retreating" or sit.multi_angle:
            return 1.0, energy > MOVE["fight_boost_energy_min"] * 0.5
        if st in ("Fighting",):
            return 1.0, energy > MOVE["fight_boost_energy_min"]
        if st == "Finishing":
            far = sit.finish is not None and sit.finish["d"] > 60
            return 1.0, far and energy > MOVE["fight_boost_energy_min"]
        if sit.zone_need > creep * walk * MOVE["zone_walk_ratio"]:
            return 1.0, False
        if self.loot_state == "AfterKill":
            return 1.0, False  # enemies already know where the fight was: grab the cubes fast
        px, py = me["pos"][0], me["pos"][1]
        near = self.tr.threat_near(px, py, MOVE["stealth_radius"])
        hot = self.tr.heat_near(px, py, MOVE["stealth_radius"] * 0.6) > 0.8
        if near < MOVE["stealth_radius"] or hot or st == "Flanking":
            return creep, False
        return 1.0, False

    def act_hide(self, obs):
        """Probe: hide. Silent creep to a covered ground spot by the next zone edge; never shoot or loot."""
        t0 = time.perf_counter()
        me = obs["self"]
        px, py = me["pos"][0], me["pos"][1]
        walk = me.get("stats", {}).get("walk_speed", 11.7)
        self.tr.update(obs)
        sit = self.assess(obs)
        camp = self.pick_camp(obs, allow_roof=False)
        act = {"tick": obs["tick"]}
        mx, my = self.follow(obs, camp[0], camp[1])
        if math.hypot(camp[0] - px, camp[1] - py) < MOVE["arrive_radius"]:
            mx = my = 0.0
        mx, my = self.avoid_walls(px, py, mx, my)
        creep = max(0.15, (self.step_speed - MOVE["creep_margin"]) / walk)
        scale, boost = creep, False
        if sit.outside or sit.zone_need > walk * MOVE["zone_boost_ratio"]:
            scale, boost = 1.0, me.get("energy", 0) > 5
        elif sit.zone_need > creep * walk * MOVE["zone_walk_ratio"]:
            scale = 1.0
        self.move_state = "Hiding"
        act["move"] = [round(mx * scale, 3), round(my * scale, 3)]
        act["boost"] = bool(boost and (mx or my))
        act["aim"] = [round(me["aim"][0] + 1.2, 4), 0.0]
        act["fire"] = [False, False]
        offer = obs.get("offer")
        if offer and offer.get("choices"):
            act["pick"] = max(range(len(offer["choices"])),
                              key=lambda i: UPGRADE_PRIORITY.get(offer["choices"][i].get("id"), 1.0))
        self.state_time["Hiding"] = self.state_time.get("Hiding", 0) + 1
        mf.draw.label(f"HIDE hp{me['hp']:.0f} alive{obs['alive']}")
        ms = (time.perf_counter() - t0) * 1000
        self.max_ms = max(self.max_ms, ms)
        self.slow = ms > 25
        self.n_ticks += 1
        return act

    def pick_camp(self, obs, force=False, allow_roof=True):
        """Camp spot near the edge of the next zone circle, scored by travel, cover, height, heat."""
        t = obs["t"]
        z = obs["zone"]
        key = (round(z["next_center"][0]), round(z["next_center"][1]), round(z["next_radius"]))
        if not force and key == self.zone_key and t - self.camp_t < MOVE["camp_reeval_s"] and self.camp:
            return self.camp
        me = obs["self"]
        px, py = me["pos"][0], me["pos"][1]
        cx, cy = z["next_center"]
        R = z["next_radius"]
        if R < 30:
            spot = (cx, cy, None, 0.0)
            self.camp, self.camp_t, self.zone_key = spot, t, key
            return spot
        margin = min(MOVE["edge_margin_max"], max(MOVE["edge_margin_min"], R * MOVE["edge_margin_frac"]))
        base = math.atan2(py - cy, px - cx)
        cands = []
        arc = math.radians(MOVE["camp_arc_deg"])
        for k in range(-7, 8):
            a = base + arc * k / 7.0
            for rr in (R - margin, R - margin * 1.8):
                x, y = cx + math.cos(a) * rr, cy + math.sin(a) * rr
                K = self.wm.nearest_open(x, y, 3)
                if K < 0:
                    continue
                x, y = self.wm.ncenter(K)
                cands.append((x, y, None))
        energy_ok = allow_roof and MOVE["use_roofs"] and me.get("max_energy", 100) >= MOVE["roof_energy_min"]
        mm = MOVE["map_margin"]
        wm = self.wm
        cands = [c for c in cands if wm.x0 + mm <= c[0] <= wm.x1 - mm and wm.y0 + mm <= c[1] <= wm.y1 - mm]
        for (x, y, top, h) in self.wm.roof_spots:
            dc = math.hypot(x - cx, y - cy)
            if not (R * 0.55 <= dc <= R - margin * 0.6):
                continue
            if not (MOVE["roof_min_h"] <= h <= MOVE["roof_max_h"]) or not energy_ok:
                continue
            if self.roof_fail.get((round(x), round(y)), -1e9) > t - 60:
                continue
            if abs(angdiff(math.atan2(y - cy, x - cx), base)) > arc:
                continue
            cands.append((x, y, top))
        best, bs = None, -1e9
        for (x, y, top) in cands:
            dist = math.hypot(x - px, y - py)
            s = -MOVE["w_travel"] * dist / 100.0
            if top is None:
                s += MOVE["w_cover"] * self.wm.cover(x, y)
                K = self.wm.nidx(x, y)
                if K >= 0 and self.wm.canopy[K]:
                    s += MOVE["w_canopy"]
            else:
                s += MOVE["w_roof"]
            s -= MOVE["w_heat"] * min(2.0, self.tr.heat_near(x, y, 30.0))
            s -= 3.0 * max(0.0, 60.0 - self.tr.threat_near(x, y, 60.0)) / 60.0
            if s > bs:
                bs, best = s, (x, y, top, s)
        if best is None:
            best = (cx, cy, None, 0.0)
        old_failed = self.camp is not None and self.camp[2] is not None and \
            self.roof_fail.get((round(self.camp[0]), round(self.camp[1])), -1e9) > t - 60
        old_off_map = self.camp is not None and not (
            wm.x0 + mm <= self.camp[0] <= wm.x1 - mm and wm.y0 + mm <= self.camp[1] <= wm.y1 - mm)
        if self.camp and key == self.zone_key and not force and not old_failed and not old_off_map \
                and (self.camp[2] is None or MOVE["use_roofs"]):
            # rescore the current spot; keep it unless the new one is clearly better
            ox, oy, otop, _ = self.camp
            os_ = -MOVE["w_travel"] * math.hypot(ox - px, oy - py) / 100.0 + \
                (MOVE["w_roof"] if otop is not None else MOVE["w_cover"] * self.wm.cover(ox, oy)) - \
                MOVE["w_heat"] * min(2.0, self.tr.heat_near(ox, oy, 30.0))
            if best[3] < os_ + MOVE["camp_switch_gain"] and math.hypot(ox - cx, oy - cy) < R - 2:
                best = self.camp
        if key != self.zone_key:
            self.reached_camp = False
        self.camp, self.camp_t, self.zone_key = best, t, key
        return best

    def follow(self, obs, gx, gy, replan_s=2.5):
        """Direction (unit vector) toward (gx, gy) along a planned path."""
        me = obs["self"]
        px, py = me["pos"][0], me["pos"][1]
        t = obs["t"]
        if math.hypot(gx - px, gy - py) < 2.0:
            return 0.0, 0.0
        need = (self.path_goal is None or math.hypot(gx - self.path_goal[0], gy - self.path_goal[1]) > 8.0
                or t - self.path_t > replan_s or not self.path)
        if need and not self.slow:
            # straight line only if it's walkable AND not through enemy sight lines
            if self.wm.walk_clear(px, py, gx, gy) and (
                    not self.dm.any() or self.dm.line_cost(px, py, gx, gy) < DANGER["line_cost_max"]):
                self.path = [(gx, gy)]
            else:
                self.path = self.wm.path(px, py, gx, gy, budget_ms=DANGER["path_budget_ms"],
                                         avoid=self.dm.cost if self.dm.any() else None)
            self.path_goal = (gx, gy)
            self.path_t = t
        while len(self.path) > 1 and math.hypot(self.path[0][0] - px, self.path[0][1] - py) < 3.0:
            self.path.pop(0)
        if not self.path:
            return unit(gx - px, gy - py)
        wx, wy = self.path[0]
        return unit(wx - px, wy - py)

    def avoid_walls(self, px, py, mx, my):
        """If the move direction runs into a wall within 4 m, slide along it."""
        if mx == 0 and my == 0:
            return mx, my
        if not self.wm.is_solid(px + mx * 4.0, py + my * 4.0):
            return mx, my
        for da in (0.6, -0.6, 1.2, -1.2, 1.8, -1.8):
            a = math.atan2(my, mx) + da
            cx, cy = math.cos(a), math.sin(a)
            if not self.wm.is_solid(px + cx * 4.0, py + cy * 4.0):
                return cx, cy
        return mx, my

    def in_zone_after(self, obs, x, y, slack=4.0):
        z = obs["zone"]
        return math.hypot(x - z["center"][0], y - z["center"][1]) < z["radius"] - slack

    def retreat_point(self, obs, sit):
        me = obs["self"]
        px, py, pz = me["pos"]
        srcs = [(h["tr"].pos[0], h["tr"].pos[1], h["tr"].pos[2] + 8.0) for h in sit.threats if h["engaged"] or h["los"]]
        for a in self.tr.recent_attackers():
            if a[2] is not None and a[1] not in {h["tr"].id for h in sit.threats}:
                srcs.append((px + math.cos(a[2]) * 100, py + math.sin(a[2]) * 100, pz + 10))
        if not srcs:
            return None
        ax = sum(s[0] for s in srcs) / len(srcs)
        ay = sum(s[1] for s in srcs) / len(srcs)
        away = math.atan2(py - ay, px - ax)
        best, bs = None, -1e9
        for r in MOVE["retreat_search_r"]:
            for k in range(-6, 7):
                a = away + k * math.pi / 8
                x, y = px + math.cos(a) * r, py + math.sin(a) * r
                K = self.wm.nearest_open(x, y, 2)
                if K < 0:
                    continue
                x, y = self.wm.ncenter(K)
                if not self.in_zone_after(obs, x, y):
                    continue
                g = self.wm.ground_at(x, y)
                hidden = sum(1 for s in srcs if not self.wm.los(s, (x, y, g + 7.0)))
                s = hidden * 3.0 - r / 15.0 + self.wm.cover(x, y) - abs(k) * 0.1
                if s > bs:
                    bs, best = s, (x, y)
        return best

    # ================================================================ loot
    def choose_loot(self, obs, sit, dest):
        me = obs["self"]
        t = obs["t"]
        px, py, pz = me["pos"]
        tr = self.tr
        if self.move_state not in ("EarlySetup", "Rotating"):
            self.loot_state = "None"
            return None
        mult = LOOT["early_detour_mult"] if self.move_state == "EarlySetup" else 1.0
        dx, dy = dest if dest else (px, py)
        base = math.hypot(dx - px, dy - py)
        w1 = (me.get("weapons") or [None, None])
        have = {w["kind"] for w in w1 if w}
        slot1 = w1[1]["kind"] if len(w1) > 1 and w1[1] else None
        rank = LOOT["weapon_rank"]
        cand = []

        def free(x, y, r=LOOT["free_radius"]):
            # free = no enemy right next to it and the spot isn't in enemy sight lines / hot sound area
            return tr.threat_near(x, y, r) > r and self.dm.at(x, y) < DANGER["loot_max"]

        def detour(x, y):
            return math.hypot(x - px, y - py) + math.hypot(dx - x, dy - y) - base

        for pid, p in tr.pickups.items():
            k = p["kind"]
            if k in have or rank.get(k, 0) <= (rank.get(slot1, -1) if slot1 else -1):
                continue
            x, y, z = p["pos"]
            if z - self.wm.ground_at(x, y) > 4 and abs(z - pz) > 4:
                continue  # on a roof we're not on
            det = detour(x, y)
            lim = LOOT["no_weapon_detour"] if slot1 is None else LOOT["max_detour"]["weapon"] * mult
            if det <= lim and free(x, y):
                cand.append((3.0 + rank.get(k, 0) + (2.0 if slot1 is None else 0.0), det, ("weapon", pid, (x, y, z))))
        recent_kill = any(t - kt < 30 for kt, _ in tr.kill_spots)
        for cid, c in tr.cubes.items():
            x, y, z = c["pos"]
            if z - self.wm.ground_at(x, y) > 4 and abs(z - pz) > 4:
                continue
            det = detour(x, y)
            lim = LOOT["after_kill_detour"] if recent_kill else LOOT["max_detour"]["cube"] * mult
            if det <= lim and free(x, y):
                cand.append((2.0, det, ("cube", cid, (x, y, z))))
        for cid, c in tr.crates.items():
            x, y, z = c["pos"]
            if z - self.wm.ground_at(x, y) > 4 and abs(z - pz) > 4:
                continue
            det = detour(x, y)
            if det <= LOOT["max_detour"]["crate"] * mult and free(x, y):
                cand.append((1.2, det, ("crate", cid, (x, y, z))))
        if sit.hp_frac >= LOOT["npc_min_hp_frac"]:
            for n in tr.npcs():
                x, y, z = n.pos
                det = detour(x, y)
                if det <= LOOT["max_detour"]["npc"] * mult and free(x, y):
                    cand.append((1.3, det, ("npc", n.id, (x, y, z))))
        for d in tr.drops:
            x, y, z = d["pos"]
            if not self.in_zone_after(obs, x, y, 10):
                continue
            if d.get("holder") not in (None, me["id"]):
                continue
            det = detour(x, y)
            if det <= LOOT["max_detour"]["drop"] * mult and free(x, y, LOOT["drop_free_radius"]) \
                    and d.get("lands_in", 0) <= math.hypot(x - px, y - py) / 10.0 + 2:
                cand.append((2.5 + 3.0 * (1 - sit.hp_frac), det, ("drop", d["id"], (x, y, z))))
        if not cand:
            self.loot_target = None
            self.loot_state = "None"
            return None
        cand.sort(key=lambda c: -(c[0] / (1.0 + max(0.0, c[1]) / 50.0)))
        kind, iid, pos = cand[0][2]
        dist = math.hypot(pos[0] - px, pos[1] - py)
        self.loot_target = (kind, iid, pos, dist)
        self.loot_state = "SettingUp" if self.move_state == "EarlySetup" else ("AfterKill" if recent_kill and kind == "cube" else "Looting")
        return self.loot_target

    def take_and_pick(self, obs, act):
        me = obs["self"]
        px, py, pz = me["pos"]
        offer = obs.get("offer")
        if offer and offer.get("choices"):
            best_i, best_v = 0, -1e9
            for i, c in enumerate(offer["choices"]):
                v = UPGRADE_PRIORITY.get(c.get("id"), 1.0)
                if v > best_v:
                    best_i, best_v = i, v
            act["pick"] = best_i
        weapons = me.get("weapons") or [None, None]
        slot1 = weapons[1] if len(weapons) > 1 else None
        if slot1 and slot1.get("kind") == "laser" and (slot1.get("charge") or 0) > 0:
            return
        rank = LOOT["weapon_rank"]
        have = {w["kind"] for w in weapons if w}
        cur = rank.get(slot1["kind"], -1) if slot1 else -1
        best = None
        for p in obs.get("pickups", []):
            x, y, z = p["pos"]
            if math.hypot(x - px, y - py) <= self.pickup_r * 0.95 and abs(z - pz) < 4:
                if p["kind"] in have or rank.get(p["kind"], 0) <= cur:
                    continue
                if best is None or rank.get(p["kind"], 0) > rank.get(best["kind"], 0):
                    best = p
        if best is not None:
            act["take"] = best["id"]
            if slot1:
                act["drop"] = 1

    # ================================================================ main
    def hold_mult(self, obs):
        """What a stand-still spot is still worth, given how long we have held it.

        Its value is in getting there and taking stock, and that is spent once we have. Without
        this, a spot we already occupy pays no travel while every alternative does, so its score
        peaks the instant we arrive and nothing can outbid it: the bot parks. Decaying it means
        the parked state always loses eventually -- to a cube, to a flank, or to going and
        looking at ground we have not seen.
        """
        if self.hold_t0 is None:
            return 1.0
        f = UTIL["hold_floor"]
        return f + (1.0 - f) * math.exp(-(obs["t"] - self.hold_t0) / UTIL["hold_decay_s"])

    def pick_landing(self, obs, px, py):
        """Where to come down: the coolest cell we can glide to, standing still included.

        `dm.at` already carries everything that matters here -- enemy sight lines weighted by
        how healthy they are, the sound heat, where we have been seen, and the negative pull of
        loot nobody is watching -- so the choice is one comparison, not a set of rules.
        """
        best, bx, by = None, px, py
        for i in range(SCAN["glide_rays"]):
            a = 2.0 * math.pi * i / SCAN["glide_rays"]
            ca, sa = math.cos(a), math.sin(a)
            for d in SCAN["glide_dists"]:
                x, y = px + ca * d, py + sa * d
                if d > 0.0 and (self.wm.is_solid(x, y) or not self.in_zone_after(obs, x, y, 8.0)):
                    continue
                s = self.dm.at(x, y) + SCAN["glide_far_cost"] * d / 100.0
                if best is None or s < best:
                    best, bx, by = s, x, y
        return (bx, by)

    def scan_step(self, obs, sit, act):
        """Opening aerial survey: jump, jet to SCAN["alt"], spin the whole way up and down.

        Returns True while the survey owns the mech. Crates, cubes and pickups spawn randomly
        each match and are listed only while in line of sight, so this one climb is the cheapest
        map-wide census there is, and the tracker keeps what it sees. Vision has no pitch limit,
        so a single spin from up here sweeps the whole 200 m circle over the rooflines.
        """
        if self.scan_done:
            return False
        me = obs["self"]
        t, pz = obs["t"], me["pos"][2]
        pz2d = (me["pos"][0], me["pos"][1])
        grounded = me.get("grounded", True)
        if self.scan_t0 is None:
            self.scan_t0, self.scan_z0 = t, pz
        el, alt = t - self.scan_t0, pz - self.scan_z0
        shot_at = me["hp"] < me.get("max_hp", 1400) * SCAN["abort_hp"]
        landed = grounded and el > SCAN["min_air_s"]
        if el > SCAN["max_s"] or shot_at or landed or (sit.engaged and alt < 1.0):
            self.scan_done = True
            return False
        if alt >= SCAN["alt"] or (not grounded and me.get("energy", 0) <= SCAN["energy_floor"]):
            self.scan_top = True        # latch: never jet again on the way down, it doubles
        climbing = not self.scan_top
        if climbing:
            act["move"] = [0.0, 0.0]                      # straight up: drifting costs height
        else:
            # Falling is free steering, and from up here we can see where to come down. The
            # danger map already prices enemy sight lines, where we have been seen, and the
            # pull of loot, so the best landing spot is simply its coolest reachable cell.
            # This is how we leave a spawn that turns out to be surrounded.
            if self.land_pt is None or t - self.land_t >= SCAN["glide_repick_s"]:
                self.land_pt, self.land_t = self.pick_landing(obs, pz2d[0], pz2d[1]), t
            lx, ly = self.land_pt
            dx, dy = lx - pz2d[0], ly - pz2d[1]
            n = math.hypot(dx, dy)
            act["move"] = [round(dx / n, 3), round(dy / n, 3)] if n > 2.0 else [0.0, 0.0]
        act["boost"] = bool(climbing and not grounded)    # boost in the air fires the jets
        if grounded:
            act["jump"] = True
        act["aim"] = [round(me["aim"][0] + 1.2, 4), 0.0]  # saturated turn rate: a circle in 1.6 s
        act["fire"] = [False, False]
        return True

    def act(self, obs):
        if PROBE.get("hide"):
            return self.act_hide(obs)
        t0 = time.perf_counter()
        t = obs["t"]
        me = obs["self"]
        px, py, pz = me["pos"]
        self.tr.update(obs)
        sit = self.assess(obs)
        # stable order, not sorted by distance: the danger map caches its wells on this tuple,
        # and re-sorting every tick would rebuild them every tick for no gain
        want = tuple([(c["pos"][0], c["pos"][1]) for c in self.tr.cubes.values()] +
                     [(p["pos"][0], p["pos"][1]) for p in self.tr.pickups.values()])
        # back off the refresh after a slow tick: knowledge a fraction of a second staler is
        # cheap, a missed 50 ms answer is a tick with no shot
        self.rc.update(t, me["pos"], me["aim"][0], self.vis_range, self.vis_fov,
                       budget=6 if self.slow else 16)
        self.dm.mark_exposure(t, me["pos"], sit.n_exposed + (1 if sit.unseen_dps > 0 else 0))
        self.dm.update(t, want)
        camp = self.pick_camp(obs)
        dest = (camp[0], camp[1])
        self.choose_states(obs, sit)
        act = {"tick": obs["tick"]}
        if self.scan_step(obs, sit, act):
            self.move_state, self.aim_state = "Surveying", "Scanning"
            self.take_and_pick(obs, act)
            self.state_time["Surveying"] = self.state_time.get("Surveying", 0) + 1
            mf.draw.label(f"SCAN alt{me['pos'][2] - self.scan_z0:4.0f} e{me.get('energy', 0):3.0f}"
                          + ("" if self.land_pt is None else " ->land"))
            if self.dump is not None and t - self.dump_t >= 0.5:
                self.dump_t = t
                self.write_mind(obs, t)
            self.max_ms = max(self.max_ms, (time.perf_counter() - t0) * 1000)
            self.n_ticks += 1
            return act
        st = self.move_state
        mx = my = 0.0
        jump = False
        target = sit.target
        walk = me.get("stats", {}).get("walk_speed", 11.7)
        zone_forced = sit.outside or sit.zone_need > walk * MOVE["zone_boost_ratio"]

        # ---------------- movement
        # The planner runs in a fight too: "keep fighting this one" is an option scored against
        # the cubes on the ground, not a state that switches the planner off. Aiming and firing
        # are decided further down regardless, so walking to loot never costs us our shots.
        fight_st = st in ("Fighting", "Finishing")
        if zone_forced and not fight_st:
            self.move_state = st = "Rotating"
            self.hold_t0 = None
            self.loot_target = None
            self.loot_state = "zone"
            mx, my = self.follow(obs, *dest)
            self.shoot_aim_states(sit)
        else:
            # utility planner: one score decides fight / loot / cover / flank / look / camp
            if self.plan is None or t - self.plan_t >= UTIL["replan_s"]:
                best, _ = utility.plan(self, obs, sit, camp)
                key = best.key if best else None
                if key != self.plan_key:
                    self.plan_d0 = math.hypot(best.x - px, best.y - py) if best else 0.0
                self.plan, self.plan_t, self.plan_key = best, t, key
            o = self.plan
            self.loot_target = None
            if o is not None and o.kind != "fight":
                dist = math.hypot(o.x - px, o.y - py)
                if o.item is not None:
                    self.loot_target = (o.kind, o.item[1], (o.x, o.y, o.z), dist)
                self.move_state = {"cover": "Retreating", "flank": "Flanking"}.get(o.kind, "Rotating")
                self.loot_state = o.kind
                on_spot = o.kind in ("camp", "cover") and dist < MOVE["arrive_radius"]
                if not on_spot:
                    self.hold_t0 = None
                elif self.hold_t0 is None:
                    self.hold_t0 = t
                hold = ((o.kind in ("crate", "npc") and dist < LOOT["crate_engage_dist"])
                        or (o.kind == "drop" and dist < 4.0) or on_spot)
                if not hold:
                    mx, my = self.follow(obs, o.x, o.y, replan_s=1.5)
                if o.kind == "camp" and dist < MOVE["arrive_radius"]:
                    self.reached_camp = True
                st = self.move_state
                self.shoot_aim_states(sit)
            else:
                # the fight won the comparison, or there is nothing else worth doing
                self.hold_t0 = None
                has_t = sit.finish is not None or target is not None
                if not fight_st and has_t:
                    self.move_state = st = "Finishing" if sit.finish is not None else "Fighting"
                self.loot_state = "fight" if has_t else "camp"
                h = sit.finish if (st == "Finishing" and sit.finish is not None) else (sit.third if st == "Finishing" and sit.third else target)
                if h is None:
                    mx, my = self.follow(obs, *dest)   # nothing to fight and nothing to want
                    self.shoot_aim_states(sit)
                if h is not None:
                    res = None
                    if st == "Fighting" and h["d"] >= COVER["min_target_dist"]:
                        res = self.cover_move(obs, sit, h)
                    if res is not None:
                        mx, my, jump = res
                    else:
                        self.cover = None
                        mx, my, jump = self.fight_move(obs, h, chase=(st == "Finishing"))
        scale, boost = self.speed_mode(obs, sit)
        if st not in ("Fighting", "Finishing"):
            mx, my = self.avoid_walls(px, py, mx, my)
        # stuck detection
        if self.last_pos is not None and (mx or my):
            moved = math.hypot(px - self.last_pos[0], py - self.last_pos[1])
            if moved < 0.15 and me.get("grounded", True):
                self.stuck_t += 0.1
            else:
                self.stuck_t = 0.0
            if self.stuck_t > 1.2:
                jump = True
                self.path = []
                a = math.atan2(my, mx) + self.rng.choice((-1.2, 1.2))
                mx, my = math.cos(a), math.sin(a)
                if self.stuck_t > 2.5:
                    self.stuck_t = 0.0
        self.last_pos = (px, py)
        act["move"] = [round(mx * scale, 3), round(my * scale, 3)]
        # boost in the air fires the jets (that's how climbing works): only allow it on the
        # ground, or during a deliberate roof climb, otherwise every jump becomes a 30 m flight
        act["boost"] = bool(boost and (mx or my) and (me.get("grounded", True) or self.climb is not None))
        if jump:
            act["jump"] = True

        # ---------------- aiming + shooting
        aim, fire, reload = self.aim_and_shoot(obs, sit, mx, my)
        act["aim"] = [round(aim[0], 4), round(max(-0.69, min(1.29, aim[1])), 4)]
        act["fire"] = fire
        if reload:
            act["reload"] = True
        self.take_and_pick(obs, act)

        # ---------------- bookkeeping / debug
        self.state_time[st] = self.state_time.get(st, 0) + 1
        label = f"{st[:4]}/{self.aim_state[:4]}/{self.shoot_state[:4]}/{self.loot_state[:4]} hp{me['hp']:.0f}"
        if target is not None:
            label += f" T{target['tr'].id} a{sit.adv:.1f}"
        mf.draw.label(label)
        mf.draw.point([camp[0], camp[1], self.wm.surface(camp[0], camp[1]) + 1], "#40c0ff", 6)
        for (x, y, v) in self.tr.hotspots(4):
            mf.draw.point([x, y, self.wm.surface(x, y) + 1], "#ff8000", min(12, 3 + v * 3))
        key = f"{st}|{self.aim_state}|{self.shoot_state}|{self.loot_state}"
        if key != self.last_log:
            mf.log(f"t={t:6.1f} hp={me['hp']:5.0f} {key} tgt={target['tr'].id if target else '-'} adv={sit.adv:.2f} alive={obs['alive']}")
            self.last_log = key
        if self.dump is not None and t - self.dump_t >= 0.5:
            self.dump_t = t
            self.write_mind(obs, t)
        ms = (time.perf_counter() - t0) * 1000
        self.max_ms = max(self.max_ms, ms)
        self.slow = ms > 25
        self.n_ticks += 1
        return act

    def roof_move(self, obs, camp):
        """Walk to the foot of the roof spot, then jump + jet up onto it."""
        me = obs["self"]
        px, py, pz = me["pos"]
        x, y, top, _ = camp
        on_roof = pz >= top - 1.0 and math.hypot(x - px, y - py) < 25
        if on_roof:
            if math.hypot(x - px, y - py) < MOVE["arrive_radius"]:
                self.reached_camp = True
                return 0.0, 0.0, False, False
            dx, dy = unit(x - px, y - py)
            return dx, dy, False, False
        K = self.wm.nearest_open(x, y, 10)
        if K < 0:
            self.roof_fail[(round(x), round(y))] = obs["t"]
            return 0.0, 0.0, False, False
        fx, fy = self.wm.ncenter(K)
        if math.hypot(fx - px, fy - py) > 7.0 and self.climb is None:
            mx, my = self.follow(obs, fx, fy)
            return mx, my, False, False
        # climbing
        if self.climb is None:
            self.climb = obs["t"]
        energy = me.get("energy", 0)
        dx, dy = unit(x - px, y - py)
        if obs["t"] - self.climb > 8.0 or (energy < 4 and pz < top - 3 and not me.get("grounded")):
            self.roof_fail[(round(x), round(y))] = obs["t"]
            self.climb = None
            self.camp_t = -1e9
            return dx, dy, False, False
        jump = bool(me.get("grounded"))
        boost = (pz < top + 1.0) and not me.get("grounded")
        if pz >= top - 0.5:
            self.climb = None
        return dx, dy, jump, boost

    # ================================================================ cover fighting
    def plan_cover(self, obs, sit, h):
        """Find a hide spot H (target can't see us) and the nearest peek spot P next to it
        (our muzzle sees the target). None if there's no usable cover nearby."""
        me = obs["self"]
        px, py = me["pos"][0], me["pos"][1]
        e = h["tr"]
        E = (e.pos[0], e.pos[1], e.pos[2] + 8.0)
        others = [(o["tr"].pos[0], o["tr"].pos[1], o["tr"].pos[2] + 8.0)
                  for o in sit.threats if o is not h and o["los"] and o["d"] < 200]
        A = COVER["angles"]
        best, bs = None, -1e9
        for r in COVER["hide_radii"]:
            for k in range(A):
                a = 2 * math.pi * k / A
                K = self.wm.nearest_open(px + math.cos(a) * r, py + math.sin(a) * r, 1)
                if K < 0:
                    continue
                x, y = self.wm.ncenter(K)
                if not self.in_zone_after(obs, x, y):
                    continue
                g = self.wm.ground_at(x, y)
                if self.wm.los(E, (x, y, g + 4.5)) or self.wm.los(E, (x, y, g + 8.5)):
                    continue  # target could see us there
                hid = sum(1 for O in others if not self.wm.los(O, (x, y, g + 6.0)))
                dT = math.hypot(x - e.pos[0], y - e.pos[1])
                s = -r / 6.0 + 1.5 * hid - abs(dT - h["d"]) / 40.0
                if dT < COVER["min_target_dist"]:
                    s -= 2.0
                if s > bs:
                    bs, best = s, (x, y)
        if best is None:
            return None
        hx, hy = best
        C = (e.pos[0], e.pos[1], e.pos[2] + 4.5)
        for r in COVER["peek_radii"]:
            for k in range(A):
                a = 2 * math.pi * k / A
                x, y = hx + math.cos(a) * r, hy + math.sin(a) * r
                K = self.wm.nidx(x, y)
                if K < 0 or self.wm.nblock[K] or not self.wm.walk_clear(hx, hy, x, y):
                    continue
                if self.wm.los((x, y, self.wm.ground_at(x, y) + 7.0), C):
                    return {"tid": e.id, "H": (hx, hy), "P": (x, y), "tpos": tuple(e.pos), "t": obs["t"],
                            "phase": "peek", "ps": obs["t"], "hs": obs["t"], "dmg": 0.0}
        return None

    def cover_move(self, obs, sit, h):
        """Peek-shoot-hide loop against target h. Returns (mx, my, jump) or None (no cover: strafe)."""
        t = obs["t"]
        me = obs["self"]
        px, py = me["pos"][0], me["pos"][1]
        e = h["tr"]
        cv = self.cover
        replan = (cv is None or cv["tid"] != e.id
                  or math.hypot(e.pos[0] - cv["tpos"][0], e.pos[1] - cv["tpos"][1]) > COVER["target_move_replan"]
                  or (t - cv["t"] > COVER["replan_s"] and not self.slow))
        if replan:
            new = self.plan_cover(obs, sit, h)
            if new is None:
                if cv is None or cv["tid"] != e.id:
                    return None
                cv["t"] = t  # keep the old plan a little longer
            else:
                if cv is not None and cv["tid"] == e.id:
                    for k in ("phase", "ps", "hs", "dmg"):
                        new[k] = cv[k]
                cv = self.cover = new
        # phase changes
        weapons = me.get("weapons") or [None, None]
        gun = next((w for w in weapons if w and w["kind"] == "gun"), None)
        laser = next((w for w in weapons if w and w["kind"] == "laser"), None)
        mag = gun.get("stats", {}).get("mag", 12) if gun else 12
        ammo = gun.get("ammo", mag) if gun else mag
        reloading = bool(gun and gun.get("reload", 0) > 0)
        dmg = sum(ev.get("amount", 0.0) for ev in obs.get("events", [])
                  if ev.get("type") == "damaged" and ev.get("weapon") != "zone")
        if cv["phase"] == "peek":
            cv["dmg"] += dmg
            fired_laser = bool(laser and laser.get("cooldown", 0) > 0.0 and (laser.get("charge") or 0) == 0
                               and laser["cooldown"] > laser.get("stats", {}).get("cooldown", 1.65) - 0.15)
            if (ammo <= 0 or reloading or cv["dmg"] >= COVER["peek_dmg"] or t - cv["ps"] > COVER["peek_max_s"]
                    or sit.hp_frac < JUDGE["low_hp_frac"] or (fired_laser and ammo < mag * 0.5)):
                cv["phase"], cv["hs"] = "hide", t
        else:
            hidden_for = t - cv["hs"]
            healed = sit.hp_frac >= COVER["heal_to"] or hidden_for > COVER["hide_max_s"] * 0.6
            ready = (not reloading and ammo >= mag * COVER["reload_frac"]
                     and hidden_for >= COVER["hide_min_s"] and healed)
            if laser and laser.get("cooldown", 0) <= 0:
                # charge the laser behind cover, then step out and release
                full = laser.get("stats", {}).get("charge", 0.75)
                ready = ready and (laser.get("charge") or 0) >= full * SHOOT["laser_release_charge"]
            if ready or hidden_for > COVER["hide_max_s"]:
                cv["phase"], cv["ps"], cv["dmg"] = "peek", t, 0.0
        # movement
        gx, gy = cv["P"] if cv["phase"] == "peek" else cv["H"]
        d = math.hypot(gx - px, gy - py)
        jump = False
        if cv["phase"] == "peek" and d < 2.5:
            # small strafe around the peek spot so we're not a still target
            if t >= self.strafe_until:
                self.strafe = -self.strafe
                self.strafe_until = t + self.rng.uniform(MOVE["strafe_flip_min"], MOVE["strafe_flip_max"])
            b = math.atan2(e.pos[1] - py, e.pos[0] - px) + self.strafe * math.pi / 2
            mx, my = math.cos(b) * 0.6, math.sin(b) * 0.6
            if self.wm.is_solid(px + mx * 4, py + my * 4):
                mx, my = -mx, -my
            jump = (me.get("grounded") and me.get("energy", 0) > MOVE["jump_energy_min"]
                    and self.rng.random() < MOVE["jump_chance"] * 0.5)
            return mx, my, jump
        if d < 1.2:
            return 0.0, 0.0, False
        mx, my = self.follow(obs, gx, gy, replan_s=1.0)
        return mx, my, False

    def safer_step(self, px, py, sx, sy):
        """Of the few spots a step away on the side we are already circling, the coolest.

        Fighting from one spot and strafing in place is what a target does. This gives the
        strafe somewhere to go: the same circle, biased toward whichever part of it the
        danger map likes -- cover, a building's shadow, away from where we have been seen.
        """
        best, bx, by = None, 0.0, 0.0
        base = math.atan2(sy, sx)
        for da in (-0.9, -0.45, 0.0, 0.45, 0.9):
            a = base + da
            qx, qy = px + math.cos(a) * MOVE["fight_probe_m"], py + math.sin(a) * MOVE["fight_probe_m"]
            if self.wm.is_solid(qx, qy):
                continue
            c = self.dm.at(qx, qy)
            if best is None or c < best:
                best, bx, by = c, math.cos(a), math.sin(a)
        return bx, by

    def fight_move(self, obs, h, chase=False):
        """Strafe perpendicular to the target, drift back, random flips and jumps; chase when finishing."""
        me = obs["self"]
        t = obs["t"]
        px, py, pz = me["pos"]
        e = h["tr"]
        b = math.atan2(e.pos[1] - py, e.pos[0] - px)
        if t >= self.strafe_until:
            # Not a coin flip: a back hit does rear_mult damage, so one side of the circle is
            # worth more than the other. Pick the way that walks us around behind their aim,
            # and keep some randomness so we are not a predictable orbit.
            off = angdiff(b + math.pi, e.aim[0])     # where we sit relative to their facing
            prefer = 1.0 if off > 0 else -1.0
            if self.rng.random() < MOVE["flank_circle_p"]:
                self.strafe = prefer
            else:
                self.strafe = -self.strafe if self.rng.random() < 0.7 else self.strafe
            self.strafe_until = t + self.rng.uniform(MOVE["strafe_flip_min"], MOVE["strafe_flip_max"])
        side = b + self.strafe * math.pi / 2
        sx, sy = math.cos(side), math.sin(side)
        if chase:
            gx, gy = self.follow(obs, e.pos[0], e.pos[1], replan_s=1.0)
            want = 30.0
            k = 1.0 if h["d"] > want else 0.2
            mx, my = gx * k + sx * 0.6, gy * k + sy * 0.6
        else:
            back = -MOVE["strafe_back_bias"]
            # weapons decide preferred range: close in only if out of every weapon's reach
            reach = 0.0
            for w in me.get("weapons") or []:
                if w:
                    reach = max(reach, w.get("stats", {}).get("range", 0.0) * (0.8 if w["kind"] == "gun" else 0.9))
            if h["d"] > reach and reach > 0:
                back = 0.5
            mx, my = sx + math.cos(b) * back, sy + math.sin(b) * back
            # pull the circle toward ground they cannot see us from: the strafe decides the
            # side, the danger map decides which part of that side is worth standing on
            gx, gy = self.safer_step(px, py, sx, sy)
            mx, my = mx + gx * MOVE["fight_cover_bias"], my + gy * MOVE["fight_cover_bias"]
        mx, my = unit(mx, my)
        # don't strafe into walls or out of the zone
        if self.wm.is_solid(px + mx * 4, py + my * 4) or not self.in_zone_after(obs, px + mx * 8, py + my * 8):
            self.strafe = -self.strafe
            self.strafe_until = t + self.rng.uniform(MOVE["strafe_flip_min"], MOVE["strafe_flip_max"])
            mx, my = self.avoid_walls(px, py, -mx, -my)
        jump = (me.get("grounded") and me.get("energy", 0) > MOVE["jump_energy_min"]
                and self.rng.random() < MOVE["jump_chance"])
        return mx, my, jump

    def aim_and_shoot(self, obs, sit, mx, my):
        me = obs["self"]
        t = obs["t"]
        cur_yaw = me["aim"][0]
        fire = [False, False]
        reload = False
        aim = [cur_yaw + 1.2, 0.0]  # default: scan (spin at full turn rate)
        if self.aim_state == "Checking" and sit.threat_bearing is not None:
            # swing to the shot's bearing rather than sweeping past it, but keep sweeping
            # slowly once we are pointed there so we still pick things up
            off = angdiff(sit.threat_bearing, cur_yaw)
            aim = [cur_yaw + (off if abs(off) > 0.25 else 0.35), 0.0]
        weapons = me.get("weapons") or [None, None]

        target = None
        mode = None
        if self.shoot_state == "Finishing" and sit.finish is not None:
            target, mode = sit.finish, "finish"
        elif self.shoot_state == "ThirdPartying" and sit.third is not None:
            target, mode = sit.third, "third"
        elif self.shoot_state == "Fighting":
            # the judged target if we can hit it, otherwise whatever we're locked onto
            if sit.target is not None and sit.target["los"] and sit.target["d"] <= SHOOT["lock_range"]:
                target = sit.target
            else:
                target = sit.lock or sit.target
            mode = "fight"
        if target is not None:
            e = target["tr"]
            s = self.cb.shot(me, e.pos, e.vel, e.cubes, "mech")
            cv = self.cover
            if cv is not None and cv["phase"] == "hide" and cv["tid"] == e.id and self.move_state == "Fighting":
                # behind cover: pre-aim at the target, reload, charge the laser for the next peek
                aim = [s.yaw, s.pitch]
                for i, w in enumerate(weapons[:2]):
                    if not w:
                        continue
                    if w["kind"] == "gun":
                        mag = w.get("stats", {}).get("mag", 12)
                        reload = w.get("reload", 0) <= 0 and w.get("ammo", mag) < mag
                    elif w["kind"] == "laser":
                        fire[i] = w.get("cooldown", 0) <= 0 and t >= self.cb.short_until
                self.last_target_t = t
                return aim, fire, reload
            if s.los:
                aim, fire, reload = self.cb.triggers(me, s, mode, t, obs.get("events", []))
                self.last_target_t = t
                return aim, fire, reload
            aim = [s.yaw, s.pitch]
            # keep a laser charge going a little while the target is hidden
            _, fire, reload = self.cb.triggers(me, s, mode, t, obs.get("events", []))
            return aim, fire, reload
        if self.shoot_state == "Looting" and self.loot_target is not None:
            kind, iid, pos, dist = self.loot_target
            if kind == "crate" and iid in self.tr.crates:
                s = self.cb.shot(me, (pos[0], pos[1], pos[2] - 1.2), (0, 0, 0), 0, "npc", radius=1.4, half_h=1.2)
                if dist < 70:
                    aim, fire, reload = self.cb.triggers(me, s, "crate", t)
                    return aim, fire, reload
            if kind == "npc" and iid in self.tr.tracks:
                n = self.tr.tracks[iid]
                s = self.cb.shot(me, n.pos, n.vel, 0, "npc")
                if dist < 70:
                    aim, fire, reload = self.cb.triggers(me, s, "npc", t)
                    return aim, fire, reload

        if self.aim_state == "LockingOn":
            pt = None
            if sit.target is not None:
                pt = sit.target["tr"].pos
            elif self.move_state == "Flanking" and sit.flank is not None:
                pt = sit.flank["tr"].pos
            elif self.tr.recent_attackers(5.0):
                a = self.tr.recent_attackers(5.0)[-1]
                if a[2] is not None:
                    aim = [a[2], 0.0]
            if pt is not None:
                o = self.cb.muzzle(me)
                yaw, pitch = self.cb.yaw_pitch(o, self.cb.target_center(pt, 0))
                aim = [yaw, pitch]
        elif self.aim_state == "SearchingItems":
            heading = math.atan2(my, mx) if (mx or my) else cur_yaw
            aim = [heading + 1.75 * math.sin(t * 2.2), 0.0]
        else:
            aim = [cur_yaw + 1.2, 0.0]

        # idle reload
        for i, w in enumerate(weapons[:2]):
            if w and w.get("kind") == "gun":
                mag = w.get("stats", {}).get("mag", 12)
                if (w.get("reload", 0) <= 0 and w.get("ammo", mag) < mag * SHOOT["reload_frac"]
                        and t - self.last_target_t > SHOOT["idle_reload_s"]):
                    reload = True
            if w and w.get("kind") == "laser" and (w.get("charge") or 0) > 0:
                # don't waste a held charge: keep holding briefly, then let go
                fire[i] = (t - self.last_target_t) < SHOOT["laser_max_hold_lost"]
        return aim, fire, reload

    def write_mind(self, obs, t):
        """One minimap snapshot: what the bot believes, on its own 4 m grid."""
        cells = set()
        for v in self.dm.views.values():
            cells.update(v["cells"].keys())
        cells.update(self.tr.heat.keys())
        cells.update(self.dm.pull.keys())
        # [cell, final cost, pull fraction]: the viewer paints cost red and pull light blue,
        # so the discount around items is visible even where the cost it cancels was large.
        danger = []
        for K in cells:
            c, p = self.dm.signed(K), self.dm.pull.get(K, 0.0)
            if abs(c) > 0.05 or p > 0.02:
                danger.append([K, round(c, 1), round(p, 2)])
        heat = [[K, round(self.tr.heat_at_cell(K), 2)] for K in self.tr.heat if self.tr.heat_at_cell(K) > 0.05]
        tracks = [[round(e.pos[0], 1), round(e.pos[1], 1), round(t - e.seen_t, 1), e.id]
                  for e in self.tr.tracks.values() if e.kind == "mech" and t - e.seen_t < 15]
        items = [["cube", round(c["pos"][0], 1), round(c["pos"][1], 1)] for c in self.tr.cubes.values()] +                 [[p["kind"], round(p["pos"][0], 1), round(p["pos"][1], 1)] for p in self.tr.pickups.values()] +                 [["crate", round(c["pos"][0], 1), round(c["pos"][1], 1)] for c in self.tr.crates.values()]
        plan = None
        if self.plan is not None:
            plan = [self.plan.kind, round(self.plan.x, 1), round(self.plan.y, 1), round(self.plan.score, 1)]
        path = [[round(x, 1), round(y, 1)] for x, y in self.path[:12]]
        try:
            self.dump.write(json.dumps({"type": "mind", "t": round(t, 2), "danger": danger, "heat": heat,
                                        "tracks": tracks, "items": items, "plan": plan, "path": path,
                                        "state": self.move_state}, separators=(",", ":")) + "\n")
            self.dump.flush()
        except (OSError, ValueError):
            self.dump = None

    def on_end(self, msg):
        tot = sum(self.state_time.values()) or 1
        dist = " ".join(f"{k}:{100 * v / tot:.0f}%" for k, v in sorted(self.state_time.items(), key=lambda kv: -kv[1]))
        mf.log(f"[end] place={msg.get('place')} kills={msg.get('kills')} ticks={self.n_ticks} max_ms={self.max_ms:.1f} states {dist}")


if __name__ == "__main__":
    mf.run(Bot)
