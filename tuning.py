"""Every number the judgement system uses, in one place.

Change values here, not in the logic. Units: meters, seconds, radians unless noted.
Probabilities are 0..1. "Score" weights are relative to each other.
"""
import math

# ---------------------------------------------------------------- movement
MOVE = {
    # Silent movement: the server makes a step sound above sound.step_speed (5 m/s).
    # We creep just under it whenever a threat may be within STEALTH_RADIUS.
    "creep_margin": 0.4,            # m/s below step_speed
    "flank_circle_p": 0.65,         # chance a strafe flip picks the side that gets behind them
    "fight_cover_bias": 0.55,       # how hard the strafe bends toward cooler ground
    "fight_probe_m": 14.0,          # how far ahead that choice looks
    # A step sound carries 40 m. At 75 we crawled whenever a remembered enemy was within
    # nearly twice the distance anyone could hear us, which cost 61% of our speed for
    # nothing. 45 is the carry range plus a margin for their movement and our error.
    "stealth_radius": 45.0,
    "roam_boost_energy_min": 45.0,  # boost while roaming only with energy to spare...
    "roam_boost_max_danger": 0.5,   # ...and only across ground that is genuinely cool
    "panic_hp": 0.30,               # below this and taking fire: break off, whatever the odds
    # Early round: head for the edge of the next zone circle.
    "early_phase_until": 45.0,      # s; EarlySetup ends at this time or on reaching the camp spot
    "edge_margin_frac": 0.14,       # camp this far inside the next circle (fraction of its radius)
    "edge_margin_min": 8.0,
    "edge_margin_max": 40.0,
    "camp_arc_deg": 70.0,           # consider spots this far either side of our nearest edge point
    "camp_reeval_s": 4.0,
    "camp_switch_gain": 0.25,       # new spot must score this much better to switch
    "arrive_radius": 6.0,
    # Camp-spot score weights
    "w_travel": 1.0,                # per 100 m of travel
    "w_cover": 1.6,                 # 0..1 cover fraction
    "w_canopy": 0.4,                # under tree crowns (concealment)
    "w_roof": 1.2,                  # high ground bonus (roof spot)
    "w_heat": 2.0,                  # enemy heat near the spot
    "roof_min_h": 5.0,              # roof must be this much above ground to count as high ground
    "roof_max_h": 38.0,             # and low enough to climb with the jets
    "roof_energy_min": 70.0,        # only start a climb with this much energy
    "use_roofs": False,             # roof climbing got stuck at the map edge in the sim; off until fixed
    "map_margin": 40.0,             # keep camp spots this far inside the map edge
    # Zone pressure: allow running/boosting when the zone would catch us.
    "zone_walk_ratio": 0.7,         # needed speed / creep speed above this -> walk at full speed
    "zone_boost_ratio": 0.8,        # needed speed / walk speed above this -> boost
    # Strafing while fighting
    "strafe_flip_min": 0.4,
    "strafe_flip_max": 1.2,
    "strafe_back_bias": 0.25,       # drift away from the target while strafing (never push forward)
    "jump_chance": 0.10,            # per decision while fighting and grounded
    "jump_energy_min": 35.0,
    "fight_boost_energy_min": 30.0,
    # Flanking: get into the target's rear arc
    "flank_dist": 55.0,
    "flank_max_dist": 220.0,
    # Retreat: break line of sight, then regenerate
    "retreat_search_r": [18.0, 30.0, 45.0],
    "retreat_done_hp": 0.75,
    "retreat_quiet_s": 6.0,
}

