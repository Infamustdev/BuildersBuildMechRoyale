"""What we know about the world that changes: enemies, items, and where unseen enemies probably are.

- Tracks: every mech/NPC in obs.mechs is exact (vision, or the server's 5 s memory). When a track
  drops out, it becomes a ghost and leaves heat at its last position.
- Heat: a decaying grid (nav cells) of "an enemy is probably here", fed by sounds (a wedge along
  the bearing, inside the distance bucket), damage from unseen attackers, and ghosts.
- Items: pickups, crates, cubes are listed only while seen, but they don't move, so we remember them.
"""
import math

from tuning import HEAT, JUDGE, LOOT


def angdiff(a, b):
    d = (a - b + math.pi) % (2 * math.pi) - math.pi
    return d


class Track:
    __slots__ = ("id", "kind", "pos", "vel", "aim", "hp", "max_hp", "cubes", "weapons", "charging",
                 "seen_t", "first_t", "last_hit_us_t", "hp_hist", "visible", "engaged_until",
                 "_head", "_flips")

    def __init__(self, a, t):
        self.id = a["id"]
        self.kind = a.get("kind", "mech")
        self.first_t = t
        self.last_hit_us_t = -1e9
        self.engaged_until = -1e9
        self.hp_hist = []
        self._head = None
        self._flips = []   # times this mech reversed direction
        self.weapons = []
        self.update(a, t)

    def update(self, a, t):
        self.pos = a["pos"]
        self.vel = a.get("vel") or [0.0, 0.0, 0.0]
        self.aim = a.get("aim") or [0.0, 0.0]
        self.hp = a.get("hp", getattr(self, "hp", 1400.0))
        self.max_hp = a.get("max_hp", getattr(self, "max_hp", 1400.0))
        self.cubes = a.get("cubes", 0)
        if a.get("weapons") is not None:
            self.weapons = a["weapons"]
        self.charging = bool(a.get("charging"))
        self.seen_t = t
        self.visible = True
        # Reversal rate: how twitchy this mech is. A target that flips direction often should
        # be led *less*, not more -- it probably will not still be going that way when the
        # bullet arrives. This is the input to the telegraph factor in Combat.lead.
        vx, vy = self.vel[0], self.vel[1]
        if vx * vx + vy * vy > 1.0:
            h = math.atan2(vy, vx)
            if self._head is not None and abs(angdiff(h, self._head)) > 1.6:
                self._flips.append(t)
            self._head = h
        self._flips = [q for q in self._flips if t - q < 4.0]
        self.hp_hist.append((t, self.hp))
        if len(self.hp_hist) > 30:
            del self.hp_hist[:10]

    @property
    def flip_rate(self):
        return len(self._flips) / 4.0

    def losing_hp(self, t, window=2.0):
        """HP lost in the last `window` seconds (someone is fighting it)."""
        old = None
        for (tt, hp) in self.hp_hist:
            if tt >= t - window:
                old = hp
                break
        return 0.0 if old is None else max(0.0, old - self.hp)


