"""Movement & survival v4 (owner: Wasilah).

decide_move(obs, memory) -> dict with ONLY: move, boost, jump, take

The loop that wins: FIGHT a hunter -> when hurt, BREAK LINE OF SIGHT behind a building
-> RECOVER (HP regenerates 45/s after 5 s without damage) -> fight the next one.

Modes, in priority order each tick:
  zone     : outside / near the edge of the safe zone -> go in
  dodge    : a bullet, mortar shell or charged laser is about to hit us -> sidestep
  retreat  : low HP or swarmed -> run to a spot with a building between us and them
  fight    : enemies in view, HP ok -> strafe unpredictably at a good distance
  recover  : hurt, nobody in view -> sit still behind cover until healed
  loot     : safe -> weapons, cubes, supply drops
  hold     : park with our back to a wall inside the next circle (silent)
"""
import math
import random

# ---- tuning knobs ----
RETREAT_HP = 0.40      # retreat below this HP fraction...
RECOVER_TO = 0.85      # ...and don't go looking for trouble until healed to this
SWARM = 3              # this many enemies within DANGER_RANGE -> retreat even if healthy
DANGER_RANGE = 90.0
FIGHT_MIN, FIGHT_MAX = 35.0, 90.0   # preferred fight distance (gun)
CLOSE_MIN, CLOSE_MAX = 15.0, 35.0   # preferred fight distance when we hold lightning
LOOT_SAFE_RANGE = 120.0
CUBE_RANGE = 60.0
PICKUP_RANGE = 70.0
NPC_AVOID = 55.0
ZONE_EDGE_BUFFER = 25.0
HOLD_FRACTION = 0.5
SNEAK = 0.4            # below 5 m/s = no footstep sound
WEAPON_PREF = {"laser": 3, "lightning": 3, "mortar": 1}


