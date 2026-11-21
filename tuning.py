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
    "stealth_radius": 75.0,         # step sounds carry 40 m; keep a safety margin
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
    "free_radius": 60.0,            # no known enemy this close to an item = "free"
    "heat_max": 0.6,                # and enemy heat around it below this
    "max_detour": {"cube": 70.0, "weapon": 90.0, "crate": 45.0, "npc": 60.0, "drop": 120.0},
    "after_kill_detour": 110.0,
    "early_detour_mult": 1.5,       # EarlySetup is more generous
    "weapon_rank": {"laser": 3, "lightning": 2, "mortar": 1, "gun": 0},
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
    "ghost_weight": 2.0,            # heat left where a tracked mech was last known
    "hot": 0.35,                    # a cell this hot counts as a likely enemy position
}

TWO_PI = 2 * math.pi

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