class Tracker:
    def __init__(self, wm, cfg):
        self.wm = wm
        self.cfg = cfg
        self.tracks = {}          # id -> Track (mechs and npcs)
        self.heat = {}            # nav cell -> (value, t)
        self.pickups = {}         # id -> {kind,pos,t}
        self.crates = {}          # id -> {pos,hp,t}
        self.cubes = {}           # id -> {pos,t}
        self.drops = []
        self.attacks = []         # (t, by, bearing, weapon, amount)
        self.kills = 0
        self._hsten = None
        self.vis_crates = set()
        self.vis_pickups = set()
        self.vis_cubes = set()
        self.kill_spots = []      # (t, pos) of our kills (cubes there)
        self.self_pos = (0.0, 0.0, 0.0)
        self.t = 0.0
        self._last_prune = 0.0
        snd = cfg.get("sound", {})
        self.noise = math.radians(snd.get("bearing_noise_deg", 7.0))
        self.carry = snd.get("range", {})
        self.near_d = snd.get("near", 60.0)
        self.mid_d = snd.get("mid", 160.0)
        self.near_range = cfg.get("vision", {}).get("near_range", 45.0)

    # ------------------------------------------------------------ heat
    def _heat_sten(self):
        """Smooth deposit kernel: one sound should leave a blob that fades outward, not a dot.

        A single cell per sample gave a scatter of unrelated specks, so the planner saw noise
        where it should have seen "probably over there" shading off in every direction -- the
        same shape a viewshed has.
        """
        if self._hsten is None:
            N, R = self.wm.N, HEAT["blob_r"]
            rc = int(R / N)
            out = []
            for dx in range(-rc, rc + 1):
                for dy in range(-rc, rc + 1):
                    ox, oy = dx * N, dy * N
                    d = math.hypot(ox, oy)
                    if d <= R:
                        out.append((ox, oy, (1.0 - d / R) ** 2))
            tot = sum(q[2] for q in out) or 1.0
            self._hsten = [(ox, oy, g / tot * len(out) * HEAT["blob_gain"]) for ox, oy, g in out]
        return self._hsten

    def add_heat(self, x, y, w):
        for ox, oy, g in self._heat_sten():
            K = self.wm.nidx(x + ox, y + oy)
            if K < 0:
                continue
            v = self.heat_at_cell(K) + w * g
            self.heat[K] = (min(v, 5.0), self.t)

    def heat_at_cell(self, K):
        e = self.heat.get(K)
        if e is None:
            return 0.0
        return e[0] * math.exp(-(self.t - e[1]) / HEAT["tau"])

    def heat_near(self, x, y, r=12.0):
        """Sum of heat in nav cells within r of (x,y)."""
        wm = self.wm
        s = 0.0
        R = int(r * wm.ninv) + 1
        I0, J0 = int((x - wm.x0) * wm.ninv), int((y - wm.y0) * wm.ninv)
        for J in range(J0 - R, J0 + R + 1):
            for I in range(I0 - R, I0 + R + 1):
                if 0 <= I < wm.nnx and 0 <= J < wm.nny:
                    K = J * wm.nnx + I
                    if K in self.heat:
                        s += self.heat_at_cell(K)
        return s

    def hotspots(self, max_n=6, min_v=None):
        """Strongest heat cells as [(x, y, value)], merged so spots are >= 20 m apart."""
        if min_v is None:
            min_v = HEAT["hot"]
        cells = []
        for K in self.heat:
            v = self.heat_at_cell(K)
            if v >= min_v:
                cells.append((v, K))
        cells.sort(reverse=True)
        out = []
        for v, K in cells:
            x, y = self.wm.ncenter(K)
            if all((x - a) ** 2 + (y - b) ** 2 > 400 for a, b, _ in out):
                out.append((x, y, v))
                if len(out) >= max_n:
                    break
        return out

    def _sound_heat(self, s):
        kind = s.get("kind", "")
        w = HEAT["sound_weight"].get(kind, 0.5)
        if w <= 0:
            return
        sx, sy = self.self_pos[0], self.self_pos[1]
        bearing = s["bearing"]
        carry = self.carry.get(kind, 300.0)
        bucket = s.get("dist", "far")
        if bucket == "near":
            lo, hi = 4.0, min(self.near_d, carry)
        elif bucket == "mid":
            lo, hi = self.near_d, min(self.mid_d, carry)
        else:
            lo, hi = self.mid_d, carry
        if hi <= lo:
            hi = lo + 10.0
        # explained by a mech we can see? then it adds nothing new
        for tr in self.tracks.values():
            if tr.kind != "mech" or not tr.visible:
                continue
            dx, dy = tr.pos[0] - sx, tr.pos[1] - sy
            d = math.hypot(dx, dy)
            if lo - 15 <= d <= hi + 15 and abs(angdiff(math.atan2(dy, dx), bearing)) < 2.5 * self.noise + 0.05:
                return
        pts = []
        step = max(6.0, (hi - lo) / 10.0)
        d = lo + step * 0.5
        while d < hi:
            for k, gw in ((-1.5, 0.32), (-0.75, 0.75), (0.0, 1.0), (0.75, 0.75), (1.5, 0.32)):
                a = bearing + k * self.noise
                x, y = sx + math.cos(a) * d, sy + math.sin(a) * d
                K = self.wm.nidx(x, y)
                if K < 0:
                    continue
                pts.append((x, y, gw * (0.4 if self.wm.nblock[K] else 1.0)))
            d += step
        tot = sum(p[2] for p in pts) or 1.0
        # spread the sound's weight over its wedge, but keep the peak meaningful
        scale = w * max(1.0, len(pts) / 12.0) / tot
        for x, y, gw in pts:
            self.add_heat(x, y, gw * scale)

    # ------------------------------------------------------------ update
    def update(self, obs):
        t = obs["t"]
        self.t = t
        me = obs["self"]
        self.self_pos = me["pos"]
        seen = set()
        for a in obs.get("mechs", []):
            tr = self.tracks.get(a["id"])
            if tr is None:
                self.tracks[a["id"]] = tr = Track(a, t)
            else:
                tr.update(a, t)
            seen.add(a["id"])
        for tid, tr in list(self.tracks.items()):
            if tid in seen:
                continue
            if tr.visible:
                # just lost it: leave heat where it was heading
                tr.visible = False
                if tr.kind == "mech":
                    px = tr.pos[0] + tr.vel[0] * 1.0
                    py = tr.pos[1] + tr.vel[1] * 1.0
                    self.add_heat(px, py, HEAT["ghost_weight"])
            if t - tr.seen_t > 60:
                del self.tracks[tid]

        for e in obs.get("events", []):
            et = e.get("type")
            if et == "damaged":
                by = e.get("by")
                self.attacks.append((t, by, e.get("bearing"), e.get("weapon"), e.get("amount", 0.0)))
                if by is not None and by in self.tracks:
                    self.tracks[by].last_hit_us_t = t
                elif by is not None and e.get("bearing") is not None and e.get("weapon") not in ("zone", "npc"):
                    rng = {"gun": 120.0, "laser": 150.0, "mortar": 140.0, "lightning": 30.0}.get(e.get("weapon"), 100.0)
                    b = e["bearing"]
                    for d in (0.5 * rng, rng, 1.5 * rng):
                        self.add_heat(self.self_pos[0] + math.cos(b) * d, self.self_pos[1] + math.sin(b) * d, 0.8)
            elif et == "kill":
                self.kills += 1
                tr = self.tracks.get(e.get("target"))
                if tr is not None:
                    self.kill_spots.append((t, tuple(tr.pos)))
                    tr.hp = 0
                    self.tracks.pop(e.get("target"), None)
            elif et == "took":
                self.pickups.pop(e.get("id"), None)
            elif et == "take_failed" and e.get("reason") == "gone":
                self.pickups.pop(e.get("id"), None)
        self.attacks = [a for a in self.attacks if t - a[0] < 10.0]
        self.kill_spots = [k for k in self.kill_spots if t - k[0] < 40.0]

        for s in obs.get("sounds", []):
            self._sound_heat(s)

        # items: refresh what we see, forget what should be visible but isn't
        sx, sy = me["pos"][0], me["pos"][1]
        vis_p = set()
        for p in obs.get("pickups", []):
            self.pickups[p["id"]] = {"kind": p["kind"], "pos": p["pos"], "t": t}
            vis_p.add(p["id"])
        vis_c = set()
        for c in obs.get("crates", []):
            self.crates[c["id"]] = {"pos": c["pos"], "hp": c.get("hp", 300.0), "t": t}
            vis_c.add(c["id"])
        vis_q = set()
        for c in obs.get("cubes", []):
            self.cubes[c["id"]] = {"pos": c["pos"], "t": t}
            vis_q.add(c["id"])
        # Items do not move, so what we have seen is worth keeping: that is the whole return on
        # the opening climb, and the reason the planner can route between loot it cannot
        # currently see. We used to delete anything within 36 m that was missing from this one
        # observation, so a tree crown or a strafe behind a wall wiped a crate off our map
        # mid-engagement. Now we only forget on real evidence -- close, clear line of sight,
        # and still not there, which means someone took it -- or after memory_s.
        nr = LOOT["forget_near_m"]
        eye = (sx, sy, me["pos"][2] + 8.0)
        for store, vis in ((self.pickups, vis_p), (self.crates, vis_c), (self.cubes, vis_q)):
            for iid in list(store):
                if iid in vis:
                    continue
                p = store[iid]["pos"]
                close = (p[0] - sx) ** 2 + (p[1] - sy) ** 2 < nr * nr and abs(p[2] - me["pos"][2]) < 6
                if close and self.wm.los(eye, (p[0], p[1], p[2] + 1.0)):
                    del store[iid]
                elif t - store[iid]["t"] > LOOT["memory_s"]:
                    del store[iid]
        # what is in THIS observation, as opposed to what we remember being there
        self.vis_crates, self.vis_pickups, self.vis_cubes = vis_c, vis_p, vis_q
        self.drops = obs.get("drops", []) or []

        if t - self._last_prune > 2.0:
            self._last_prune = t
            for K in list(self.heat):
                if self.heat_at_cell(K) < HEAT["prune"]:
                    del self.heat[K]

    # ------------------------------------------------------------ queries
    def enemies(self, max_age=0.01):
        """Mechs currently in obs (visible or server memory)."""
        return [tr for tr in self.tracks.values() if tr.kind == "mech" and self.t - tr.seen_t <= max_age + 1e-6]

    def npcs(self):
        return [tr for tr in self.tracks.values() if tr.kind == "npc" and tr.visible]

    def recent_attackers(self, window=None):
        if window is None:
            window = JUDGE["attack_memory_s"]
        out = [a for a in self.attacks if self.t - a[0] <= window and a[3] not in ("zone",)]
        return out

    def threat_near(self, x, y, r):
        """Closest known/ghost enemy distance to (x,y), considering recently lost tracks."""
        best = 1e9
        for tr in self.tracks.values():
            if tr.kind != "mech":
                continue
            age = self.t - tr.seen_t
            if age > 15:
                continue
            d = math.hypot(tr.pos[0] - x, tr.pos[1] - y) - age * 6.0
            best = min(best, d)
        return best
