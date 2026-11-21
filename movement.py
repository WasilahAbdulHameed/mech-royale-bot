"""Movement & survival (owner: Wasilah).

decide_move(obs, memory) -> dict with ONLY: move, boost, jump, take

Priority each tick:
  1. Get into / stay inside the safe zone
  2. Low HP or outnumbered -> run away (but not out of the zone)
  3. One enemy -> strafe side to side so we're harder to hit
  4. Safe -> grab cubes, weapons, supply drops
  5. Otherwise -> sneak slowly (silent) near the next zone's center
Plus: avoid NPC drones, and un-stick ourselves if we walk into a wall.
"""
import math
import random

# ---- tuning knobs (change these to experiment) ----
LOW_HP = 0.45          # below this fraction of max HP, we retreat
DANGER_RANGE = 90.0    # enemies closer than this count as a threat
LOOT_SAFE_RANGE = 120.0  # only loot if no enemy is closer than this
CUBE_RANGE = 50.0      # walk to visible cubes within this distance
PICKUP_RANGE = 60.0    # walk to weapon pickups within this distance
NPC_AVOID = 55.0       # keep this far from NPC drones
ZONE_EDGE_BUFFER = 25.0  # start moving inward when this close to the zone edge
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

    # ---- 1. Zone ----
    if not me["in_zone"] or margin < ZONE_EDGE_BUFFER:
        dx, dy = to_next
        speed, mode = 1.0, "zone"
        boost = (not me["in_zone"] or margin < 10) and energy > 10

    # ---- 2. Retreat ----
    elif threats and (hp_frac < LOW_HP or len(threats) >= 2):
        ax = sum(x - m["pos"][0] for m in threats)
        ay = sum(y - m["pos"][1] for m in threats)
        ax, ay = _unit(ax, ay)
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
        # keep a medium distance: back off if too close, close in a bit if far
        k = -0.5 if d < 40 else (0.3 if d > 120 else 0.0)
        dx, dy = _unit(sx + bx * k + to_next[0] * (0.5 if far_from_next else 0), sy + by * k + to_next[1] * (0.5 if far_from_next else 0))
        speed, mode = 1.0, "strafe"

    # ---- 4. Loot ----
    else:
        target = None
        if hp_frac > 0.5 and nearest_enemy > LOOT_SAFE_RANGE:
            target, mode, out_take = _pick_loot(obs, me, x, y, reach)
            if out_take is not None:
                out["take"] = out_take
        if target is not None:
            dx, dy = _unit(target[0] - x, target[1] - y)
            speed = 1.0
            if _dist2d(target, (x, y)) < 2.0:
                speed = 0.0  # standing on it (supply drop ring / pickup)
        elif far_from_next:
            dx, dy = to_next
            speed, mode = 1.0, "travel"
        else:
            # ---- 5. Hold: sneak around a spot inside the next circle ----
            hp_pt = memory.get("hold_pt")
            if hp_pt is None or _dist2d(hp_pt, (x, y)) < 5 or _dist2d(hp_pt, (ncx, ncy)) > next_r * 0.5:
                ang = random.uniform(-math.pi, math.pi)
                rad = random.uniform(0, next_r * 0.35)
                hp_pt = (ncx + rad * math.cos(ang), ncy + rad * math.sin(ang))
                memory["hold_pt"] = hp_pt
            dx, dy = _unit(hp_pt[0] - x, hp_pt[1] - y)
            speed, mode = SNEAK, "hold"

    # ---- NPC avoidance (they shoot anyone within ~45 m of their home) ----
    if mode not in ("zone",):
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
        hist.clear()

    if dx or dy:
        memory["move_yaw"] = math.atan2(dy, dx)
    memory["mode"] = mode

    out.update(move=[dx * speed, dy * speed], boost=bool(boost), jump=bool(jump))
    return out


def _pick_loot(obs, me, x, y, reach):
    """Returns (target_point or None, mode, take_id or None)."""
    held = {w["kind"] for w in me["weapons"] if w}
    slot1_empty = me["weapons"][1] is None if len(me["weapons"]) > 1 else False

    # Weapons: only if we have a free slot
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

    # Cubes: +4% HP and damage each
    cubes = sorted(obs.get("cubes", []), key=lambda c: _dist2d(c["pos"], (x, y)))
    if cubes and _dist2d(cubes[0]["pos"], (x, y)) < CUBE_RANGE and abs(cubes[0]["pos"][2] - me["pos"][2]) < 15:
        return cubes[0]["pos"], "loot-cube", None

    # Supply drop: full HP + 3 cubes, only if close and nobody else is on it
    for dr in obs.get("drops", []):
        d = _dist2d(dr["pos"], (x, y))
        if d < 100 and dr.get("lands_in", 99) < 8 and dr.get("holder") in (None, me["id"]):
            return dr["pos"], "supply-drop", None

    return None, "hold", None