# ---------------------------------------------------------------- judgement
JUDGE = {
    # Target selection: expected DPS x priority / torso-slew cost
    "killable_s": 3.0,              # finishable in this long -> the kill is the whole point
    "pri_killable": 4.0,
    "pri_charging": 1.5,            # our hit cancels a laser charge
    "pri_attacking": 1.5,
    "pri_rear": 1.5,                # behind it: our damage is x1.5
    "pri_nolos": 0.3,
    "pri_backturn": 0.55,           # shooting it turns our back on something worse
    "backturn_rad": 1.48,           # 85 degrees
    "focus_margin": 2.2,            # a challenger must beat the locked target by this much
    "slew_w": 1.5,

    # Fight advantage A = (time they need to kill us) / (time we need to kill them).
    # An engaged enemy is fought unless one of the retreat rules below fires.
    "flee_adv": 0.6,                # below this, retreat (exp 2 tried 0.4: 2.38 vs 2.58)
    "low_hp_flee_adv": 1.3,         # at low HP, retreat below this advantage (exp 2 tried 0.9)
    "flee_hysteresis": 0.4,         # leave a retreat to fight only above flee_adv + this
    "engage_memory_s": 3.0,         # an enemy stays "engaged" this long after the last sign
    "flank_min_adv": 1.2,           # only go hunting/flanking when clearly stronger
    "finish_hp_abs": 260.0,         # enemy HP at or below this is a finishing target...
    "finish_hp_frac": 0.25,         # ...or at or below this fraction of its max
    "finish_max_dist": 140.0,
    "low_hp_frac": 0.33,            # our HP fraction that counts as low
    "multi_angle_deg": 50.0,        # attackers this far apart in bearing = shot from multiple angles
    "attack_memory_s": 3.0,         # an enemy that damaged us this recently is "attacking"
    "engage_range": 200.0,          # enemies beyond this are ignored for fight decisions
    "approach_speed": 1.0,          # closing speed (m/s) that counts as approaching
    "facing_deg": 35.0,             # enemy aim within this of us = aiming at us
    "exposure_penalty": 0.25,       # advantage multiplier lost per extra enemy with sight of us
    # Assumed accuracy of enemy bots by weapon (we can't see their skill)
    "enemy_acc": {"gun": 0.45, "laser": 0.65, "lightning": 0.95, "mortar": 0.35},
    "rear_mult": 1.5,
    "third_party_hp_frac": 0.45,    # third-party an enemy fighting someone else below this HP
}

# ---------------------------------------------------------------- shooting
SHOOT = {
    # Anti-aggro: the squad shares vision, so opening fire on a distant, healthy bot that is
    # not already fighting us simply recruits it. Shoot attackers, the wounded, and anything
    # close; leave idle bystanders alone.
    "poke_max_m": 95.0,
    "poke_max_laser_m": 150.0,
    "poke_wounded_frac": 0.85,

    # Fire the gun on expected value rather than on crossing the target.
    "p_fire": 0.22,                 # normal threshold on modelled hit chance
    "p_fire_near": 0.12,            # inside p_fire_near_m, or finishing: take the cheap shot
    "p_fire_near_m": 60.0,
    "p_fire_low": 0.30,             # ...but be choosy when a reload is close
    "p_fire_low_ammo": 3,

    # No hit-chance threshold (exp 3): fire whenever the shot is lined up, i.e. in range, line of
    # sight, and the torso (after this tick's slew) points inside the target's silhouette.
    "lineup_frac": 1.0,             # fraction of the target's angular half-width that counts as lined up
    "lock_range": 230.0,            # auto-lock onto any visible enemy mech within this range
    "pred_noise": 0.5,              # fraction of possible target dodge we can't predict (judgement only)
    "target_accel": 58.5,           # ground accel of a mech (config.mech.ground_accel)
    "laser_release_charge": 0.97,   # release at this fraction of full charge
    "laser_max_hold_lost": 1.6,     # s to keep a charge after losing the target (covers the step out of cover)
    "lightning_energy_reserve": 25.0,
    "mortar_min_dist": 22.0,        # self-splash is 50%; stay clear
    "mortar_max_dist": 225.0,
    "reload_frac": 0.4,             # reload early below this ammo fraction when idle
    "idle_reload_s": 1.5,
}

