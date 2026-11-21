"""Survival-first Mech Royale combat. Only returns aim, fire, reload.

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

    # Prefer fighting back, or finishing a nearby weakened enemy.
    target = None
    kind = None
    best_score = float("inf")
    for enemy in obs.get("mechs", []):
        distance = _d3(pos, enemy["pos"])
        is_attacker = str(enemy["id"]) in attackers
        if enemy.get("kind") == "npc":
            if not is_attacker or distance > 65:
                continue
            score = distance + 30
        else:
            hp_ratio = enemy.get("hp", 1400) / max(1, enemy.get("max_hp", 1400))
            # At low health, preserve regeneration by avoiding new fights.
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
            target, kind, best_score = enemy, enemy.get("kind"), score

    # Crates grant useful cubes/weapon loot, but avoid shooting them during danger.
    if target is None and health_ratio > 0.60 and not attackers:
        nearby = [c for c in obs.get("crates", [])
                  if _d3(pos, c["pos"]) < CRATE_RANGE]
        if nearby:
            crate = min(nearby, key=lambda c: _d3(pos, c["pos"]))
            target = {"pos": crate["pos"], "vel": [0, 0, 0]}
            kind = "crate"

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

    # Special weapon, if useful. Do not start a charge that slows our escape
    # when health is low; existing charges are released below.
    plan = "gun"
    if special:
        name = special.get("kind")
        s = special.get("stats", {})
        if name == "lightning" and distance < s.get("range", 45) - 3:
            plan = "lightning"
        elif name == "laser" and distance < s.get("range", 280):
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
        if aligned and distance < gun.get("stats", {}).get("range", 230):
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
