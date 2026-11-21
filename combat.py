"""Mech Royale v7 combat: improved gun leading and disciplined laser timing.

Only returns aim, fire, reload.

Compatible with bot.py merging combat actions into movement actions.
Movement/retreat must be handled by movement.py; this module does not move.
"""
import math

ENGAGE_RANGE = 80.0       # avoid provoking distant mechs
THREAT_RANGE = 180.0      # defend against recent attackers farther away
CRATE_RANGE = 45.0


def _d3(a, b):
    return math.dist(a, b)


def _d2(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _angdiff(a, b):
    return abs((a - b + math.pi) % (2 * math.pi) - math.pi)


def _aim_at(src, dst):
    return (math.atan2(dst[1] - src[1], dst[0] - src[0]),
            math.atan2(dst[2] - src[2], _d2(src, dst)))


def _lead(src, tpos, tvel, speed):
    p = list(tpos)
    for _ in range(3):
        seconds = min(1.5, _d3(src, p) / max(speed, 1.0))
        p = [tpos[i] + tvel[i] * seconds for i in range(3)]
    return p


def _mortar_pitch(distance, dz, speed, gravity):
    speed2 = speed * speed
    disc = speed2 * speed2 - gravity * (gravity * distance * distance + 2 * dz * speed2)
    if disc < 0 or distance < 1 or gravity <= 0:
        return None
    return math.atan((speed2 - math.sqrt(disc)) / (gravity * distance))


def decide_combat(obs, memory):
    me = obs["self"]
    pos = me["pos"]
    now = obs["t"]
    stats = me.get("stats", {})
    muzzle = [pos[0], pos[1], pos[2] + stats.get("muzzle_height", 7.0)]
    weapons = me.get("weapons", [])
    gun = weapons[0] if len(weapons) > 0 else None
    special = weapons[1] if len(weapons) > 1 else None
    out = {"fire": [False, False]}
    health_ratio = me.get("hp", 1400) / max(1, me.get("max_hp", 1400))

    # Record recent attackers, and face the direction shots came from.
    attackers = memory.setdefault("combat_attackers", {})
    for ev in obs.get("events", []):
        if ev.get("type") == "damaged":
            if ev.get("bearing") is not None:
                memory["combat_hit_bearing"] = ev["bearing"]
                memory["combat_hit_time"] = now
            if ev.get("by") is not None:
                attackers[str(ev["by"])] = now
    for enemy_id, when in list(attackers.items()):
        if now - when > 6.0:
            del attackers[enemy_id]

    # 1. Enemy mechs always have priority over farming targets.
    target = None
    kind = None
    best_score = float("inf")
    for enemy in obs.get("mechs", []):
        if enemy.get("kind") != "mech":
            continue
        distance = _d3(pos, enemy["pos"])
        is_attacker = str(enemy["id"]) in attackers
        hp_ratio = enemy.get("hp", 1400) / max(1, enemy.get("max_hp", 1400))
        can_finish = hp_ratio < 0.18 and distance < 65
        if is_attacker:
            if distance > THREAT_RANGE:
                continue
        elif (health_ratio < 0.45 and not can_finish) or distance > ENGAGE_RANGE:
            continue
        score = distance + 50 * hp_ratio - (85 if is_attacker else 0)
        if can_finish:
            score -= 30
        if score < best_score:
            target, kind, best_score = enemy, "mech", score

    # 2. Farm the NPC or crate selected by movement.py, if still visible.
    # IDs can be integers or strings; normalize to avoid a mismatch.
    if target is None:
        farm_id = memory.get("farm_target")
        if farm_id is not None:
            for npc in obs.get("mechs", []):
                if npc.get("kind") == "npc" and str(npc.get("id")) == str(farm_id):
                    target, kind = npc, "npc"
                    break
            if target is None:
                for crate in obs.get("crates", []):
                    if str(crate.get("id")) == str(farm_id):
                        target, kind = crate, "crate"
                        break

    # 3. If not farming, defend against an NPC that recently damaged us.
    if target is None:
        nearby_attackers = [npc for npc in obs.get("mechs", [])
                            if npc.get("kind") == "npc"
                            and str(npc.get("id")) in attackers
                            and _d3(pos, npc["pos"]) <= 65]
        if nearby_attackers:
            target = min(nearby_attackers, key=lambda npc: _d3(pos, npc["pos"]))
            kind = "npc"

    if target is None:
        # Taking damage from behind is especially costly; protect front armor.
        if now - memory.get("combat_hit_time", -1e9) < 4:
            out["aim"] = [memory["combat_hit_bearing"], 0.0]
        elif "move_yaw" in memory:
            out["aim"] = [memory["move_yaw"], 0.0]
        if (gun and gun.get("kind") == "gun" and gun.get("reload", 0) <= 0
                and gun.get("ammo", 99) < gun.get("stats", {}).get("mag", 12) * 0.75):
            out["reload"] = True
        return out

    height = 4.5 if kind == "mech" else (1.75 if kind == "npc" else 0.5)
    target_pos = [target["pos"][0], target["pos"][1], target["pos"][2] + height]
    velocity = target.get("vel", [0, 0, 0])
    distance = _d3(muzzle, target_pos)
    current_yaw, current_pitch = me["aim"]
    # A smaller tolerance at midrange avoids shots that miss a strafing mech.
    tolerance = max(0.018, min(0.06, math.atan2(1.6, max(distance, 1.0))))

    # Buildings block direct gun, laser and lightning shots. The map's blocked()
    # method accepts horizontal (x, y) points, as used by movement.py.
    # Mortars are indirect and can sometimes clear buildings, so do not
    # suppress a mortar just because the ground-level line is blocked.
    gmap = memory.get("map")
    blocked = False
    if gmap is not None and getattr(gmap, "ok", False):
        try:
            blocked = bool(gmap.blocked(pos[:2], target["pos"][:2], step=5.0))
        except (AttributeError, TypeError, ValueError):
            blocked = False

    # Special weapon, if useful. Do not start a charge that slows our escape
    # when health is low; existing charges are released below.
    plan = "gun"
    if special:
        name = special.get("kind")
        s = special.get("stats", {})
        if name == "lightning" and not blocked and distance < s.get("range", 45) - 3:
            plan = "lightning"
        elif name == "laser" and not blocked and distance < s.get("range", 280):
            if health_ratio > 0.5 or special.get("charge", 0) > 0:
                plan = "laser"
        elif name == "mortar" and 32 < distance < 190 and special.get("cooldown", 0) <= 0:
            plan = "mortar"

    if plan == "mortar":
        s = special.get("stats", {})
        speed, gravity = s.get("speed", 72), s.get("gravity", 22)
        tube = [pos[0], pos[1], pos[2] + stats.get("height", 9) + 0.5]
        predicted = _lead(tube, target_pos, velocity, speed * 0.7)
        pitch = _mortar_pitch(_d2(tube, predicted), predicted[2] - tube[2], speed, gravity)
        if pitch is not None:
            yaw = math.atan2(predicted[1] - tube[1], predicted[0] - tube[0])
            out["aim"] = [yaw, pitch]
            if _angdiff(current_yaw, yaw) < 0.04 and abs(current_pitch - pitch) < 0.035:
                out["fire"][1] = True
            return out
        plan = "gun"

    gun_speed = gun.get("stats", {}).get("speed", 252) if gun else 252
    gun_predicted = _lead(muzzle, target_pos, velocity, gun_speed)
    gun_yaw, gun_pitch = _aim_at(muzzle, gun_predicted)

    # Hitscan laser and lightning use the current target position rather than
    # the gun's projectile lead. Track their aim separately from gun aim.
    direct_yaw, direct_pitch = _aim_at(muzzle, target_pos)
    if plan == "gun":
        yaw, pitch = gun_yaw, gun_pitch
    else:
        yaw, pitch = direct_yaw, direct_pitch
    out["aim"] = [yaw, pitch]
    aligned = (_angdiff(current_yaw, yaw) < tolerance
               and abs(current_pitch - pitch) < tolerance * 1.5)

    if gun and gun.get("kind") == "gun":
        if gun.get("ammo", 0) == 0 and gun.get("reload", 0) <= 0:
            out["reload"] = True
        # Gun must agree with actual aim, even while laser/lightning controls
        # the torso. Previously it could fire while aimed at the wrong lead.
        gun_aligned = (_angdiff(current_yaw, gun_yaw) < tolerance
                       and abs(current_pitch - gun_pitch) < tolerance * 1.5)
        if gun_aligned and not blocked and distance < gun.get("stats", {}).get("range", 230):
            out["fire"][0] = True

    if special and plan == "lightning":
        s = special.get("stats", {})
        cone = math.radians(s.get("cone_deg", 30)) / 2
        if (_angdiff(current_yaw, yaw) < cone * 0.8
                and me.get("energy", 0) >= s.get("energy", 3) + 8):
            out["fire"][1] = True
    elif special and plan == "laser" and special.get("cooldown", 0) <= 0:
        charge = special.get("charge", 0)
        s = special.get("stats", {})
        full = max(0.01, s.get("charge", 0.75))
        hold_limit = full + s.get("hold_s", 2.0)
        # Charging slows movement: do not begin a new charge while escaping,
        # healing, or fighting with critically low HP.
        escaping = memory.get("mode") in ("retreat", "recover", "zone", "dodge")
        can_start = (not escaping and health_ratio > 0.5 and not blocked)
        if charge > 0:
            # Release only once aligned; don't hold so long that the laser
            # auto-fires at an unwanted angle. The engine fires on release.
            out["fire"][1] = not (aligned or charge >= hold_limit - 0.15) if charge >= full * 0.95 else True
        elif can_start:
            out["fire"][1] = True

    return out