# ---------------------------------------------------------------- loot
LOOT = {
    "retreat_grab_r": 40.0,         # while retreating, still grab cubes this close...
    "retreat_grab_enemy_min": 90.0, # ...if no enemy is closer than this
    "no_weapon_detour": 320.0,      # with an empty secondary slot, go this far out of the way for a weapon
    "free_radius": 60.0,            # no known enemy this close to an item = "free"
    "heat_max": 0.6,                # and enemy heat around it below this
    "max_detour": {"cube": 70.0, "weapon": 90.0, "crate": 45.0, "npc": 60.0, "drop": 120.0},
    "after_kill_detour": 110.0,
    "early_detour_mult": 1.5,       # EarlySetup is more generous
    "engage_timeout_s": 2.5,        # standing on a crate this long with no damage = give up
    "give_up_s": 20.0,              # ...and do not offer it again for this long
    "memory_s": 120.0,              # how long a seen item stays on our map unseen
    "forget_near_m": 12.0,          # ...unless we are this close with a clear view and it is gone
    "weapon_rank": {"laser": 3, "lightning": 2, "mortar": 1, "gun": 0},
    # How much of the "fill the empty slot" bonus each weapon deserves. The gun alone is
    # 107 sustained DPS; a laser roughly doubles that and lightning more than triples it
    # inside 45 m, while a mortar is 10-40 DPS against anything that moves.
    "weapon_value": {"laser": 1.0, "lightning": 0.95, "mortar": 0.4, "gun": 0.0},
    "npc_min_hp_frac": 0.7,         # only farm drones when healthy
    "drop_min_s_left": 0.0,
    "drop_free_radius": 100.0,
    "crate_engage_dist": 25.0,
}

# Upgrade preference (higher = better). Unknown ids get 1.
UPGRADE_PRIORITY = {
    "plating": 10, "armor": 9.5, "nanites": 8, "gyros": 7.5, "laser_charge": 7,
    "laser_damage": 6.8, "gun_damage": 6.5, "gun_rate": 6.3, "sensors": 6,
    "gun_velocity": 5.5, "gun_mag": 5, "lightning_efficiency": 4.5, "capacitor": 4,
    "reactor": 3.8, "jets": 3.5, "servos": 3.2, "mortar_reload": 3, "lightning_range": 3,
    "lightning_chain": 2.5, "mortar_velocity": 2, "mortar_splash": 2, "laser_pierce": 1.5,
}

# Heat (where we think unseen enemies are) from sounds.
HEAT = {
    "tau": 6.0,                     # s for heat to decay by e
    "prune": 0.02,
    "sound_weight": {
        "gun": 1.0, "laser": 1.2, "laser_charge": 1.6, "lightning": 1.0, "mortar": 1.0,
        "explosion": 0.25, "step": 1.6, "boost": 1.2, "jet": 1.0, "reload": 0.8,
        "death": 0.2, "crate": 0.6, "npc_gun": 0.1, "drop": 0.0,
    },
    "blob_r": 12.0,                 # a sound deposits a fading blob of this radius...
    "blob_gain": 0.35,              # ...with this much of the old single-cell weight at the centre
    "ghost_weight": 2.0,            # heat left where a tracked mech was last known
    "hot": 0.35,                    # a cell this hot counts as a likely enemy position
}

TWO_PI = 2 * math.pi

