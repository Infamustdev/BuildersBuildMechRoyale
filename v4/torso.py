"""Torso: where to look and aim. Reads only the shared picture, never the legs' plan.

Target: the judge's gun lead aim, or the hitscan aim once the laser is about to release.
No target, in order:
  1. pre-aim (yaw and pitch) at the nearest engaged mech or one that hit us in 6 s, so the gun is
     already on it when it steps out
  2. one fast turn in the opening (1.75 s)
  3. the muzzle of a laser/lightning beam fired by a mech we cannot see (1.5 s)
  4. the last hit on us (2.5 s), then the last mech sound (1.5 s)
  5. the hottest likely-enemy cell within 150 m (heat layer), with a narrow +-0.5 rad sweep
  6. a slow +-1.9 rad sweep around the walking direction
Never spins: every look is a bearing the server slews to at turn rate.
"""
import math

OPEN_TURN_S = 1.75
HIT_S = 2.5
SOUND_S = 1.5
SWEEP_RAD = 1.9
SWEEP_RATE = 1.3     # rad/s of sweep phase
BEAM_S = 1.5
HOT_R = 150.0
HOT_SWEEP = 0.5
HOT_EVERY = 0.5      # s between hotspot searches


def yaw_pitch(o, p):
    dx, dy, dz = p[0] - o[0], p[1] - o[1], p[2] - o[2]
    return math.atan2(dy, dx), math.atan2(dz, math.hypot(dx, dy))


class Torso:
    def __init__(self, judge):
        self.j = judge
        self.t0 = None
        self.phase = 0.0
        self.last_t = None
        self.mode = "sweep"
        self.heading = 0.0
        self.hot, self.hot_t = None, -1e9
        self.look = None     # (x, y) or bearing we are looking toward, for the viewer

    def decide(self, ctx):
        me = ctx.me
        t = ctx.t
        if self.t0 is None:
            self.t0 = t
        dt = 0.0 if self.last_t is None else t - self.last_t
        self.last_t = t
        px, py, pz = me["pos"]
        vx, vy = me["vel"][0], me["vel"][1]
        if vx * vx + vy * vy > 4.0:
            self.heading = math.atan2(vy, vx)

        j = self.j
        st = me.get("stats") or {}
        if j.sol is not None:
            sol = j.sol
            self.mode = "laser" if sol.hitscan else ("farm" if j.farm_kind else "target")
            yaw, pitch = (sol.hit_yaw, sol.hit_pitch) if sol.hitscan else (sol.gun_yaw, sol.gun_pitch)
            return [yaw, max(float(st.get("min_pitch", -0.7)), min(float(st.get("max_pitch", 1.3)), pitch))]
        self.look = None
        watch = [tr for tr in ctx.mem.recent_mechs(2.5) if tr in j.engaged or t - tr.last_hit_us_t < 6.0]
        if watch:
            tr = min(watch, key=lambda e: (e.pos[0] - px) ** 2 + (e.pos[1] - py) ** 2)
            self.mode = "watch"
            self.look = (tr.pos[0], tr.pos[1])
            h = j.mech_h * (1.0 + j.size_per_cube * (tr.cubes or 0))
            yaw, pitch = yaw_pitch(j.muzzle, (tr.pos[0], tr.pos[1], tr.pos[2] + h * 0.5))
            return [yaw, max(float(st.get("min_pitch", -0.7)), min(float(st.get("max_pitch", 1.3)), pitch))]
        if t - self.t0 < OPEN_TURN_S:
            self.mode = "open"
            return [me["aim"][0] + 1.5, 0.0]
        lb = ctx.mem.last_beam
        if lb and t - lb[0] <= BEAM_S:
            self.mode = "beam"
            self.look = (lb[1], lb[2])
            return [math.atan2(lb[2] - py, lb[1] - px), 0.0]
        for h in reversed(ctx.mem.hits):
            if t - h[0] <= HIT_S and h[2] is not None and h[3] not in ("zone",):
                self.mode = "hit"
                return [h[2], 0.0]
        ls = ctx.mem.last_sound
        if ls and t - ls[0] <= SOUND_S:
            self.mode = "sound"
            return [ls[1], 0.0]
        self.phase += dt * SWEEP_RATE
        if t - self.hot_t >= HOT_EVERY:
            self.hot, self.hot_t = ctx.heat.hotspot(px, py, HOT_R), t
        if self.hot is not None:
            self.mode = "heat"
            self.look = (self.hot[0], self.hot[1])
            b = math.atan2(self.hot[1] - py, self.hot[0] - px)
            return [b + HOT_SWEEP * math.sin(self.phase), 0.0]
        self.mode = "sweep"
        return [self.heading + SWEEP_RAD * math.sin(self.phase), 0.0]
