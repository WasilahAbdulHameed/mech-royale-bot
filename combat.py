"""Combat v7 merged (owner: teammate) = v6 combat + the teammate's v7 improvements:
- gun checks its OWN lead aim when lightning/laser is steering the torso
- laser: release once lined up, never hold so long it auto-fires; no new charge while escaping
- ids compared as strings for farm_target
(v6's 150 m engage range and aim tolerance are kept - v7's tighter values scored lower.)

Earlier notes: Combat v6 = v5 combat + the best ideas from the teammate's version.
Keep decide_combat(obs, memory) and return only aim/fire/reload.

From the teammate's version: don't START new fights when we're badly hurt (but always fight
back and always finish off a nearly-dead enemy), cap how far ahead we lead a target,
keep an energy reserve for lightning, and top up the gun early when idle.

Priorities:
  1. Enemy mechs (kills = points). Prefer weak ones, ones shooting us, ones showing their back.
  2. Whatever movement.py is farming (memory["farm_target"]): an NPC drone or a crate.
  3. NPC drones that are shooting us.
Then: lead moving targets with the gun, laser = charge and release when lined up,
lightning whenever something is inside its cone, mortar over walls / at slow targets.
With nothing to shoot: face whoever hit us, otherwise sweep the view to spot loot and enemies.
"""
import math

