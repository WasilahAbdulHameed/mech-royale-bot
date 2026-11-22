"""Mech Royale v9 combat: focused targets, line-of-sight-aware selection.

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

    # 1. Focus a viable enemy instead of retargeting every tenth of a second.
    # The previous implementation sometimes picked the nearest mech BEHIND a
    # wall, dropped it and ignored a second mech that could actually be hit.
    # With coordinated enemies that wastes many possible shots.
    gmap = memory.get("map")
    can_check_cover = gmap is not None and getattr(gmap, "ok", False)
    previous = memory.get("combat_focus_id")
    target = None
    kind = None
    best_score = float("inf")
    special_kind = special.get("kind") if special else None
    for enemy in obs.get("mechs", []):
        if enemy.get("kind") != "mech":
            continue
        distance = _d3(pos, enemy["pos"])
        is_attacker = str(enemy["id"]) in attackers
        hp_ratio = enemy.get("hp", 1400) / max(1, enemy.get("max_hp", 1400))
        can_finish = hp_ratio < 0.22 and distance < 70
        if is_attacker:
            if distance > THREAT_RANGE:
                continue
        elif (health_ratio < 0.45 and not can_finish) or distance > ENGAGE_RANGE:
            continue

        blocked = False
        if can_check_cover:
            try:
                blocked = bool(gmap.blocked(pos[:2], enemy["pos"][:2], step=5.0))
            except (AttributeError, TypeError, ValueError):
                pass
        # Mortars can lob over cover; otherwise don't fixate on hidden targets.
        if blocked and not is_attacker and special_kind != "mortar":
            continue

        # The aim/finish bonuses prevent target hopping between two similar
        # enemies; finishing one enemy removes a gun from the opposing squad.
        score = distance + 75 * hp_ratio
        score -= 35 if is_attacker else 0
        score -= 95 if can_finish else 0
        score -= 48 if str(enemy["id"]) == previous else 0
        score += 170 if blocked else 0
        if score < best_score:
            target, kind, best_score = enemy, "mech", score

    if target is not None:
        memory["combat_focus_id"] = str(target["id"])
    else:
        memory.pop("combat_focus_id", None)

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
    tolerance = max(0.025, math.atan2(2.0, max(distance, 1.0)))

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
    predicted = _lead(muzzle, target_pos, velocity, gun_speed)
    aim_point = predicted if plan == "gun" else target_pos
    yaw, pitch = _aim_at(muzzle, aim_point)
    out["aim"] = [yaw, pitch]
    aligned = (_angdiff(current_yaw, yaw) < tolerance
               and abs(current_pitch - pitch) < tolerance * 1.5)

    if gun and gun.get("kind") == "gun":
        if gun.get("ammo", 0) == 0 and gun.get("reload", 0) <= 0:
            out["reload"] = True
        if aligned and not blocked and distance < gun.get("stats", {}).get("range", 230):
            out["fire"][0] = True

    if special and plan == "lightning":
        s = special.get("stats", {})
        cone = math.radians(s.get("cone_deg", 30)) / 2
        if (_angdiff(current_yaw, yaw) < cone * 0.8
                and me.get("energy", 0) >= s.get("energy", 3) + 8):
            out["fire"][1] = True
    elif special and plan == "laser" and special.get("cooldown", 0) <= 0:
        charge = special.get("charge", 0)
        full = special.get("stats", {}).get("charge", 0.75)
        if charge >= full * 0.95:
            # False releases an existing charge, even if not perfectly aligned.
            out["fire"][1] = False
        else:
            out["fire"][1] = True

    return out
