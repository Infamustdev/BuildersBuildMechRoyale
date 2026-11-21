"""Utility planner: pick where to go next by one score instead of hard rules.

    score = reward - danger - travel

reward  : what's at the destination (cubes, a better weapon, crates, drones, supply drops,
          cover when we're hurt or losing, a flank spot behind an enemy, the zone-edge camp).
danger  : the danger map (enemy sight lines + sound heat) at the destination and along the way,
          weighted more the lower our HP and the worse the current fight.
travel  : seconds to get there.
The current destination gets a small bonus (hysteresis) so the bot doesn't flip every tick.
All weights live in tuning.UTIL.
"""
import math

from tuning import LOOT, UTIL


class Option:
    __slots__ = ("kind", "key", "x", "y", "z", "reward", "score", "item")

    def __init__(self, kind, key, x, y, z, reward, item=None):
        self.kind, self.key, self.x, self.y, self.z = kind, key, x, y, z
        self.reward, self.item, self.score = reward, item, -1e9


def plan(bot, obs, sit, camp):
    """Return the best Option for this tick."""
    me = obs["self"]
    px, py, pz = me["pos"]
    walk = me.get("stats", {}).get("walk_speed", 11.7)
    tr, dm, wm = bot.tr, bot.dm, bot.wm
    hp = sit.hp_frac
    losing = sit.adv < 1.0 and bool(sit.engaged or sit.unseen_dps > 0)
    weapons = me.get("weapons") or [None, None]
    slot1 = weapons[1]["kind"] if len(weapons) > 1 and weapons[1] else None
    have = {w["kind"] for w in weapons if w}
    rank = LOOT["weapon_rank"]
    opts = []

    def reachable(x, y, z):
        # items on a roof we're not on are out of reach
        return not (z - wm.ground_at(x, y) > 4 and abs(z - pz) > 4)

    # ---- items
    for cid, c in tr.cubes.items():
        x, y, z = c["pos"]
        if reachable(x, y, z):
            # nearby cubes add up: one trip collects the cluster
            n = sum(1 for o in tr.cubes.values() if (o["pos"][0] - x) ** 2 + (o["pos"][1] - y) ** 2 < 64)
            opts.append(Option("cube", ("cube", cid), x, y, z, UTIL["r_cube"] * n, ("cube", cid)))
    for pid, p in tr.pickups.items():
        k = p["kind"]
        if k in have:
            continue
        gain = rank.get(k, 0) - (rank.get(slot1, -1) if slot1 else -1)
        if gain <= 0:
            continue
        x, y, z = p["pos"]
        if reachable(x, y, z):
            r = UTIL["r_weapon_empty"] if slot1 is None else UTIL["r_weapon_upgrade"] * gain
            opts.append(Option("weapon", ("weapon", pid), x, y, z, r, ("weapon", pid)))
    if hp >= UTIL["farm_min_hp"]:
        for cid, c in tr.crates.items():
            x, y, z = c["pos"]
            if reachable(x, y, z):
                opts.append(Option("crate", ("crate", cid), x, y, z, UTIL["r_crate"], ("crate", cid)))
        for n in tr.npcs():
            x, y, z = n.pos
            opts.append(Option("npc", ("npc", n.id), x, y, z, UTIL["r_npc"], ("npc", n.id)))
    for d in tr.drops:
        x, y, z = d["pos"]
        if d.get("holder") not in (None, me["id"]) or not bot.in_zone_after(obs, x, y, 10):
            continue
        opts.append(Option("drop", ("drop", d["id"]), x, y, z,
                           UTIL["r_drop"] + UTIL["r_drop_heal"] * (1.0 - hp), ("drop", d["id"])))

    # ---- positions
    opts.append(Option("camp", ("camp",), camp[0], camp[1], 0.0, UTIL["r_camp"]))
    if sit.threats or sit.unseen_dps > 0:
        cp = bot.retreat_point(obs, sit)
        if cp is not None:
            # cover only pays once we're actually hurt: 0 above cover_hp, full at 0 HP
            hurt = max(0.0, (UTIL["cover_hp"] - hp) / UTIL["cover_hp"])
            r = UTIL["r_cover_hurt"] * hurt + (UTIL["r_cover_losing"] if losing and hp < UTIL["cover_hp"] + 0.15 else 0.0)
            opts.append(Option("cover", ("cover",), cp[0], cp[1], 0.0, r))
    if sit.flank is not None and hp > UTIL["flank_min_hp"]:
        e = sit.flank["tr"]
        back = e.aim[0] + math.pi
        fx = e.pos[0] + math.cos(back) * UTIL["flank_dist"]
        fy = e.pos[1] + math.sin(back) * UTIL["flank_dist"]
        K = wm.nearest_open(fx, fy, 4)
        if K >= 0:
            fx, fy = wm.ncenter(K)
            opts.append(Option("flank", ("flank", e.id), fx, fy, 0.0,
                               UTIL["r_flank"] * min(2.0, sit.flank.get("adv", 1.0))))

    # ---- score
    dw = UTIL["w_danger"] * (1.0 + UTIL["hp_danger_gain"] * (1.0 - hp)) * (UTIL["losing_danger_mult"] if losing else 1.0)
    best = None
    for o in opts:
        d = math.hypot(o.x - px, o.y - py)
        if not bot.in_zone_after(obs, o.x, o.y, 2.0) and o.kind != "camp":
            continue
        z = obs["zone"]
        dn = math.hypot(o.x - z["next_center"][0], o.y - z["next_center"][1]) - z["next_radius"]
        zone_pen = UTIL["w_zone"] * max(0.0, dn) / 50.0
        danger = dm.at(o.x, o.y) + UTIL["path_danger_frac"] * dm.line_cost(px, py, o.x, o.y)
        o.score = o.reward - dw * danger - UTIL["w_travel"] * d / walk - zone_pen
        if bot.plan_key is not None and o.key == bot.plan_key:
            o.score += UTIL["stick_bonus"]
        if best is None or o.score > best.score:
            best = o
    return best, opts