ENGAGE_RANGE = 150.0     # start fights with enemy mechs closer than this (gun range is 230)
LOW_HP_NO_NEW_FIGHTS = 0.45  # below this, only fight back / finish kills
FINISH_HP = 0.2          # an enemy below this fraction is always worth finishing (if < 70 m)


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
        tt = min(1.5, _d3(src, p) / max(speed, 1.0))  # don't guess too far ahead
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

    att = memory.setdefault("attackers", {})
    for ev in obs.get("events", []):
        if ev.get("type") == "damaged" and ev.get("by") is not None:
            att[ev["by"]] = t
    attackers = {k for k, when in att.items() if t - when < 5}

    def wall_between(p):
        return gmap is not None and gmap.blocked(pos, p, step=4.0)

    my_hp = me["hp"] / max(1.0, me["max_hp"])

    # ---- 1. enemy mechs ----
    target, tkind, best, t_blocked = None, None, 1e9, False
    for m in obs["mechs"]:
        if m["kind"] != "mech":
            continue
        d = _d3(pos, m["pos"])
        hp_frac = m.get("hp", 1000) / max(1.0, m.get("max_hp", 1400))
        is_att = m["id"] in attackers
        can_finish = hp_frac < FINISH_HP and d < 70
        if not is_att and not can_finish:
            if d > ENGAGE_RANGE or my_hp < LOW_HP_NO_NEW_FIGHTS:
                continue
        back = _angdiff(m["aim"][0], math.atan2(pos[1] - m["pos"][1], pos[0] - m["pos"][0])) > math.pi / 2
        score = d + hp_frac * 120 - (40 if is_att else 0) - (30 if back else 0) - (40 if can_finish else 0)
        blocked = wall_between(m["pos"])
        if blocked:
            score += 150
        if score < best:
            target, tkind, best, t_blocked = m, "mech", score, blocked

    # ---- 2. farm target / 3. drones shooting us ----
    if target is None:
        fid = memory.get("farm_target")
        for n in obs["mechs"]:
            if n["kind"] == "npc" and ((fid is not None and str(n["id"]) == str(fid)) or n["id"] in attackers):
                if not wall_between(n["pos"]):
                    target, tkind = n, "npc"
                    break
        if target is None and fid is not None:
            for c in obs.get("crates", []):
                if str(c["id"]) == str(fid) and not wall_between(c["pos"]):
                    target, tkind = {"pos": c["pos"], "vel": [0, 0, 0], "id": c["id"]}, "crate"
                    break

    gun_ok = w0 is not None and w0["kind"] == "gun"
    if gun_ok and w0.get("ammo", 1) == 0 and w0.get("reload", 0) == 0:
        out["reload"] = True

    # ---- nothing to shoot: look around ----
    if target is None:
        if t - memory.get("hit_t", -99) < 3.0:
            out["aim"] = [memory["hit_bearing"], 0.0]
        else:
            base = memory.get("move_yaw", 0.0)
            # sweep +-70 degrees around where we're going/facing to spot loot and enemies
            out["aim"] = [base + 1.2 * math.sin(t * 1.6), 0.0]
        if gun_ok and w0.get("ammo", 99) < w0["stats"].get("mag", 12) * 0.75 and w0.get("reload", 0) == 0:
            out["reload"] = True
        return out

    # ---- aim + fire ----
    height = {"mech": 4.5, "npc": 1.75}.get(tkind, 0.5)
    tpos = [target["pos"][0], target["pos"][1], target["pos"][2] + height]
    tvel = target.get("vel", [0, 0, 0])
    dist = _d3(muzzle, tpos)
    cur_yaw, cur_pitch = me["aim"]
    radius = {"mech": 2.5, "npc": 1.8}.get(tkind, 1.0)
    tol = max(0.012, math.atan2(radius * 0.9, max(dist, 1.0)))

    plan = "gun"
    if w1:
        k = w1["kind"]
        if k == "lightning" and dist < w1["stats"].get("range", 45) - 2 and not t_blocked:
            plan = "lightning"
        elif k == "laser" and dist < w1["stats"].get("range", 280) and not t_blocked and tkind != "crate":
            plan = "laser"
        elif (k == "mortar" and 25 < dist < 220 and w1.get("cooldown", 0) <= 0
              and (t_blocked or _d2(tvel, (0, 0)) < 3)):
            plan = "mortar"
    if t_blocked and plan != "mortar":
        out["aim"] = list(_aim_at(muzzle, tpos))  # face them (front armor), hold fire
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

    speed = w0["stats"].get("speed", 252) if gun_ok else 252
    gun_pt = _lead(muzzle, tpos, tvel, speed)
    gun_yaw, gun_pitch = _aim_at(muzzle, gun_pt)
    direct_yaw, direct_pitch = _aim_at(muzzle, tpos)   # laser & lightning are instant: no lead
    yaw, pitch = (gun_yaw, gun_pitch) if plan == "gun" else (direct_yaw, direct_pitch)
    out["aim"] = [yaw, pitch]
    aligned = _angdiff(cur_yaw, yaw) < tol and abs(cur_pitch - pitch) < tol * 2
    gun_aligned = _angdiff(cur_yaw, gun_yaw) < tol and abs(cur_pitch - gun_pitch) < tol * 2

    charging = plan == "laser" and w1.get("charge", 0) > 0
    if gun_ok and dist < w0["stats"].get("range", 230) and gun_aligned and w0.get("ammo", 1) > 0 and not charging:
        out["fire"][0] = True

    if w1 and plan == "lightning":
        cone = math.radians(w1["stats"].get("cone_deg", 30)) / 2
        if _angdiff(cur_yaw, yaw) < cone * 0.85 and me["energy"] >= w1["stats"].get("energy", 3) + 6:
            out["fire"][1] = True
    elif w1 and plan == "laser" and w1.get("cooldown", 0) <= 0:
        charge = w1.get("charge", 0)
        full = max(0.01, w1["stats"].get("charge", 0.75))
        hold_limit = full + w1["stats"].get("hold_s", 2.0)
        escaping = memory.get("mode") in ("retreat", "surrounded", "recover", "zone", "dodge")
        if charge > 0:
            if charge >= full * 0.9:
                # release (False) when lined up, or just before it would auto-fire
                out["fire"][1] = not (aligned or charge >= hold_limit - 0.15)
            else:
                out["fire"][1] = True
        elif not escaping and my_hp > 0.5:
            out["fire"][1] = True

    return out


