"""What the bot remembers between ticks: enemy tracks, items, recent hits on us, recent sounds.

Pure bookkeeping. Nothing here decides anything; the heatmaps and the four controllers read it.
"""
import math


def angdiff(a, b):
    return (a - b + math.pi) % (2 * math.pi) - math.pi


class Track:
    __slots__ = ("id", "kind", "pos", "vel", "aim", "hp", "max_hp", "cubes", "weapons", "charging",
                 "seen_t", "visible", "last_hit_us_t")

    def __init__(self, a, t):
        self.id = a["id"]
        self.kind = a.get("kind", "mech")
        self.last_hit_us_t = -1e9
        self.weapons = []
        self.hp = 1400.0
        self.max_hp = 1400.0
        self.update(a, t)

    def update(self, a, t):
        self.pos = a["pos"]
        self.vel = a.get("vel") or [0.0, 0.0, 0.0]
        self.aim = a.get("aim") or [0.0, 0.0]
        self.hp = a.get("hp", self.hp)
        self.max_hp = a.get("max_hp", self.max_hp)
        self.cubes = a.get("cubes", 0)
        if a.get("weapons") is not None:
            self.weapons = a["weapons"]
        self.charging = bool(a.get("charging"))
        self.seen_t = t
        self.visible = True


class Memory:
    def __init__(self, cfg, wm):
        self.wm = wm
        self.t = 0.0
        self.me = None
        self.tracks = {}       # id -> Track
        self.lost = []         # tracks that dropped out this tick (the heatmap leaves a ghost)
        self.hits = []         # (t, by, bearing, weapon, amount) damage we took, last 10 s
        self.sounds = []       # this tick's sounds
        self.last_sound = None  # (t, bearing) of the last mech-made sound
        self.laser_fired = {}   # mech id -> time it last fired its laser (2 s cooldown after)
        self.last_beam = None   # (t, x, y) muzzle of the last laser/lightning by a mech we cannot see
        self.pickups, self.crates, self.cubes = {}, {}, {}
        self.kills = 0
        self.npc_hit = None     # (t, bearing) of the last drone hit on us

    def update(self, obs):
        t = obs["t"]
        self.t = t
        self.me = obs["self"]
        seen = set()
        for a in obs.get("mechs", []):
            tr = self.tracks.get(a["id"])
            if tr is None:
                self.tracks[a["id"]] = Track(a, t)
            else:
                tr.update(a, t)
            seen.add(a["id"])
        self.lost = []
        for tid, tr in list(self.tracks.items()):
            if tid in seen:
                continue
            if tr.visible:
                tr.visible = False
                if tr.kind == "mech":
                    self.lost.append(tr)
            if t - tr.seen_t > 60:
                del self.tracks[tid]

        for e in obs.get("events", []):
            et = e.get("type")
            if et == "damaged":
                by = e.get("by")
                self.hits.append((t, by, e.get("bearing"), e.get("weapon"), e.get("amount", 0.0)))
                if by in self.tracks:
                    self.tracks[by].last_hit_us_t = t
                if e.get("weapon") == "npc" and e.get("bearing") is not None:
                    self.npc_hit = (t, e["bearing"])
            elif et == "kill":
                self.kills += 1
                self.tracks.pop(e.get("target"), None)
            elif et == "took" or (et == "take_failed" and e.get("reason") == "gone"):
                self.pickups.pop(e.get("id"), None)
        self.hits = [h for h in self.hits if t - h[0] < 10.0]

        self.sounds = obs.get("sounds", []) or []
        for s in self.sounds:
            if s.get("kind") not in ("npc_gun", "drop", "death", "explosion", "crate"):
                self.last_sound = (t, s["bearing"])

        for b in obs.get("beams", []) or []:
            tr = self.tracks.get(b.get("by"))
            path = b.get("path") or []
            if tr is not None and b.get("kind", b.get("weapon")) == "laser":
                self.laser_fired[tr.id] = t - float(b.get("age", 0.0))
            if path and not (tr is not None and tr.visible):
                self.last_beam = (t - float(b.get("age", 0.0)), path[0][0], path[0][1])
        for p in obs.get("pickups", []):
            self.pickups[p["id"]] = {"kind": p["kind"], "pos": p["pos"], "t": t}
        for c in obs.get("crates", []):
            self.crates[c["id"]] = {"pos": c["pos"], "t": t}
        for c in obs.get("cubes", []):
            self.cubes[c["id"]] = {"pos": c["pos"], "t": t}
        # Items do not move: forget one only when it should be in view and is not (within 40 m,
        # clear line, missed for 0.35 s), or after 90 s.
        x, y, z = self.me["pos"]
        eye = (x, y, z + 8.0)
        for store in (self.pickups, self.crates, self.cubes):
            for iid, v in list(store.items()):
                if v["t"] == t:
                    continue
                p = v["pos"]
                if t - v["t"] > 90.0 or (t - v["t"] > 0.35 and math.hypot(p[0] - x, p[1] - y) < 40.0
                                          and self.wm.los(eye, (p[0], p[1], p[2] + 1.5), canopy=False)):
                    del store[iid]

    def enemies(self, max_age=0.01):
        """Mechs in this observation (the server's own memory included)."""
        return [tr for tr in self.tracks.values()
                if tr.kind == "mech" and self.t - tr.seen_t <= max_age + 1e-6]

    def recent_mechs(self, max_age):
        return [tr for tr in self.tracks.values() if tr.kind == "mech" and self.t - tr.seen_t <= max_age]
