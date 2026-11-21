"""Combat v3 (owner: teammate). Improve freely, but keep decide_combat(obs, memory).

decide_combat(obs, memory) -> dict with ONLY: aim, fire, reload

In practice we fight 15 house bots that hunt us, so kills = better placement. This file:
- engages any enemy mech in gun range, favouring weak ones and ones showing us their back (1.5x damage)
- never wastes shots into walls (uses the map), lobs the mortar OVER walls instead
- leads moving targets with the gun; laser = charge then release when lined up; lightning up close
- NPC drones only when they shoot us; crates only when no enemy has been seen for a while
- with no target: faces whoever last hit us, else the direction we're walking / looking out from cover
"""
import math

ENGAGE_RANGE = 190.0     # shoot enemy mechs closer than this (gun range is 230)
CRATE_RANGE = 45.0
CRATE_QUIET_S = 6.0      # only shoot crates if no enemy seen for this long


def _d3(a, b):
    return math.dist(a, b)


def _d2(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _angdiff(a, b):
    return abs((a - b + math.pi) % (2 * math.pi) - math.pi)


def _aim_at(src, dst):
    return math.atan2(dst[1] - src[1], dst[0] - src[0]), math.atan2(dst[2] - src[2], _d2(src, dst))


def _lead(src, tpos, tvel, speed):
    p = list(tpos)
    for _ in range(3):
        tt = _d3(src, p) / max(speed, 1.0)
        p = [tpos[0] + tvel[0] * tt, tpos[1] + tvel[1] * tt, tpos[2] + tvel[2] * tt]
    return p


def _mortar_pitch(dist, dz, v, g, high=False):
    v2 = v * v
    disc = v2 * v2 - g * (g * dist * dist + 2 * dz * v2)
    if disc < 0 or dist < 1:
        return None
    root = math.sqrt(disc)
    return math.atan((v2 + root) / (g * dist)) if high else math.atan((v2 - root) / (g * dist))


def decide_combat(obs, memory):
    me = obs["self"]
    pos = me["pos"]
    t = obs["t"]
    stats = me.get("stats", {})
    muzzle = [pos[0], pos[1], pos[2] + stats.get("muzzle_height", 7.0)]
    weapons = me["weapons"]
    w0 = weapons[0] if len(weapons) > 0 else None
    w1 = weapons[1] if len(weapons) > 1 else None
    gmap = memory.get("map")
    if gmap is not None and not gmap.ok:
        gmap = None
    out = {"fire": [False, False]}

    # who is shooting us (remembered 5 s)
    att = memory.setdefault("attackers", {})
    for ev in obs.get("events", []):
        if ev.get("type") == "damaged" and ev.get("by") is not None:
            att[ev["by"]] = t
    attackers = {k for k, when in att.items() if t - when < 5}

    def wall_between(p):
        return gmap is not None and gmap.blocked(pos, p, step=4.0)

    # ---- choose target ----
    target, tkind, best, t_blocked = None, None, 1e9, False
    for m in obs["mechs"]:
        d = _d3(pos, m["pos"])
        if m["kind"] == "mech":
            if d > ENGAGE_RANGE and m["id"] not in attackers:
                continue
            hp_frac = m.get("hp", 1000) / max(1.0, m.get("max_hp", 1400))
            back_to_us = _angdiff(m["aim"][0], math.atan2(pos[1] - m["pos"][1], pos[0] - m["pos"][0])) > math.pi / 2
            score = d + hp_frac * 120 - (40 if m["id"] in attackers else 0) - (30 if back_to_us else 0)
        else:
            if m["id"] not in attackers:
                continue
            score = d + 100
        blocked = wall_between(m["pos"])
        if blocked:
            score += 150  # still possible with the mortar, but prefer clear shots
        if score < best:
            target, tkind, best, t_blocked = m, m["kind"], score, blocked

    if target is None and t - memory.get("last_enemy_t", -99) > CRATE_QUIET_S:
        crates = [c for c in obs.get("crates", []) if _d3(pos, c["pos"]) < CRATE_RANGE and not wall_between(c["pos"])]
        if crates:
            c = min(crates, key=lambda c: _d3(pos, c["pos"]))
            target, tkind = {"pos": c["pos"], "vel": [0, 0, 0], "id": c["id"]}, "crate"

    # ---- no target ----
    if target is None:
        if t - memory.get("hit_t", -99) < 3.0:
            out["aim"] = [memory["hit_bearing"], 0.0]
        elif "move_yaw" in memory:
            out["aim"] = [memory["move_yaw"], 0.0]
        if w0 and w0["kind"] == "gun" and w0.get("ammo", 99) < w0["stats"].get("mag", 12) * 0.6 and w0.get("reload", 0) == 0:
            out["reload"] = True
        return out

    # ---- aim + fire ----
    height = 4.5 if tkind == "mech" else (1.75 if tkind == "npc" else 0.5)
    tpos = [target["pos"][0], target["pos"][1], target["pos"][2] + height]
    tvel = target.get("vel", [0, 0, 0])
    dist = _d3(muzzle, tpos)
    cur_yaw, cur_pitch = me["aim"]
    tol = max(0.015, math.atan2(1.8, max(dist, 1.0)))

    plan = "gun"
    if w1:
        k = w1["kind"]
        if k == "lightning" and dist < w1["stats"].get("range", 45) - 3 and not t_blocked:
            plan = "lightning"
        elif k == "laser" and dist < w1["stats"].get("range", 280) and not t_blocked:
            plan = "laser"
        elif k == "mortar" and 25 < dist < 220 and w1.get("cooldown", 0) <= 0 and (t_blocked or _d2(tvel, (0, 0)) < 3):
            plan = "mortar"  # over walls, or at targets standing still
    if t_blocked and plan != "mortar":
        # can't hit through the wall: keep facing them (front armor) but hold fire
        out["aim"] = list(_aim_at(muzzle, tpos))
        return out

    if plan == "mortar":
        st = w1["stats"]
        v, g = st.get("speed", 72), st.get("gravity", 22)
        tube = [pos[0], pos[1], pos[2] + stats.get("height", 9.0) + 0.5]
        lp = _lead(tube, [tpos[0], tpos[1], target["pos"][2]], tvel, v * 0.7)
        pitch = _mortar_pitch(_d2(tube, lp), lp[2] - tube[2], v, g, high=t_blocked)
        if pitch is not None and pitch <= stats.get("max_pitch", 1.4):
            yaw = math.atan2(lp[1] - tube[1], lp[0] - tube[0])
            out["aim"] = [yaw, pitch]
            if _angdiff(cur_yaw, yaw) < 0.03 and abs(cur_pitch - pitch) < 0.02:
                out["fire"][1] = True
            return out
        if t_blocked:
            out["aim"] = list(_aim_at(muzzle, tpos))
            return out
        plan = "gun"

    speed = w0["stats"].get("speed", 252) if w0 else 252
    laser_release = plan == "laser" and w1.get("charge", 0) >= w1["stats"].get("charge", 0.75) * 0.9
    aim_pt = tpos if laser_release else _lead(muzzle, tpos, tvel, speed)  # laser is instant: no lead
    yaw, pitch = _aim_at(muzzle, aim_pt)
    out["aim"] = [yaw, pitch]
    aligned = _angdiff(cur_yaw, yaw) < tol and abs(cur_pitch - pitch) < tol * 1.5

    if w0 and w0["kind"] == "gun" and dist < w0["stats"].get("range", 230) and not laser_release:
        if aligned and w0.get("ammo", 1) > 0:
            out["fire"][0] = True

    if w1 and plan == "lightning":
        cone = math.radians(w1["stats"].get("cone_deg", 30)) / 2
        if _angdiff(cur_yaw, yaw) < cone * 0.8 and me["energy"] > 6:
            out["fire"][1] = True
    elif w1 and plan == "laser" and w1.get("cooldown", 0) <= 0:
        # hold to charge; release (fire=False) once charged and lined up with where they ARE
        out["fire"][1] = not (laser_release and aligned)

    return out