# Utility planner (exp 7): score = reward - danger - travel. Rewards are in "points"; one point
# of danger is one point of the danger map (a cell an enemy can see close-up costs ~4-7).
UTIL = {
    "r_cube": 6.0,                  # per cube (clusters add up)
    "cube_heal_gain": 2.0,          # x3 at 0 HP: a cube heals, so it competes with cover
    "r_weapon_empty": 12.0,         # any weapon while slot 2 is empty
    "r_weapon_upgrade": 3.0,        # per rank of improvement (laser 3 > lightning 2 > mortar 1)
    "r_weapon_rank": 4.0,           # per rank of the weapon itself: a laser is worth the walk
    "contest_span": 80.0,           # metres of head start that makes an item ours
    "contest_floor": 0.25,          # an item someone is standing on is still worth something
    # A crate is 300 HP for 1 cube plus a weapon half the time (mortar/laser/lightning equally).
    # In this file's own currency that is r_cube 6.0 + 0.5 * ~8 for the average weapon = ~10,
    # discounted for the time spent shooting it and the 220 m noise of it breaking.
    "crate_quiet": 1.75,             # x this when no enemy is known (swept)
    "r_crate": 7.0,
    "r_npc": 5.0,                   # 2 cubes, shoots back a little
    "r_npc_biting": 14.0,           # ...and this much more if it is shooting us right now
    "r_drop": 8.0,                  # 3 cubes + full heal...
    "r_drop_heal": 8.0,             # ...worth more when hurt
    "r_camp": 1.5,                  # the zone-edge spot: where we go when nothing else is worth it
    "r_explore": 7.0,               # looking at ground we have not seen lately, times how stale
    "explore_tau": 35.0,            # seconds for a cell to go from fresh to fully stale
    "explore_cells": 4,             # stalest cells offered as options each replan
    "hold_decay_s": 6.0,            # a held spot (camp/cover) is worth e-folding less per this
    "hold_floor": 0.15,             # ...but never quite nothing, so cover still beats open ground
    "cover_hp": 0.5,                # cover is worth nothing above this HP fraction...
    "r_cover_hurt": 16.0,           # ...and up to this at 0 HP
    "r_cover_losing": 4.0,          # extra while losing a fight, only below cover_hp + 0.15
    "r_fight": 5.0,                 # staying in the fight, times advantage (capped)
    "fight_adv_cap": 3.0,           # past this the fight is already won; more does not pay more
    "fight_kill_gain": 2.0,         # x3 against an enemy on its last legs: finish it
    "r_flank": 4.0,                 # times our fight advantage (max 2)
    "flank_dist": 55.0,
    "flank_min_hp": 0.5,
    "farm_min_hp": 0.5,             # crates/drones only when healthy
    "w_danger": 1.0,
    "hp_danger_gain": 3.0,          # danger weight x(1 + 3*(1-hp)): x4 at 0 HP
    "losing_danger_mult": 1.5,
    "path_danger_frac": 0.15,       # weight of danger summed along the straight route
    "path_inflate": 1.2,            # a route is longer than the straight line
    "eta_scale": 6.0,               # reward is halved at this many seconds of travel
    "w_zone": 3.0,                  # per 50 m outside the next circle
    "stick_bonus": 1.0,             # hysteresis for the current destination
    "commit_gain": 3.0,             # ...times this much more once we've closed the whole gap
    "replan_s": 0.5,                # re-score options this often
}

# Danger map (exp 6): path cost per 4 m cell. Base step cost is 1 per cell.
# Opening aerial survey. Crates, cubes and pickups spawn randomly each match and are listed
# only while in line of sight, so a climb at t=0 is the cheapest map-wide census we can get --
# and the tracker keeps what it sees. Vision is a 120 deg *yaw* wedge with no pitch limit, so
# one spin (2*pi / turn_rate 4 = 1.6 s) sweeps the whole 200 m circle. Cost: jets are loudness
# 90, the loudest tier, and we are a silhouette with no cover the whole time.
SCAN = {
    "enabled": True,
    "alt": 30.0,                    # 30 m: measured to see as much as 60 m does, for half...
    "energy_floor": 35.0,           # ...the airtime and with energy left for the first fight
    "max_s": 12.0,                  # hard stop, whatever height we reached
    "abort_hp": 0.97,               # only real damage aborts the climb; chip fire does not,
                                    # or we never get the altitude the scan exists for
    "min_air_s": 0.5,               # ignore "grounded" for this long after the jump
    # The descent is free steering: we are already in the air, so pick where to come down.
    "glide_rays": 16,               # bearings considered for the landing spot
    "glide_dists": (0.0, 25.0, 50.0, 80.0),
    "glide_far_cost": 0.8,          # per 100 m, so we only travel for a real improvement
    "glide_repick_s": 0.4,
}

# Stepping out from under a mortar. Shells are the one big hit the game announces: they are
# listed in `projectiles` with a velocity, and only gravity acts on them.
DODGE = {
    "gravity": 22.0,
    "splash_r": 7.0,                # splash falls to 30% at this radius
    "margin": 4.0,                  # clear the edge, do not graze it
    "horizon_s": 3.0,               # shells further out than this may still be aimed elsewhere
    "min_react_s": 0.2,             # below this we cannot move far enough to matter
}

