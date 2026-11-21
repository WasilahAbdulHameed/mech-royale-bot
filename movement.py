"""Movement & survival v2 (owner: Wasilah).

decide_move(obs, memory) -> dict with ONLY: move, boost, jump, take

Priority each tick:
  1. Get into / stay inside the safe zone
  2. Low HP or outnumbered -> run to a spot BEHIND a building (away from enemies)
  3. One enemy -> strafe side to side so we're harder to hit
  4. Safe -> grab cubes, weapons, supply drops
  5. Otherwise -> park with our back to a wall inside the next circle, standing still (silent)
Plus: steer around buildings, avoid NPC drones, and un-stick ourselves on walls.
v2 = uses the map (mapdata.py). If the map fails to load, it behaves like v1.
"""
import math
import random

# ---- tuning knobs (change these to experiment) ----
LOW_HP = 0.5           # below this fraction of max HP, we retreat
DANGER_RANGE = 90.0    # enemies closer than this count as a threat
LOOT_SAFE_RANGE = 120.0  # only loot if no enemy is closer than this
CUBE_RANGE = 50.0      # walk to visible cubes within this distance
PICKUP_RANGE = 60.0    # walk to weapon pickups within this distance
NPC_AVOID = 55.0       # keep this far from NPC drones
ZONE_EDGE_BUFFER = 25.0  # start moving inward when this close to the zone edge
HOLD_FRACTION = 0.5    # park within this fraction of the next circle's radius
SNEAK = 0.4            # walk speed fraction when idle (below 5 m/s = no footstep sound)
WEAPON_PREF = {"laser": 3, "lightning": 2, "mortar": 1}  # which loot we want most


