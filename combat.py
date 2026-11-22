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
                 "mortar_aim", "los", "point", "gun_lined", "laser_aim", "laser_lined", "in_gun_range",
                 "in_laser_range")

    def __init__(self):
        self.yaw = self.pitch = 0.0
        self.d = 1e9
        self.flight = 0.0
        self.p_gun = self.p_laser = self.p_light = self.p_mortar = 0.0
        self.mortar_aim = None
        self.los = False
        self.point = None
        self.gun_lined = self.laser_lined = False
        self.in_gun_range = self.in_laser_range = False
        self.laser_aim = None


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

    def lead(self, origin, tpos, tvel, speed, cubes=0, kind="mech", extra_t=0.0, flip_rate=0.0):
        """Predict where a projectile of `speed` meets the target. Returns (point, flight_time).

        Horizontal velocity is damped by the telegraph factor f = (1 - e^(-2.r.T)) / (2.r.T),
        where r is how often this target reverses. Leading a juking mech by its full current
        velocity aims at where it would be if it kept going, which is exactly what it will not
        do; f is the least-squares lead for a target that flips at Poisson rate r, and tends to
        1 for a steady walker and to 0 for something thrashing back and forth.
        """
        c = self.target_center(tpos, cubes, kind)
        t = 0.0
        g = self.mech.get("gravity", 22.0)
        vz = tvel[2] if len(tvel) > 2 else 0.0
        p = c
        for _ in range(4):
            tt = t + extra_t
            z = c[2] + (vz * tt - 0.5 * g * tt * tt if abs(vz) > 0.5 else 0.0)
            z = max(z, self.wm.surface(c[0], c[1]) + 1.0) if abs(vz) > 0.5 else z
            f = 1.0
            if flip_rate > 1e-6 and tt > 1e-6:
                k = 2.0 * flip_rate * tt
                f = (1.0 - math.exp(-k)) / k
            p = (c[0] + tvel[0] * tt * f, c[1] + tvel[1] * tt * f, z)
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

    def shot(self, me, tpos, tvel, cubes=0, kind="mech", radius=None, half_h=None, flip_rate=0.0):
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
        p, ft = self.lead(o, tpos, tvel, gun.get("speed", 252.0), cubes, kind, flip_rate=flip_rate)
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
        frac = SHOOT["lineup_frac"]
        s.in_gun_range = s.d <= rng
        if s.los and s.in_gun_range:
            s.p_gun = self.p_projectile(s.d, ft, lat, ey, ep, gun.get("spread_deg", 0.6), radius, half_h)
            s.gun_lined = ey <= math.atan(radius * frac / s.d) and ep <= math.atan(half_h * frac / s.d)
        lz = W.get("laser", {})
        # laser is hitscan: lead only by the ~0.1 s until the release lands
        lp, _ = self.lead(o, tpos, tvel, 1e9, cubes, kind, extra_t=0.1, flip_rate=flip_rate)
        s.laser_aim = self.yaw_pitch(o, lp)
        s.in_laser_range = s.d <= lz.get("range", 280.0)
        if s.los and s.in_laser_range:
            ly, lpp = self.aim_error(me, s.laser_aim[0], s.laser_aim[1])
            s.p_laser = self.p_projectile(s.d, 0.1, lat, ly, lpp, 0.05, radius, half_h)
            s.laser_lined = ly <= math.atan(radius * frac / s.d) and lpp <= math.atan(half_h * frac / s.d)
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
        weapons = me.get("weapons") or [None, None]
        fire = [False, False]
        aim = [s.yaw, s.pitch]
        kinds = {}
        for i, w in enumerate(weapons[:2]):
            if w:
                kinds[w["kind"]] = (i, w)

        # mortar: only when it's the best option (target behind cover or out of gun reach)
        use_mortar = False
        if "mortar" in kinds and s.mortar_aim is not None and s.mortar_aim[3] and mode in ("fight", "finish", "third"):
            i, w = kinds["mortar"]
            if w.get("cooldown", 0) <= 0 and (not s.los or not s.in_gun_range):
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
                if s.los and s.in_laser_range:
                    self.laser_lost_since = None
                    if charge >= full * SHOOT["laser_release_charge"]:
                        # about to release: point at the hitscan lead, not the bullet lead
                        aim = [s.laser_aim[0], s.laser_aim[1]]
                        if self.laser_full_since is None:
                            self.laser_full_since = t
                        held = t - self.laser_full_since
                        if s.laser_lined or held > self.W["laser"].get("hold_s", 2.0) - 0.3:
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
                if (w.get("cooldown", 0) <= 0 and t >= self.short_until and s.los and s.in_laser_range
                        and mode in ("fight", "finish", "third", "crate", "npc")):
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
            else:
                # Fire on expected value, not on "the crosshair crossed them". A miss costs a
                # round, the reload that follows and a 320 m noise; the threshold is lower up
                # close and against something we can finish, higher when the magazine is nearly
                # out and a reload would take us out of the fight.
                if mode in ("crate", "npc"):
                    # A crate does not dodge and does not shoot back. Gating it on hit
                    # probability starved the damage check that decides whether we are making
                    # progress, so we shelved crates we were standing in front of.
                    need = 0.0
                else:
                    need = SHOOT["p_fire"]
                    if s.d < SHOOT["p_fire_near_m"] or mode == "finish":
                        need = SHOOT["p_fire_near"]
                    elif ammo <= SHOOT["p_fire_low_ammo"]:
                        need = SHOOT["p_fire_low"]
                if s.gun_lined and s.p_gun >= need:
                    fire[i] = True
        return aim, fire, reload
