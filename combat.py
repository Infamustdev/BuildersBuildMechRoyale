"""Aiming and shooting: lead calculation, hit probability, per-weapon trigger logic, damage models.

Facts from the rules this relies on (protocol.md section 7):
- Shots leave along the ACTUAL torso aim, which slews at turn_rate / pitch_rate.
- Gun bullets fly straight (no drop) at `speed` for `range`, with a small Gaussian spread.
- Laser is hitscan when released; lightning hits the nearest target in a 30 deg cone within 45 m.
- Mortar shells fall at `gravity`; splash is blocked by walls; self-splash is 50%.
- Buildings and terrain block shots; trees do not.
"""
import math

from tuning import JUDGE, SHOOT
from tracker import angdiff


def erf(x):
    return math.erf(x)


class Shot:
    """Result of aiming at a target: where to point and how likely each weapon hits."""
    __slots__ = ("yaw", "pitch", "d", "flight", "p_gun", "p_laser", "p_light", "p_mortar",
                 "mortar_aim", "los", "point")

    def __init__(self):
        self.yaw = self.pitch = 0.0
        self.d = 1e9
        self.flight = 0.0
        self.p_gun = self.p_laser = self.p_light = self.p_mortar = 0.0
        self.mortar_aim = None
        self.los = False
        self.point = None