DANGER = {
    "view_range": 200.0,            # enemies see this far (vision range)
    "rays": 120,                    # viewshed rays per enemy (3 degrees apart)
    "cone_deg": 120.0,              # enemy view cone; cells inside it cost more
    "seen_cost": 4.0,               # extra cost of a cell an enemy can see (close end)
    "cone_mult": 1.8,
    # Sound is the only thing we know about the enemies we cannot see, which is most of them
    # most of the time. At 1.5 the heat from a gun fired inside 60 m peaked at under half of
    # one visible enemy's sight line, so hearing a fight nearby barely moved us. 2.5 x cap 3
    # puts a loud, close, recent sound on a par with standing in somebody's cone.
    "heat_cost": 2.5,               # per unit of sound heat
    "pull_max": 0.85,               # danger counts for only 15% standing on a wanted item...
    "pull_radius": 24.0,            # ...fading to full price this far out (squared falloff)
    "pull_items_max": 24,           # nearest N wanted items build the pull wells (cost guard)
    "pull_bonus": 3.0,              # ...and pay this on top, so a safe item is actively inviting
    "threat_floor": 0.2,            # a nearly dead mech's sight lines still cost this fraction
    "exposure_rate": 0.5,           # per second per enemy watching us, added where we stand
    "exposure_cap": 4.0,            # ...up to this; the squad converges, it does not teleport
    "exposure_fade_s": 12.0,        # how fast a spot cools once nobody can see us there
    "exposure_radius": 20.0,        # they converge on roughly where we were, not the exact cell
    "exposure_cost": 1.5,           # danger per unit of accumulated exposure
    "forget_s": 10.0,               # drop an enemy's viewshed this long after last sighting
    "recast_move": 10.0,            # recompute a viewshed after the enemy moves this far
    "recast_s": 3.0,
    # Budgets are in work, not wall-clock: a time budget makes the same match play out
    # differently depending on how busy the machine was, which destroys reproducibility.
    "path_budget_nodes": 1800,      # A* nodes expanded before we accept a partial path
    "recon_cells_per_tick": 12,     # knowledge-grid cells refreshed per decision
    "max_recasts_per_tick": 1,      # ~6 ms each; keeps a big group inside the time budget
    "line_samples": 20,           # cap probes per route; long options were costing a tick
    "line_cost_max": 2.0,           # straight walk allowed if its total danger is below this
    "loot_max": 3.0,                # an item is "free" if its cell's danger is below this
}

# Diagnostic probes. Set "hide" back to False for the real bot.
# hide: creep silently to a covered ground spot near the next zone edge, never shoot, never loot,
#       move only to stay inside the zone. Its practice score shows whether house bots die
#       without us (e.g. to the zone): ~0 means practice points come only from our kills.
PROBE = {
    "hide": False,                  # P1 scored 0.04: house bots never die without our kills
}

# Cover fighting (exp 4): peek out, shoot, duck back behind a wall, reload/charge/heal, repeat.
COVER = {
    "hide_radii": [5.0, 9.0, 14.0, 20.0, 28.0],
    "peek_radii": [3.0, 5.0, 7.5, 10.0],
    "angles": 12,
    "replan_s": 1.5,
    "target_move_replan": 15.0,     # re-plan if the target moved this far since planning
    "peek_max_s": 3.0,              # longest single peek
    "peek_dmg": 220.0,              # duck back after taking this much damage in one peek
    "hide_min_s": 0.8,
    "hide_max_s": 8.0,
    "heal_to": 0.55,                # while hidden, wait for regen up to this HP fraction (if below)
    "reload_frac": 0.7,             # peek again once the gun has this fraction of its magazine
    "min_target_dist": 18.0,        # closer than this, just fight (cover won't help)
    "adv_bonus": 1.6,               # fight advantage multiplier when a cover plan exists
}
