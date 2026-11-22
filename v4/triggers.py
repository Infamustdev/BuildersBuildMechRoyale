"""Triggers: what to fire this tick. Reads the judge's solution and our actual (slewed) aim.

- Gun: in range, loaded, and the aim after one tick of slewing sits inside 0.75 x target radius
  (yaw) / 0.40 x height (pitch) of the lead point. No hit-chance gate.
- Lightning: in range - 1.5 m, target inside cone/2 - 0.05 rad, energy for a shot.
- Laser: hold charge; release at full once lined up, or early when the charge already kills.
  Pre-charge on an engaged mech we cannot see yet.
- Drones and crates (the judge's farm solution): gun and lightning only.
- Reload when nothing is in sight and the magazine is under 60% (or things have been quiet 3 s).
Gun and secondary fire together.
"""
import math

from memory import angdiff


class Triggers:
    def __init__(self, judge):
        self.j = judge
        self.mode = "hold"
        self.shots = {"gun": 0, "laser": 0, "lightning": 0}

    def decide(self, ctx):
        j = self.j
        me = ctx.me
        t = ctx.t
        st = me.get("stats") or {}
        ws = (me.get("weapons") or [])[:2]
        my_aim = me.get("aim") or [0.0, 0.0]
        energy = float(me.get("energy", 0.0))
        fire = [False, False]
        reload = False
        tgt, sol = j.target, j.sol
        self.mode = "hold"

        if sol is not None:
            turn_r = float(st.get("turn_rate", 3.2)) * j.tick_s
            pitch_r = float(st.get("pitch_rate", 2.5)) * j.tick_s

            def slew_ok(wy, wp):
                ey = max(0.0, abs(angdiff(wy, my_aim[0])) - turn_r)
                ep = max(0.0, abs(wp - my_aim[1]) - pitch_r)
                return ey < sol.gun_tol_y and ep < sol.gun_tol_p

            hit_lined = (max(0.0, abs(angdiff(sol.hit_yaw, my_aim[0])) - turn_r) < sol.las_tol_y
                         and max(0.0, abs(sol.hit_pitch - my_aim[1]) - pitch_r) < sol.las_tol_p)
            for i, w in enumerate(ws):
                if not w:
                    continue
                k = w.get("kind")
                wst = w.get("stats") or {}
                if k == "gun":
                    if w.get("ammo", 0) <= 0 and w.get("reload", 0.0) <= 0.0:
                        reload = True            # empty: reload now, even mid-fight
                    if (sol.gun_ok and w.get("ammo", 0) > 0 and w.get("reload", 0.0) <= 0.0
                            and slew_ok(sol.gun_yaw, sol.gun_pitch)):
                        fire[i] = True
                elif k == "lightning":
                    cone = math.radians(float(wst.get("cone_deg", 30.0)))
                    rel = math.hypot(tgt.vel[0] - me["vel"][0], tgt.vel[1] - me["vel"][1]) if tgt is not None else 0.0
                    if (sol.dist < float(wst.get("range", 45.0)) - 1.0 - 0.05 * rel
                            and abs(angdiff(sol.hit_yaw, my_aim[0])) - turn_r * 0.9 < cone * 0.4
                            and energy >= float(wst.get("energy", 3.0))):
                        fire[i] = True
                elif k == "laser":
                    if not sol.is_mech or sol.dist > float(wst.get("range", 200.0)) * 0.97:
                        continue
                    full = float(wst.get("charge", 1.4))
                    chg = float(w.get("charge", 0.0))
                    if sol.lethal and hit_lined:
                        fire[i] = False          # let go: this charge kills
                    elif chg < full - 0.051:
                        fire[i] = True           # keep charging
                    else:
                        fire[i] = not hit_lined  # full: release once lined up
            self.mode = ("fire" if any(fire) else "aim") + (":" + j.farm_kind if j.farm_kind else "")
        elif j.foe is not None:
            # pre-charge the laser on an engaged mech that is about to come round the corner
            for i, w in enumerate(ws):
                if w and w.get("kind") == "laser":
                    wst = w.get("stats") or {}
                    d = j.dist.get(j.foe.id, 1e9)
                    if (w.get("cooldown", 0.0) <= 0.05 and d < float(wst.get("range", 200.0))
                            and w.get("charge", 0.0) < float(wst.get("charge", 1.4)) + float(wst.get("hold_s", 1.5)) - 0.35):
                        fire[i] = True
                        self.mode = "precharge"

        _, gun = j.weapon(me, "gun")
        if gun is not None and gun.get("reload", 0.0) <= 0.0 and sol is None:
            mag = int((gun.get("stats") or {}).get("mag", 12))
            ammo = gun.get("ammo", 0)
            if ammo < mag and (ammo <= mag * 0.6 or t - j.last_threat_at > 3.0):
                reload = True
                self.mode = "reload"
        for i, w in enumerate(ws):
            if fire[i] and w:
                self.shots[w.get("kind")] = self.shots.get(w.get("kind"), 0) + 1
        return fire, reload