def _dist2d(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _unit(x, y):
    n = math.hypot(x, y)
    return (x / n, y / n) if n > 1e-6 else (0.0, 0.0)


def _rotate(x, y, ang):
    c, s = math.cos(ang), math.sin(ang)
    return (x * c - y * s, x * s + y * c)


def decide_move(obs, memory):
    me = obs["self"]
    x, y, _ = me["pos"]
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

    # Remember when/where we got hit (combat uses this to turn and face the attacker)
    for ev in obs.get("events", []):
        if ev.get("type") == "damaged" and ev.get("bearing") is not None:
            memory["hit_t"] = t
            memory["hit_bearing"] = ev["bearing"]
    recently_hit = t - memory.get("hit_t", -99) < 2.0

    # Zone geometry
    ncx, ncy = zone["next_center"]
    next_r = zone["next_radius"]
    ccx, ccy = zone["center"]
    margin = zone["radius"] - _dist2d((x, y), (ccx, ccy))  # meters until the current edge
    to_next = _unit(ncx - x, ncy - y)
    far_from_next = _dist2d((x, y), (ncx, ncy)) > next_r * 0.6

    dx, dy, speed, boost, mode = 0.0, 0.0, SNEAK, False, "hold"
    face = None  # direction to look when parked
    goal = None  # a point we're walking to (gets steered around buildings)

    # ---- 1. Zone ----
    if not me["in_zone"] or margin < ZONE_EDGE_BUFFER:
        goal = (ncx, ncy)
        speed, mode = 1.0, "zone"
        boost = (not me["in_zone"] or margin < 10) and energy > 10

    # ---- 2. Retreat behind a building ----
    elif threats and (hp_frac < LOW_HP or len(threats) >= 2):
        tx = sum(m["pos"][0] for m in threats) / len(threats)
        ty = sum(m["pos"][1] for m in threats) / len(threats)
        spot = _retreat_spot(gmap, memory, t, x, y, (tx, ty), zone)
        if spot is not None:
            goal = spot[:2]
        else:
            ax, ay = _unit(x - tx, y - ty)
            pull = 1.5 if far_from_next else 0.4  # don't run out of the zone
            dx, dy = _unit(ax + to_next[0] * pull, ay + to_next[1] * pull)
        speed, mode = 1.0, "retreat"
        boost = energy > 25

    # ---- 3. Duel: strafe ----
    elif threats or recently_hit:
        if threats:
            m = threats[0]
            bx, by = _unit(m["pos"][0] - x, m["pos"][1] - y)
            d = _dist2d(m["pos"], (x, y))
        else:
            b = memory["hit_bearing"]
            bx, by, d = math.cos(b), math.sin(b), 60.0
        side = 1 if int(t / 1.5) % 2 == 0 else -1  # switch strafe direction every 1.5 s
        sx, sy = -by * side, bx * side
        k = -0.5 if d < 40 else (0.3 if d > 120 else 0.0)  # keep medium distance
        z = 0.5 if far_from_next else 0.0
        dx, dy = _unit(sx + bx * k + to_next[0] * z, sy + by * k + to_next[1] * z)
        speed, mode = 1.0, "strafe"

    # ---- 4. Loot / 5. Hold ----
    else:
        target = None
        if hp_frac > 0.5 and nearest_enemy > LOOT_SAFE_RANGE:
            target, mode, take = _pick_loot(obs, me, x, y, reach)
            if take is not None:
                out["take"] = take
        if target is not None:
            goal, speed = target[:2], 1.0
        elif far_from_next:
            goal, speed, mode = (ncx, ncy), 1.0, "travel"
        else:
            spot = _hold_spot(gmap, memory, x, y, ncx, ncy, next_r)
            goal = spot[:2]
            face = spot[2]
            mode = "hold"
            d = _dist2d(goal, (x, y))
            speed = SNEAK if d > 30 else (0.3 if d > 2.5 else 0.0)
            if d > 30 and nearest_enemy > 200:
                speed = 0.8  # nobody around: get there faster

    # Turn a goal point into a direction, steering around buildings
    if goal is not None:
        dx, dy = _unit(goal[0] - x, goal[1] - y)
        if gmap is not None and _dist2d(goal, (x, y)) > 6:
            dx, dy = _steer(gmap, x, y, dx, dy)

    # ---- NPC avoidance (they shoot anyone within ~45 m of their home) ----
    if mode != "zone":
        for n in npcs:
            d = _dist2d(n["pos"], (x, y))
            if d < NPC_AVOID:
                px, py = _unit(x - n["pos"][0], y - n["pos"][1])
                w = (NPC_AVOID - d) / NPC_AVOID * 1.5
                dx, dy = _unit(dx + px * w, dy + py * w)
                speed = max(speed, 0.8)

    # ---- Stuck detection: moving but not going anywhere -> sidestep + jump ----
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
        memory["unstick_until"] = t + 1.5
        memory["unstick_ang"] = random.choice([math.pi / 2, -math.pi / 2, math.pi * 0.75])
        memory.pop("hold_pt", None)  # maybe that spot is unreachable: pick another
        hist.clear()

    # What combat should face when it has no target
    if speed == 0.0 and face is not None:
        memory["move_yaw"] = face  # back to the wall, looking out
    elif dx or dy:
        memory["move_yaw"] = math.atan2(dy, dx)
    memory["mode"] = mode

    out.update(move=[dx * speed, dy * speed], boost=bool(boost), jump=bool(jump))
    return out


# ---------------- helpers ----------------

def _steer(gmap, x, y, dx, dy, probe=15.0):
    """If a building is straight ahead, try turning left/right until the way is clear."""
    if not gmap.blocked((x, y), (x + dx * probe, y + dy * probe), step=5.0):
        return dx, dy
    for deg in (30, -30, 60, -60, 90, -90, 120, -120):
        rx, ry = _rotate(dx, dy, math.radians(deg))
        if not gmap.blocked((x, y), (x + rx * probe, y + ry * probe), step=5.0):
            return rx, ry
    return dx, dy


def _hold_spot(gmap, memory, x, y, ncx, ncy, next_r):
    """A parking spot inside the next circle. With the map: next to a wall. Cached until invalid."""
    spot = memory.get("hold_pt")
    limit = next_r * HOLD_FRACTION
    if spot is not None and _dist2d(spot, (ncx, ncy)) <= limit:
        return spot
    spot = None
    if gmap is not None:
        best = 1e9
        # search near us first, then near the zone center
        for cx, cy in ((x, y), (ncx, ncy)):
            for d, p in gmap.cover_near(cx, cy, 150.0)[:400]:
                dc = _dist2d(p, (ncx, ncy))
                if dc > limit:
                    continue
                score = _dist2d(p, (x, y)) + 0.3 * dc
                if score < best:
                    best, spot = score, p
            if spot is not None:
                break
    if spot is None:  # no map / no wall nearby: random spot near the center
        ang = random.uniform(-math.pi, math.pi)
        rad = random.uniform(0, next_r * 0.35)
        spot = (ncx + rad * math.cos(ang), ncy + rad * math.sin(ang), math.atan2(ncy - y, ncx - x))
    memory["hold_pt"] = spot
    return spot


def _retreat_spot(gmap, memory, t, x, y, threat, zone):
    """A cover spot within 80 m that has a building between it and the threat. Recomputed once a second."""
    if gmap is None:
        return None
    cached = memory.get("retreat")
    if cached and t - cached[0] < 1.0:
        return cached[1]
    ccx, ccy = zone["center"]
    best = None
    checked = 0
    for d, p in gmap.cover_near(x, y, 80.0):
        if _dist2d(p, (ccx, ccy)) > zone["radius"] - 15:
            continue  # don't hide outside the zone
        if _dist2d(p, threat) < _dist2d((x, y), threat) - 10:
            continue  # don't run toward them
        checked += 1
        if gmap.blocked(threat, p, step=5.0):
            best = p
            break  # list is sorted by distance: first hit is the closest
        if checked >= 15:
            break
    memory["retreat"] = (t, best)
    return best


def _pick_loot(obs, me, x, y, reach):
    """Returns (target_point or None, mode, take_id or None)."""
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
        if d < 100 and dr.get("lands_in", 99) < 8 and dr.get("holder") in (None, me["id"]):
            return dr["pos"], "supply-drop", None

    return None, "hold", None
