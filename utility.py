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


# Destinations that are not consumed by arriving: standing on them does not finish them.
HOLD_KINDS = ("camp", "cover", "flank")


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
    # Travel is priced at full walk with a path-inflation factor, not at the speed we happen
    # to be creeping at: costing it at creep roughly doubled the penalty and deleted distant
    # loot from the option list entirely.
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

    enemies = [e for e in tr.tracks.values() if e.kind == "mech"]

    def uncontested(x, y):
        """How much of this item we can expect to actually get.

        A cube with someone standing over it is already theirs; the same cube with the nearest
        enemy twice as far away as us is as good as in hand. One smooth term, so it shades the
        choice rather than ruling anything in or out.
        """
        if not enemies:
            return 1.0
        du = math.hypot(x - px, y - py)
        de = min(math.hypot(x - e.pos[0], y - e.pos[1]) for e in enemies)
        lead = (de - du) / UTIL["contest_span"]
        return UTIL["contest_floor"] + (1.0 - UTIL["contest_floor"]) * max(0.0, min(1.0, 0.5 + lead))

    # ---- items
    for cid, c in tr.cubes.items():
        x, y, z = c["pos"]
        if reachable(x, y, z):
            # nearby cubes add up: one trip collects the cluster
            n = sum(1 for o in tr.cubes.values() if (o["pos"][0] - x) ** 2 + (o["pos"][1] - y) ** 2 < 64)
            # a cube is +4% max HP and heals the difference, so it is itself a heal: when we
            # are hurt, walking to the cubes we just knocked loose beats walking away from them
            r = UTIL["r_cube"] * n * (1.0 + UTIL["cube_heal_gain"] * (1.0 - hp)) * uncontested(x, y)
            opts.append(Option("cube", ("cube", cid), x, y, z, r, ("cube", cid)))
    for pid, p in tr.pickups.items():
        k = p["kind"]
        if k in have:
            continue
        gain = rank.get(k, 0) - (rank.get(slot1, -1) if slot1 else -1)
        if gain <= 0:
            continue
        x, y, z = p["pos"]
        if reachable(x, y, z):
            # the weapon we end up holding decides every fight for the rest of the match, so
            # rank counts for itself and not only for the size of the upgrade: a laser is the
            # prize, and worth crossing the map for while nobody is contesting it
            # Scale the empty-slot bonus by how good the weapon is, do not add it flat. A flat
            # bonus made a mortar two thirds as attractive as a laser purely for filling the
            # slot, which is how we walked past a near crate for a far mortar.
            base = UTIL["r_weapon_empty"] if slot1 is None else UTIL["r_weapon_upgrade"] * gain
            r = base * LOOT["weapon_value"].get(k, 0.5) * uncontested(x, y)
            opts.append(Option("weapon", ("weapon", pid), x, y, z, r, ("weapon", pid)))
    if hp >= UTIL["farm_min_hp"]:
        for cid, c in tr.crates.items():
            x, y, z = c["pos"]
            if obs["t"] - bot.give_up.get(cid, -1e9) < LOOT["give_up_s"]:
                continue            # we stood there shooting it and nothing happened
            if reachable(x, y, z):
                # with nobody in sight there is nothing better to do with the time, and a
                # crate is a cube plus half a weapon -- the upgrades are how we win later
                quiet = UTIL["crate_quiet"] if not sit.threats else 1.0
                opts.append(Option("crate", ("crate", cid), x, y, z,
                                   UTIL["r_crate"] * quiet * uncontested(x, y), ("crate", cid)))
        for n in tr.npcs():
            x, y, z = n.pos
            opts.append(Option("npc", ("npc", n.id), x, y, z, UTIL["r_npc"], ("npc", n.id)))
    # A drone that is shooting us is not farming, it is a wound that keeps bleeding, so it is
    # offered whatever our HP: the `farm_min_hp` gate above is about whether a drone is worth
    # a detour, not about whether to deal with one that is already hurting us.
    if sit.npc_dps > 0 and sit.npc_by is not None:
        n = tr.tracks.get(sit.npc_by)
        if n is not None and n.kind == "npc":
            x, y, z = n.pos
            opts.append(Option("npc", ("npc", n.id), x, y, z,
                               UTIL["r_npc"] + UTIL["r_npc_biting"], ("npc", n.id)))
    for d in tr.drops:
        x, y, z = d["pos"]
        if d.get("holder") not in (None, me["id"]) or not bot.in_zone_after(obs, x, y, 10):
            continue
        opts.append(Option("drop", ("drop", d["id"]), x, y, z,
                           UTIL["r_drop"] + UTIL["r_drop_heal"] * (1.0 - hp), ("drop", d["id"])))

    # ---- staying in the fight
    # Fighting is a choice like any other, not a mode: it competes with the cubes on the ground
    # and the laser two streets over. Scored where we stand, so it pays no travel, and the
    # danger of the spot counts against it like anywhere else. A fight we are winning but not
    # finishing scores modestly; a nearly dead enemy outbids almost anything.
    tgt = sit.finish if sit.finish is not None else sit.target
    if tgt is not None:
        e = tgt["tr"]
        mh = getattr(e, "max_hp", 0.0) or 0.0
        ehp = max(0.0, min(1.0, (e.hp / mh) if mh > 0 else 1.0))
        r = (UTIL["r_fight"] * min(UTIL["fight_adv_cap"], max(0.0, sit.adv))
             * (1.0 + UTIL["fight_kill_gain"] * (1.0 - ehp)))
        opts.append(Option("fight", ("fight", e.id), px, py, pz, r))

    # ---- looking around
    # Ground we have not seen lately is where the next cube or weapon has to come from, and it
    # gets worth more the longer we ignore it. Arriving makes a cell fresh, so its reward
    # collapses the moment we get there and the next stalest one takes over: the bot keeps
    # moving because moving is what pays, not because a rule tells it to.
    for (ex, ey, frac) in bot.rc.stale(obs["t"], px, py, UTIL["explore_tau"], UTIL["explore_cells"]):
        K = wm.nearest_open(ex, ey, 6)
        if K < 0:
            continue
        ox, oy = wm.ncenter(K)
        if bot.in_zone_after(obs, ox, oy, 4.0):
            opts.append(Option("look", ("look", K), ox, oy, 0.0, UTIL["r_explore"] * frac))

    # ---- positions
    # A spot we are already standing on pays no travel, so without this its score would peak the
    # instant we arrive and nothing could ever outbid it. Its worth is in getting there and
    # taking stock; that is spent, so it decays while we hold it.
    opts.append(Option("camp", ("camp",), camp[0], camp[1], 0.0, UTIL["r_camp"] * bot.hold_mult(obs)))
    if sit.threats or sit.unseen_dps > 0:
        cp = bot.retreat_point(obs, sit)
        if cp is not None:
            # cover only pays once we're actually hurt: 0 above cover_hp, full at 0 HP
            hurt = max(0.0, (UTIL["cover_hp"] - hp) / UTIL["cover_hp"])
            r = UTIL["r_cover_hurt"] * hurt + (UTIL["r_cover_losing"] if losing and hp < UTIL["cover_hp"] + 0.15 else 0.0)
            opts.append(Option("cover", ("cover",), cp[0], cp[1], 0.0, r * bot.hold_mult(obs)))
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
        # Discount the reward by travel time instead of subtracting a cost from it. Subtraction
        # drives anything far away negative, so it stops being an option at all; dividing keeps
        # a valuable thing worth going to get -- a big cube pile 12 s away still scores a third
        # of its value rather than nothing.
        eta = d * UTIL["path_inflate"] / walk
        o.score = o.reward / (1.0 + eta / UTIL["eta_scale"]) - dw * danger - zone_pen
        if bot.plan_key is not None and o.key == bot.plan_key:
            bonus = UTIL["stick_bonus"]
            if o.kind not in HOLD_KINDS:
                # commitment grows with the ground already covered: a cube 3 m away is nearly
                # paid for, so it takes far more danger to make us drop it than it did at 80 m.
                # Only for things that are consumed on arrival -- a camp or cover spot is never
                # "finished", so a growing bonus there would just pin us in place forever.
                d0 = bot.plan_d0 or d
                prog = max(0.0, min(1.0, 1.0 - d / d0)) if d0 > 1e-6 else 0.0
                bonus *= 1.0 + UTIL["commit_gain"] * prog
            o.score += bonus
        if best is None or o.score > best.score:
            best = o
    return best, opts