def _dist2d(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _unit(x, y):
    n = math.hypot(x, y)
    return (x / n, y / n) if n > 1e-6 else (0.0, 0.0)


def _rotate(x, y, ang):
    c, s = math.cos(ang), math.sin(ang)
    return (x * c - y * s, x * s + y * c)


def _angdiff(a, b):
    return abs((a - b + math.pi) % (2 * math.pi) - math.pi)


def decide_move(obs, memory):
    me = obs["self"]
    x, y, z = me["pos"]
    t = obs["t"]
    zone = obs["zone"]
    hp_frac = me["hp"] / max(1.0, me["max_hp"])
    energy = me["energy"]
    reach = memory.get("reach", 4.0)
    gmap = memory.get("map")
    if gmap is not None and not gmap.ok:
        gmap = None
    out = {}

    enemies = [m for m in obs["mechs"] if m["kind"] == "mech"]
    npcs = [m for m in obs["mechs"] if m["kind"] == "npc"]
    enemy_d = sorted(((_dist2d(m["pos"], (x, y)), m) for m in enemies), key=lambda p: p[0])
    nearest_enemy = enemy_d[0][0] if enemy_d else 1e9
    threats = [m for d, m in enemy_d if d < DANGER_RANGE]
    if enemies:
        memory["last_enemy_t"] = t

    for ev in obs.get("events", []):
        if ev.get("type") == "damaged" and ev.get("bearing") is not None:
            memory["hit_t"] = t
            memory["hit_bearing"] = ev["bearing"]
    recently_hit = t - memory.get("hit_t", -99) < 2.0

    # "wounded" sticks until we've healed back up (hysteresis, so we don't flip-flop)
    if hp_frac < RETREAT_HP:
        memory["wounded"] = True
    elif hp_frac >= RECOVER_TO:
        memory["wounded"] = False
    wounded = memory.get("wounded", False)

    ncx, ncy = zone["next_center"]
    next_r = zone["next_radius"]
    ccx, ccy = zone["center"]
    margin = zone["radius"] - _dist2d((x, y), (ccx, ccy))
    to_next = _unit(ncx - x, ncy - y)
    far_from_next = _dist2d((x, y), (ncx, ncy)) > next_r * 0.6
    has_lightning = any(w and w["kind"] == "lightning" for w in me["weapons"])

    dx, dy, speed, boost, mode = 0.0, 0.0, SNEAK, False, "hold"
    face, goal = None, None
    dodge = _dodge_dir(obs, me, x, y, z)

    # ---- 1. Zone ----
    if not me["in_zone"] or margin < ZONE_EDGE_BUFFER:
        goal = (ncx, ncy)
        speed, mode = 1.0, "zone"
        boost = (not me["in_zone"] or margin < 10) and energy > 10

    # ---- 2. Dodge ----
    elif dodge is not None:
        dx, dy = dodge
        speed, mode = 1.0, "dodge"
        boost = energy > 20

    # ---- 3. Retreat behind a building ----
    elif threats and (wounded or len(threats) >= SWARM):
        tx = sum(m["pos"][0] for m in threats) / len(threats)
        ty = sum(m["pos"][1] for m in threats) / len(threats)
        spot = _retreat_spot(gmap, memory, t, x, y, (tx, ty), zone)
        if spot is not None:
            goal = spot[:2]
        else:
            ax, ay = _unit(x - tx, y - ty)
            pull = 1.5 if far_from_next else 0.4
            dx, dy = _unit(ax + to_next[0] * pull, ay + to_next[1] * pull)
        speed, mode = 1.0, "retreat"
        boost = energy > 25

    # ---- 4. Fight: strafe with random timing at a good distance ----
    elif enemies or recently_hit:
        if enemies:
            m = enemy_d[0][1]
            bx, by = _unit(m["pos"][0] - x, m["pos"][1] - y)
            d = enemy_d[0][0]
        else:
            b = memory["hit_bearing"]
            bx, by, d = math.cos(b), math.sin(b), 60.0
        if t >= memory.get("strafe_until", -1):
            memory["strafe_side"] = -memory.get("strafe_side", 1)
            memory["strafe_until"] = t + random.uniform(0.6, 1.6)  # unpredictable -> hard to lead
        side = memory.get("strafe_side", 1)
        sx, sy = -by * side, bx * side
        lo, hi = (CLOSE_MIN, CLOSE_MAX) if has_lightning else (FIGHT_MIN, FIGHT_MAX)
        k = -0.7 if d < lo else (0.6 if d > hi else 0.0)
        zp = 0.6 if far_from_next else 0.0
        dx, dy = _unit(sx + bx * k + to_next[0] * zp, sy + by * k + to_next[1] * zp)
        speed, mode = 1.0, "fight"

    # ---- 5. Recover: still and hidden until healed ----
    elif wounded:
        spot = _hold_spot(gmap, memory, x, y, ncx, ncy, next_r, near_only=True)
        goal, face = spot[:2], spot[2]
        d = _dist2d(goal, (x, y))
        speed, mode = (0.8 if d > 2.5 else 0.0), "recover"

    # ---- 6. Loot / 7. Hold ----
    else:
        target = None
        quiet = t - memory.get("last_enemy_t", -99) > 4.0
        if quiet and nearest_enemy > LOOT_SAFE_RANGE:
            target, mode, take = _pick_loot(obs, me, x, y, reach)
            if take is not None:
                out["take"] = take
        if target is not None:
            goal, speed = target[:2], 1.0
            if mode == "supply-drop" and _dist2d(goal, (x, y)) < 3.0:
                speed = 0.0  # stand inside the ring until captured
        elif far_from_next:
            goal, speed, mode = (ncx, ncy), 1.0, "travel"
        else:
            spot = _hold_spot(gmap, memory, x, y, ncx, ncy, next_r)
            goal, face = spot[:2], spot[2]
            d = _dist2d(goal, (x, y))
            speed, mode = (0.8 if d > 30 else (0.35 if d > 2.5 else 0.0)), "hold"

    if goal is not None:
        dx, dy = _unit(goal[0] - x, goal[1] - y)
        if gmap is not None and _dist2d(goal, (x, y)) > 6:
            dx, dy = _steer(gmap, x, y, dx, dy)

    # ---- avoid NPC drones (except when the zone forces us) ----
    if mode not in ("zone", "dodge"):
        for n in npcs:
            d = _dist2d(n["pos"], (x, y))
            if d < NPC_AVOID:
                px, py = _unit(x - n["pos"][0], y - n["pos"][1])
                w = (NPC_AVOID - d) / NPC_AVOID * 1.5
                dx, dy = _unit(dx + px * w, dy + py * w)
                speed = max(speed, 0.8)

    # ---- stuck detection ----
    hist = memory.setdefault("pos_hist", [])
    hist.append((x, y))
    if len(hist) > 10:
        hist.pop(0)
    jump = False
    if t < memory.get("unstick_until", -1):
        dx, dy = _rotate(dx, dy, memory["unstick_ang"])
        speed, jump, boost = 1.0, me["grounded"], energy > 15
        mode += "+unstick"
    elif speed > 0.3 and len(hist) == 10 and _dist2d(hist[0], hist[-1]) < 1.5:
        memory["unstick_until"] = t + 1.2
        memory["unstick_ang"] = random.choice([math.pi / 2, -math.pi / 2, math.pi * 0.75])
        memory.pop("hold_pt", None)
        hist.clear()

    if speed == 0.0 and face is not None:
        memory["move_yaw"] = face
    elif dx or dy:
        memory["move_yaw"] = math.atan2(dy, dx)
    memory["mode"] = mode

    out.update(move=[dx * speed, dy * speed], boost=bool(boost), jump=bool(jump))
    return out


# ---------------- helpers ----------------

def _dodge_dir(obs, me, x, y, z):
    """Direction to sidestep if something is about to hit us, else None."""
    cz = z + 4.5  # our center of mass
    best = None
    for p in obs.get("projectiles", []):
        px, py, pz = p["pos"]
        vx, vy, vz = p["vel"]
        if p["kind"] == "mortar":
            # where will the shell come down (roughly at our height)?
            g = 22.0
            a, b, c = -0.5 * g, vz, pz - z
            disc = b * b - 4 * a * c
            if disc < 0:
                continue
            tt = (-b - math.sqrt(disc)) / (2 * a)
            if not 0 < tt < 2.5:
                continue
            lx, ly = px + vx * tt, py + vy * tt
            if math.hypot(lx - x, ly - y) < 10:
                return _unit(x - lx, y - ly)  # run away from the landing point
            continue
        rx, ry, rz = x - px, y - py, cz - pz
        v2 = vx * vx + vy * vy + vz * vz
        if v2 < 1:
            continue
        tca = (rx * vx + ry * vy + rz * vz) / v2  # time of closest approach
        if not 0 < tca < 0.6:
            continue
        mx, my = px + vx * tca - x, py + vy * tca - y
        if math.hypot(mx, my) < 4.0:
            # step sideways, away from where it will pass
            sx, sy = _unit(-vy, vx)
            if sx * mx + sy * my > 0:
                sx, sy = -sx, -sy
            best = (sx, sy)
    if best is not None:
        return best
    # a laser charging and pointed at us
    for m in obs["mechs"]:
        if m["kind"] == "mech" and m.get("charging"):
            bearing_to_me = math.atan2(y - m["pos"][1], x - m["pos"][0])
            if _angdiff(m["aim"][0], bearing_to_me) < 0.12 and _dist2d(m["pos"], (x, y)) < 280:
                bx, by = _unit(x - m["pos"][0], y - m["pos"][1])
                side = 1 if int(obs["t"] * 2) % 2 else -1
                return (-by * side, bx * side)
    return None


def _steer(gmap, x, y, dx, dy, probe=15.0):
    """If any wall (even a short one) is straight ahead, turn left/right until the way is clear."""
    if not gmap.blocked((x, y), (x + dx * probe, y + dy * probe), step=4.0, walk=True):
        return dx, dy
    for deg in (25, -25, 50, -50, 75, -75, 100, -100, 130, -130):
        rx, ry = _rotate(dx, dy, math.radians(deg))
        if not gmap.blocked((x, y), (x + rx * probe, y + ry * probe), step=4.0, walk=True):
            return rx, ry
    return dx, dy


def _hold_spot(gmap, memory, x, y, ncx, ncy, next_r, near_only=False):
    """Parking spot inside the next circle, next to a wall when we have the map."""
    spot = memory.get("hold_pt")
    limit = next_r * HOLD_FRACTION
    if spot is not None and _dist2d(spot, (ncx, ncy)) <= limit and (not near_only or _dist2d(spot, (x, y)) < 60):
        return spot
    spot = None
    if gmap is not None:
        best = 1e9
        centers = ((x, y),) if near_only else ((x, y), (ncx, ncy))
        for cx, cy in centers:
            for d, p in gmap.cover_near(cx, cy, 60.0 if near_only else 150.0)[:400]:
                dc = _dist2d(p, (ncx, ncy))
                if dc > limit and not near_only:
                    continue
                score = _dist2d(p, (x, y)) + 0.3 * dc
                if score < best:
                    best, spot = score, p
            if spot is not None:
                break
    if spot is None:
        if near_only:
            spot = (x, y, memory.get("move_yaw", 0.0))
        else:
            ang = random.uniform(-math.pi, math.pi)
            rad = random.uniform(0, next_r * 0.35)
            spot = (ncx + rad * math.cos(ang), ncy + rad * math.sin(ang), math.atan2(ncy - y, ncx - x))
    memory["hold_pt"] = spot
    return spot


def _retreat_spot(gmap, memory, t, x, y, threat, zone):
    if gmap is None:
        return None
    cached = memory.get("retreat")
    if cached and t - cached[0] < 1.0:
        return cached[1]
    ccx, ccy = zone["center"]
    best, checked = None, 0
    for d, p in gmap.cover_near(x, y, 80.0):
        if _dist2d(p, (ccx, ccy)) > zone["radius"] - 15:
            continue
        if _dist2d(p, threat) < _dist2d((x, y), threat) - 10:
            continue
        checked += 1
        if gmap.blocked(threat, p, step=5.0):
            best = p
            break
        if checked >= 15:
            break
    memory["retreat"] = (t, best)
    if best is not None:
        memory["hold_pt"] = best  # recover right there afterwards
    return best


def _pick_loot(obs, me, x, y, reach):
    held = {w["kind"] for w in me["weapons"] if w}
    slot1_empty = me["weapons"][1] is None if len(me["weapons"]) > 1 else False

    if slot1_empty:
        best, best_score = None, -1
        for p in obs.get("pickups", []):
            if p["kind"] in held or p["kind"] not in WEAPON_PREF:
                continue
            d = _dist2d(p["pos"], (x, y))
            if d > PICKUP_RANGE or abs(p["pos"][2] - me["pos"][2]) > 15:
                continue
            score = WEAPON_PREF[p["kind"]] * 100 - d
            if score > best_score:
                best, best_score = p, score
        if best is not None:
            d = _dist2d(best["pos"], (x, y))
            take = best["id"] if d < reach and abs(best["pos"][2] - me["pos"][2]) < 4 else None
            return best["pos"], "loot-weapon", take

    cubes = sorted(obs.get("cubes", []), key=lambda c: _dist2d(c["pos"], (x, y)))
    if cubes and _dist2d(cubes[0]["pos"], (x, y)) < CUBE_RANGE and abs(cubes[0]["pos"][2] - me["pos"][2]) < 15:
        return cubes[0]["pos"], "loot-cube", None

    for dr in obs.get("drops", []):
        d = _dist2d(dr["pos"], (x, y))
        arrive = d / 11.0  # seconds to walk there
        if d < 220 and dr.get("lands_in", 99) <= arrive + 4 and dr.get("holder") in (None, me["id"]):
            return dr["pos"], "supply-drop", None

    # crates: walk up to nearby ones so combat can break them for cubes/weapons
    crates = sorted(obs.get("crates", []), key=lambda c: _dist2d(c["pos"], (x, y)))
    if crates and 12 < _dist2d(crates[0]["pos"], (x, y)) < 60 and abs(crates[0]["pos"][2] - me["pos"][2]) < 6:
        return crates[0]["pos"], "loot-crate", None

    return None, "hold", None