class Combat:
    def __init__(self, cfg, wm):
        self.cfg = cfg
        self.wm = wm
        self.W = cfg.get("weapons", {})
        self.mech = cfg.get("mech", {})
        self.dpc = cfg.get("cubes", {}).get("damage_per_cube", 0.04)
        self.cap = cfg.get("cubes", {}).get("stat_cap", 10)
        self.size_per_cube = cfg.get("cubes", {}).get("size_per_cube", 0.05)
        self.laser_full_since = None
        self.laser_lost_since = None
        self.short_until = -1.0

    # ------------------------------------------------------------ geometry
    def muzzle(self, me, kind="gun"):
        st = me.get("stats", {})
        p = me["pos"]
        v = me.get("vel") or [0, 0, 0]
        h = st.get("muzzle_height", 7.0)
        if kind == "mortar":
            h = h + 2.5 * (h / 7.0)
        # where we'll be when the shot leaves (half a decision ahead)
        return (p[0] + v[0] * 0.05, p[1] + v[1] * 0.05, p[2] + h)

    def target_center(self, tpos, cubes, kind="mech"):
        if kind == "npc":
            return (tpos[0], tpos[1], tpos[2] + 1.75)
        s = 1.0 + self.size_per_cube * (cubes or 0)
        return (tpos[0], tpos[1], tpos[2] + 4.5 * s)

    def lead(self, origin, tpos, tvel, speed, cubes=0, kind="mech", extra_t=0.0):
        """Predict where a projectile of `speed` meets the target. Returns (point, flight_time)."""
        c = self.target_center(tpos, cubes, kind)
        t = 0.0
        g = self.mech.get("gravity", 22.0)
        vz = tvel[2] if len(tvel) > 2 else 0.0
        p = c
        for _ in range(4):
            tt = t + extra_t
            z = c[2] + (vz * tt - 0.5 * g * tt * tt if abs(vz) > 0.5 else 0.0)
            z = max(z, self.wm.surface(c[0], c[1]) + 1.0) if abs(vz) > 0.5 else z
            p = (c[0] + tvel[0] * tt, c[1] + tvel[1] * tt, z)
            d = math.dist(origin, p)
            t = d / speed if speed > 0 else 0.0
        return p, t

    @staticmethod
    def yaw_pitch(o, p):
        dx, dy, dz = p[0] - o[0], p[1] - o[1], p[2] - o[2]
        return math.atan2(dy, dx), math.atan2(dz, math.hypot(dx, dy))

    def aim_error(self, me, yaw, pitch):
        """Residual angular error after one sim tick of slewing toward (yaw, pitch)."""
        st = me.get("stats", {})
        cy, cp = me["aim"]
        dy = abs(angdiff(yaw, cy)) - st.get("turn_rate", 4.0) * 0.05
        dp = abs(pitch - cp) - st.get("pitch_rate", 3.0) * 0.05
        return max(0.0, dy), max(0.0, dp)

    # ------------------------------------------------------------ probabilities
    def p_projectile(self, d, flight, lat_speed, err_y, err_p, spread_deg, radius, half_h):
        sig_s = math.radians(spread_deg)
        a = SHOOT["target_accel"]
        dodge = SHOOT["pred_noise"] * min(lat_speed * flight, 0.5 * a * flight * flight)
        sh = math.sqrt((sig_s * d) ** 2 + dodge ** 2 + (err_y * d) ** 2 + 0.09)
        sv = math.sqrt((sig_s * d) ** 2 + (0.3 * dodge) ** 2 + (err_p * d) ** 2 + 0.09)
        return erf(radius / (sh * math.sqrt(2))) * erf(half_h / (sv * math.sqrt(2)))

    def shot(self, me, tpos, tvel, cubes=0, kind="mech", radius=None, half_h=None):
        """Everything needed to shoot at a target at tpos with velocity tvel."""
        s = Shot()
        W = self.W
        gun = W.get("gun", {})
        for w in me.get("weapons") or []:
            if w and w.get("kind") == "gun":
                gun = w.get("stats", gun)
        size = 1.0 + self.size_per_cube * (cubes or 0)
        if radius is None:
            radius = (1.8 if kind == "npc" else 2.5 * size)
        if half_h is None:
            half_h = (1.75 if kind == "npc" else 4.5 * size)
        o = self.muzzle(me)
        p, ft = self.lead(o, tpos, tvel, gun.get("speed", 252.0), cubes, kind)
        s.point = p
        s.flight = ft
        s.yaw, s.pitch = self.yaw_pitch(o, p)
        s.d = math.dist(o, p)
        c = self.target_center(tpos, cubes, kind)
        top = (c[0], c[1], c[2] + half_h * 0.8)
        s.los = self.wm.los(o, c) or self.wm.los(o, top)
        ey, ep = self.aim_error(me, s.yaw, s.pitch)
        # lateral speed of the target relative to our line of fire
        bx, by = (p[0] - o[0]), (p[1] - o[1])
        bl = math.hypot(bx, by) or 1.0
        lat = abs(tvel[0] * (-by / bl) + tvel[1] * (bx / bl))
        rng = gun.get("range", 230.0)
        if s.los and s.d <= rng:
            s.p_gun = self.p_projectile(s.d, ft, lat, ey, ep, gun.get("spread_deg", 0.6), radius, half_h)
        lz = W.get("laser", {})
        if s.los and s.d <= lz.get("range", 280.0):
            s.p_laser = self.p_projectile(s.d, 0.1, lat, ey, ep, 0.05, radius, half_h)
        lt = W.get("lightning", {})
        if s.los and s.d <= lt.get("range", 45.0) * 0.95:
            off = abs(angdiff(s.yaw, me["aim"][0]))
            s.p_light = 1.0 if off < math.radians(lt.get("cone_deg", 30.0)) * 0.4 else 0.0
        s.mortar_aim = self.mortar_solution(me, tpos, tvel, cubes, kind)
        if s.mortar_aim is not None:
            yaw, pitch, T, clear = s.mortar_aim
            if clear:
                mo = W.get("mortar", {})
                sr = mo.get("splash_radius", 7.0)
                dodge = SHOOT["pred_noise"] * min(math.hypot(tvel[0], tvel[1]) * T, 0.5 * SHOOT["target_accel"] * T * T)
                ey2, ep2 = self.aim_error(me, yaw, pitch)
                sig = math.sqrt(dodge ** 2 + ((ey2 + ep2) * s.d) ** 2 + 1.0)
                s.p_mortar = erf(sr * 0.6 / (sig * math.sqrt(2)))
        return s

    def mortar_solution(self, me, tpos, tvel, cubes, kind):
        mo = self.W.get("mortar", {})
        for w in me.get("weapons") or []:
            if w and w.get("kind") == "mortar":
                mo = w.get("stats", mo)
        v = mo.get("speed", 72.0)
        g = mo.get("gravity", 22.0)
        o = self.muzzle(me, "mortar")
        T = 1.0
        best = None
        for _ in range(3):
            tx = tpos[0] + tvel[0] * T
            ty = tpos[1] + tvel[1] * T
            tz = tpos[2] + 1.0
            D = math.hypot(tx - o[0], ty - o[1])
            if D < SHOOT["mortar_min_dist"] or D > SHOOT["mortar_max_dist"]:
                return None
            H = tz - o[2]
            disc = v ** 4 - g * (g * D * D + 2 * H * v * v)
            if disc < 0:
                return None
            sq = math.sqrt(disc)
            sols = []
            for sgn in (-1, 1):
                th = math.atan((v * v + sgn * sq) / (g * D))
                sols.append(th)
            yaw = math.atan2(ty - o[1], tx - o[0])
            for th in sols:
                if not (self.mech.get("min_pitch", -0.7) <= th <= self.mech.get("max_pitch", 1.3)):
                    continue
                T2 = D / (v * math.cos(th))
                clear = self._arc_clear(o, yaw, th, v, g, T2)
                if clear:
                    best = (yaw, th, T2, True)
                    break
                if best is None:
                    best = (yaw, th, T2, False)
            if best is None:
                return None
            T = best[2]
        return best

    def _arc_clear(self, o, yaw, th, v, g, T):
        cy, sy = math.cos(yaw), math.sin(yaw)
        vh, vz = v * math.cos(th), v * math.sin(th)
        prev = o
        n = 14
        for q in range(1, n):
            t = T * q / n
            p = (o[0] + cy * vh * t, o[1] + sy * vh * t, o[2] + vz * t - 0.5 * g * t * t)
            if not self.wm.los(prev, p, step=2.0):
                return False
            prev = p
        return True

    # ------------------------------------------------------------ damage models (judgement)
    def mult(self, cubes):
        return 1.0 + self.dpc * min(cubes or 0, self.cap)

    def enemy_dps(self, weapons, d, cubes):
        """Expected damage per second an enemy deals to us at distance d (our armor applied later)."""
        W = self.W
        acc = JUDGE["enemy_acc"]
        kinds = set(weapons or []) | {"gun"}
        dps = 0.0
        if "gun" in kinds and d <= W["gun"].get("range", 230.0):
            g = W["gun"]
            fall = max(0.2, 1.0 - d / 300.0)
            dps += g["damage"] / g["cooldown"] * acc["gun"] * fall
        if "laser" in kinds and d <= W["laser"].get("range", 280.0):
            lz = W["laser"]
            dps += lz["damage"] / (lz["charge"] + lz["cooldown"]) * acc["laser"]
        if "lightning" in kinds and d <= W["lightning"].get("range", 45.0):
            lt = W["lightning"]
            dps += lt["damage"] / lt["cooldown"] * acc["lightning"] * 0.6
        if "mortar" in kinds and SHOOT["mortar_min_dist"] <= d <= SHOOT["mortar_max_dist"]:
            mo = W["mortar"]
            dps += mo["damage"] / mo["cooldown"] * acc["mortar"]
        return dps * self.mult(cubes)

    def our_dps(self, me, d, tvel, cubes_t=0):
        """Expected damage per second we deal to a target at distance d moving at tvel."""
        dps = 0.0
        lat = math.hypot(tvel[0], tvel[1]) * 0.7
        size = 1.0 + self.size_per_cube * (cubes_t or 0)
        for w in me.get("weapons") or []:
            if not w:
                continue
            k, st = w["kind"], w.get("stats", {})
            if k == "gun" and d <= st.get("range", 230):
                ft = d / st.get("speed", 252.0)
                p = self.p_projectile(d, ft, lat, 0, 0, st.get("spread_deg", 0.6), 2.5 * size, 4.5 * size)
                dps += st["damage"] / st["cooldown"] * p * (st.get("mag", 12) * st["cooldown"]) / (st.get("mag", 12) * st["cooldown"] + st.get("reload", 2.4))
            elif k == "laser" and d <= st.get("range", 280):
                p = self.p_projectile(d, 0.1, lat, 0, 0, 0.05, 2.5 * size, 4.5 * size)
                dps += st["damage"] / (st["charge"] + st["cooldown"]) * p
            elif k == "lightning" and d <= st.get("range", 45):
                dps += st["damage"] / st["cooldown"] * 0.6
            elif k == "mortar" and SHOOT["mortar_min_dist"] <= d <= SHOOT["mortar_max_dist"]:
                dps += st["damage"] / st["cooldown"] * 0.3
        return dps * self.mult(me.get("cubes", 0))

    # ------------------------------------------------------------ triggers
    def triggers(self, me, s, mode, t, events=()):
        """Decide aim and fire for a target described by Shot `s`.
        mode: 'fight' | 'finish' | 'third' | 'crate' | 'npc'.  Returns (aim, fire[2], reload)."""
        for e in events:
            if e.get("type") == "effect" and e.get("effect") == "short":
                self.short_until = t + self.W.get("lightning", {}).get("short_s", 2.0)
        thr = SHOOT["min_hit_prob"]
        if mode == "finish":
            thr = SHOOT["finish_hit_prob"]
        elif mode == "crate":
            thr = SHOOT["crate_hit_prob"]
        weapons = me.get("weapons") or [None, None]
        fire = [False, False]
        aim = [s.yaw, s.pitch]
        kinds = {}
        for i, w in enumerate(weapons[:2]):
            if w:
                kinds[w["kind"]] = (i, w)

        # mortar: only when it's the best option (target behind cover or out of gun reach)
        use_mortar = False
        if "mortar" in kinds and s.mortar_aim is not None and mode in ("fight", "finish", "third"):
            i, w = kinds["mortar"]
            if w.get("cooldown", 0) <= 0 and s.p_mortar >= SHOOT["mortar_min_prob"] and (not s.los or s.p_gun < thr):
                use_mortar = True
        if use_mortar:
            i, w = kinds["mortar"]
            yaw, pitch, T, clear = s.mortar_aim
            aim = [yaw, pitch]
            ey, ep = self.aim_error(me, yaw, pitch)
            if ey + ep < 0.02:
                fire[i] = True
            return aim, fire, False

        # laser: hold to charge, release when full and on target
        if "laser" in kinds:
            i, w = kinds["laser"]
            charge = w.get("charge", 0.0) or 0.0
            full = w.get("stats", {}).get("charge", 0.75)
            if charge > 0:
                lead_pt = (s.point[0], s.point[1], s.point[2]) if s.point else None
                if s.los and s.p_laser > 0:
                    self.laser_lost_since = None
                    if charge >= full * SHOOT["laser_release_charge"]:
                        if self.laser_full_since is None:
                            self.laser_full_since = t
                        held = t - self.laser_full_since
                        if s.p_laser >= SHOOT["laser_min_prob"] or held > self.W["laser"].get("hold_s", 2.0) - 0.3:
                            fire[i] = False  # release -> fires
                            self.laser_full_since = None
                        else:
                            fire[i] = True
                    else:
                        fire[i] = True
                else:
                    if self.laser_lost_since is None:
                        self.laser_lost_since = t
                    fire[i] = (t - self.laser_lost_since) < SHOOT["laser_max_hold_lost"]
                    if not fire[i]:
                        self.laser_lost_since = None
                        self.laser_full_since = None
            else:
                self.laser_full_since = None
                if (w.get("cooldown", 0) <= 0 and t >= self.short_until and s.los and
                        s.p_laser >= SHOOT["laser_min_prob"] * 0.85 and mode in ("fight", "finish", "third", "crate", "npc")):
                    fire[i] = True  # start charging

        if "lightning" in kinds:
            i, w = kinds["lightning"]
            if (w.get("cooldown", 0) <= 0 and s.p_light > 0 and
                    me.get("energy", 0) > SHOOT["lightning_energy_reserve"] and mode != "crate"):
                fire[i] = True

        reload = False
        if "gun" in kinds:
            i, w = kinds["gun"]
            ammo = w.get("ammo", 1)
            if w.get("reload", 0) > 0 or ammo <= 0:
                reload = ammo <= 0 and w.get("reload", 0) <= 0
            elif s.p_gun >= thr:
                fire[i] = True
        return aim, fire, reload
