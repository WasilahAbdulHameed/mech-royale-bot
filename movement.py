"""Movement v6 (owner: Wasilah): FARM, then FIGHT.

decide_move(obs, memory) -> dict with ONLY: move, boost, jump, take, drop
It also sets memory["farm_target"] (an NPC drone or crate id) for combat.py to shoot.

Why: in practice the 15 house bots never kill each other, so every bot that dies before
us is one WE killed. Points = kills. So: get strong first (weapons, cubes), then win fights.

Modes, in priority order each tick:
  zone     : outside / near the edge of the safe zone -> go in
  dodge    : a bullet, mortar shell or charged laser is about to hit us -> sidestep
  retreat  : wounded and enemies near -> run to a spot behind a building
  fight    : enemy mechs in view -> strafe at a good distance (closer if we have lightning)
  recover  : wounded, nobody in view -> sit still behind cover until healed
  farm     : weapons > cubes > supply drop > NPC drones > crates
  explore  : nothing visible -> walk to a new spot inside the next circle, looking around
"""
import math
import random

# ---- tuning knobs ----
RETREAT_HP = 0.35      # retreat below this HP fraction...
RECOVER_TO = 0.80      # ...and recover until healed to this
DANGER_RANGE = 90.0
FIGHT_RANGE = 110.0    # only switch to fight mode for enemies this close (or ones shooting us / coming at us)
FIGHT_MIN, FIGHT_MAX = 30.0, 80.0   # preferred distance with just the gun
CLOSE_MIN, CLOSE_MAX = 12.0, 32.0   # preferred distance with lightning (45 m range)
WEAPON_RANGE = 160.0   # walk this far for a weapon
CUBE_RANGE = 110.0
NPC_RANGE = 160.0      # go hunt drones this far away
NPC_SHOOT_DIST = 55.0  # stand this far from a drone while shooting it
CRATE_RANGE = 120.0
CRATE_SHOOT_DIST = 14.0
ZONE_EDGE_BUFFER = 25.0
WEAPON_PREF = {"lightning": 3, "laser": 2, "mortar": 1}


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
    memory["farm_target"] = None

    enemies = [m for m in obs["mechs"] if m["kind"] == "mech"]
    enemy_d = sorted(((_dist2d(m["pos"], (x, y)), m) for m in enemies), key=lambda p: p[0])
    threats = [m for d, m in enemy_d if d < DANGER_RANGE]
    if enemies:
        memory["last_enemy_t"] = t

    def coming_at_us(m, d):
        vx, vy = m["vel"][0], m["vel"][1]
        return d < 190 and (vx * (x - m["pos"][0]) + vy * (y - m["pos"][1])) / max(d, 1.0) > 4.0
    engaged = [(d, m) for d, m in enemy_d if d < FIGHT_RANGE or coming_at_us(m, d)]

    for ev in obs.get("events", []):
        if ev.get("type") == "damaged" and ev.get("bearing") is not None:
            memory["hit_t"] = t
            memory["hit_bearing"] = ev["bearing"]
            memory["hit_by_mech"] = ev.get("by") is not None and ev["by"] < 100
    recently_hit_by_mech = t - memory.get("hit_t", -99) < 2.0 and memory.get("hit_by_mech")

    if hp_frac < RETREAT_HP:
        memory["wounded"] = True
    elif hp_frac >= RECOVER_TO:
        memory["wounded"] = False
    wounded = memory.get("wounded", False)

    ncx, ncy = zone["next_center"]
    next_r = zone["next_radius"]
    ccx, ccy = zone["center"]
    margin = zone["radius"] - _dist2d((x, y), (ccx, ccy))
    has_lightning = any(w and w["kind"] == "lightning" for w in me["weapons"])

    dx, dy, speed, boost, mode = 0.0, 0.0, 1.0, False, "explore"
    goal, face = None, None
    dodge = _dodge_dir(obs, x, y, z)

    # ---- 1. Zone ----
    if not me["in_zone"] or margin < ZONE_EDGE_BUFFER:
        goal, mode = (ncx, ncy), "zone"
        boost = (not me["in_zone"] or margin < 10) and energy > 10

    # ---- 2. Dodge ----
    elif dodge is not None:
        dx, dy = dodge
        mode, boost = "dodge", energy > 20

    # ---- 3. Retreat ----
    elif threats and wounded:
        tx = sum(m["pos"][0] for m in threats) / len(threats)
        ty = sum(m["pos"][1] for m in threats) / len(threats)
        spot = _retreat_spot(gmap, memory, t, x, y, (tx, ty), zone)
        if spot is not None:
            goal = spot[:2]
        else:
            to_c = _unit(ncx - x, ncy - y)
            ax, ay = _unit(x - tx, y - ty)
            dx, dy = _unit(ax + to_c[0] * 0.6, ay + to_c[1] * 0.6)
        mode, boost = "retreat", energy > 25

    # ---- 4. Fight (only close / incoming enemies; far ones don't stop our farming) ----
    elif engaged or recently_hit_by_mech:
        if engaged:
            d, m = engaged[0]
            bx, by = _unit(m["pos"][0] - x, m["pos"][1] - y)
        else:
            b = memory["hit_bearing"]
            bx, by, d = math.cos(b), math.sin(b), 60.0
        if t >= memory.get("strafe_until", -1):
            memory["strafe_side"] = -memory.get("strafe_side", 1)
            memory["strafe_until"] = t + random.uniform(0.5, 1.4)
        side = memory.get("strafe_side", 1)
        sx, sy = -by * side, bx * side
        lo, hi = (CLOSE_MIN, CLOSE_MAX) if has_lightning else (FIGHT_MIN, FIGHT_MAX)
        k = -0.8 if d < lo else (0.8 if d > hi else 0.0)
        far = _dist2d((x, y), (ncx, ncy)) > next_r * 0.7
        to_c = _unit(ncx - x, ncy - y)
        zp = 0.6 if far else 0.0
        dx, dy = _unit(sx + bx * k + to_c[0] * zp, sy + by * k + to_c[1] * zp)
        mode = "fight"
        # pick up cubes / weapons dropped right next to us even mid-fight
        _grab_adjacent(obs, me, x, y, reach, out)

    # ---- 5. Recover ----
    elif wounded:
        spot = _cover_near_me(gmap, memory, x, y)
        goal, face = spot[:2], spot[2]
        speed = 0.8 if _dist2d(goal, (x, y)) > 2.5 else 0.0
        mode = "recover"
        _grab_adjacent(obs, me, x, y, reach, out)

    # ---- 6. Farm / 7. Explore ----
    else:
        f = _pick_farm(obs, me, x, y, reach, hp_frac, zone, memory)
        if f is not None:
            goal, mode, stand = f["goal"], f["mode"], f.get("stand", 0.0)
            if f.get("take") is not None:
                out["take"] = f["take"]
                if f.get("drop") is not None:
                    out["drop"] = f["drop"]
            memory["farm_target"] = f.get("target")
            d = _dist2d(goal, (x, y))
            if stand > 0:
                # shooting a drone/crate: hold this distance and sidestep a little
                if d < stand * 0.8:
                    bx, by = _unit(x - goal[0], y - goal[1])
                    dx, dy, goal = bx, by, None
                elif d < stand * 1.2:
                    bx, by = _unit(goal[0] - x, goal[1] - y)
                    side = 1 if int(t / 1.2) % 2 == 0 else -1
                    dx, dy, goal = -by * side, bx * side, None
                    speed = 0.6
            elif d < 1.5:
                speed = 0.0
        else:
            goal = _explore_point(memory, x, y, ncx, ncy, next_r)
            mode = "explore"

    if goal is not None:
        dx, dy = _unit(goal[0] - x, goal[1] - y)
        if gmap is not None and _dist2d(goal, (x, y)) > 6:
            dx, dy = _route(gmap, memory, t, x, y, goal, dx, dy)

    # ---- stuck detection: we asked to move a lot but barely moved ----
    hist = memory.setdefault("pos_hist", [])
    hist.append((x, y, math.hypot(dx, dy) * speed))
    if len(hist) > 12:
        hist.pop(0)
    jump = False
    if t < memory.get("unstick_until", -1):
        dx, dy = _rotate(dx, dy, memory["unstick_ang"])
        speed, jump, boost = 1.0, me["grounded"], energy > 15
        mode += "+unstick"
    elif len(hist) == 12:
        wanted = sum(h[2] for h in hist[:-1]) * me.get("stats", {}).get("walk_speed", 11.7) * 0.1
        moved = _dist2d(hist[0], hist[-1])
        if wanted > 6.0 and moved < wanted * 0.2 and mode not in ("fight", "hunt-drone", "crate", "dodge"):
            memory["unstick_until"] = t + 1.0
            memory["unstick_ang"] = random.choice([math.pi / 2, -math.pi / 2, math.pi * 0.75])
            memory["stuck_count"] = memory.get("stuck_count", 0) + 1
            if memory["stuck_count"] >= 3:  # same target keeps blocking us: skip it for a while
                memory.setdefault("bad_targets", {})[memory.get("last_goal_key")] = t
                memory.pop("explore_pt", None)
                memory.pop("cover_pt", None)
                memory["stuck_count"] = 0
            hist.clear()
    if memory.get("last_goal_key") != ((round(goal[0]), round(goal[1])) if goal is not None else None):
        memory["stuck_count"] = 0
    memory["last_goal_key"] = (round(goal[0]), round(goal[1])) if goal is not None else None

    if speed == 0.0 and face is not None:
        memory["move_yaw"] = face
    elif dx or dy:
        memory["move_yaw"] = math.atan2(dy, dx)
    memory["mode"] = mode

    out.update(move=[dx * speed, dy * speed], boost=bool(boost), jump=bool(jump))
    return out


# ---------------- farming ----------------

def _bad(memory_bad, pos, t):
    when = memory_bad.get((round(pos[0]), round(pos[1])))
    return when is not None and t - when < 20


def _pick_farm(obs, me, x, y, reach, hp_frac, zone, memory):
    """Best thing to farm right now, or None."""
    t = obs["t"]
    mem_bad = memory.get("bad_targets", {})
    ccx, ccy = zone["center"]
    safe_r = zone["radius"] - 20

    def ok(pos, max_dz):
        return (abs(pos[2] - me["pos"][2]) <= max_dz and _dist2d(pos, (ccx, ccy)) < safe_r
                and not _bad(mem_bad, pos, t))

    weapons = me["weapons"]
    held = {w["kind"] for w in weapons if w}
    slot1 = weapons[1] if len(weapons) > 1 else None

    # 1) weapons: fill the empty slot, or swap a mortar for lightning/laser
    best, best_score = None, -1e9
    for p in obs.get("pickups", []):
        k = p["kind"]
        if k in held or k not in WEAPON_PREF or not ok(p["pos"], 12):
            continue
        if slot1 is not None and not (slot1["kind"] == "mortar" and WEAPON_PREF[k] > 1):
            continue
        d = _dist2d(p["pos"], (x, y))
        if d > WEAPON_RANGE:
            continue
        score = WEAPON_PREF[k] * 60 - d
        if score > best_score:
            best, best_score = p, score
    if best is not None:
        d = _dist2d(best["pos"], (x, y))
        res = {"goal": best["pos"][:2], "mode": "get-" + best["kind"]}
        if d < reach * 0.9 and abs(best["pos"][2] - me["pos"][2]) < 4:
            res["take"] = best["id"]
            if slot1 is not None:
                if slot1.get("charge", 0) == 0:
                    res["drop"] = 1
                else:
                    res.pop("take")
        return res

    # 2) cubes
    cubes = [c for c in obs.get("cubes", []) if ok(c["pos"], 10) and _dist2d(c["pos"], (x, y)) < CUBE_RANGE]
    if cubes:
        c = min(cubes, key=lambda c: _dist2d(c["pos"], (x, y)))
        return {"goal": c["pos"][:2], "mode": "cube"}

    # 3) supply drop (full HP + 3 cubes) if we can get there around landing time
    for dr in obs.get("drops", []):
        d = _dist2d(dr["pos"], (x, y))
        if d < 220 and dr.get("lands_in", 99) <= d / 11.0 + 4 and dr.get("holder") in (None, me["id"]):
            return {"goal": dr["pos"][:2], "mode": "supply-drop"}

    # 4) NPC drones (350 HP, 2 cubes each) - only when healthy
    if hp_frac > 0.6:
        npcs = [n for n in obs["mechs"] if n["kind"] == "npc" and ok(n["pos"], 15)
                and _dist2d(n["pos"], (x, y)) < NPC_RANGE]
        if npcs:
            n = min(npcs, key=lambda n: _dist2d(n["pos"], (x, y)) + n.get("hp", 350) * 0.05)
            return {"goal": n["pos"][:2], "mode": "hunt-drone", "target": n["id"], "stand": NPC_SHOOT_DIST}

    # 5) crates (1 cube, 50% weapon)
    crates = [c for c in obs.get("crates", []) if ok(c["pos"], 6) and _dist2d(c["pos"], (x, y)) < CRATE_RANGE]
    if crates:
        c = min(crates, key=lambda c: _dist2d(c["pos"], (x, y)))
        return {"goal": c["pos"][:2], "mode": "crate", "target": c["id"], "stand": CRATE_SHOOT_DIST}
    return None


def _grab_adjacent(obs, me, x, y, reach, out):
    """Mid-fight: take a better weapon if we're standing on it."""
    weapons = me["weapons"]
    held = {w["kind"] for w in weapons if w}
    slot1 = weapons[1] if len(weapons) > 1 else None
    for p in obs.get("pickups", []):
        if p["kind"] in held or p["kind"] not in WEAPON_PREF:
            continue
        if _dist2d(p["pos"], (x, y)) < reach * 0.9 and abs(p["pos"][2] - me["pos"][2]) < 4:
            if slot1 is None:
                out["take"] = p["id"]
            elif slot1["kind"] == "mortar" and WEAPON_PREF[p["kind"]] > 1 and slot1.get("charge", 0) == 0:
                out["take"], out["drop"] = p["id"], 1
            return


def _explore_point(memory, x, y, ncx, ncy, next_r):
    pt = memory.get("explore_pt")
    if pt is None or _dist2d(pt, (x, y)) < 8 or _dist2d(pt, (ncx, ncy)) > next_r * 0.75:
        for _ in range(10):
            ang = random.uniform(-math.pi, math.pi)
            rad = random.uniform(0, next_r * 0.65)
            cand = (ncx + rad * math.cos(ang), ncy + rad * math.sin(ang))
            if _dist2d(cand, (x, y)) > 50 or next_r < 80:
                break
        pt = cand
        memory["explore_pt"] = pt
    return pt


# ---------------- safety helpers ----------------

def _dodge_dir(obs, x, y, z):
    cz = z + 4.5
    best = None
    for p in obs.get("projectiles", []):
        px, py, pz = p["pos"]
        vx, vy, vz = p["vel"]
        if p["kind"] == "mortar":
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
                return _unit(x - lx, y - ly)
            continue
        if p["kind"] == "npc":
            continue  # slow & weak, not worth breaking our plan
        rx, ry, rz = x - px, y - py, cz - pz
        v2 = vx * vx + vy * vy + vz * vz
        if v2 < 1:
            continue
        tca = (rx * vx + ry * vy + rz * vz) / v2
        if not 0 < tca < 0.5:
            continue
        mx, my = px + vx * tca - x, py + vy * tca - y
        if math.hypot(mx, my) < 3.5:
            sx, sy = _unit(-vy, vx)
            if sx * mx + sy * my > 0:
                sx, sy = -sx, -sy
            best = (sx, sy)
    if best is not None:
        return best
    for m in obs["mechs"]:
        if m["kind"] == "mech" and m.get("charging"):
            bearing_to_me = math.atan2(y - m["pos"][1], x - m["pos"][0])
            if _angdiff(m["aim"][0], bearing_to_me) < 0.12 and _dist2d(m["pos"], (x, y)) < 280:
                bx, by = _unit(x - m["pos"][0], y - m["pos"][1])
                side = 1 if int(obs["t"] * 2) % 2 else -1
                return (-by * side, bx * side)
    return None


def _route(gmap, memory, t, x, y, goal, dx, dy):
    """Go straight if the way is clear; otherwise follow an A* route around the buildings."""
    if not gmap.blocked((x, y), goal, step=3.0, walk=True):
        memory.pop("path", None)
        return dx, dy
    nav = getattr(gmap, "nav", None)
    if nav is None:
        return _steer(gmap, x, y, dx, dy)
    p = memory.get("path")
    need = (p is None or _dist2d(p["goal"], goal) > 8 or t - p["t"] > 4.0
            or min((_dist2d(w, (x, y)) for w in p["pts"][p["i"]:p["i"] + 6]), default=99) > 12)
    if need and p is not None and t - p["t"] < 0.5:
        need = False  # plan at most twice a second (keeps every reply fast)
    if need and t - memory.get("path_fail_t", -99) < 1.5:
        return _steer(gmap, x, y, dx, dy)  # just failed to find a route: don't retry every tick
    if need:
        pts = nav.path((x, y), goal, max_expand=4000)
        if pts is None:
            memory.pop("path", None)
            memory["path_fail_t"] = t
            return _steer(gmap, x, y, dx, dy)
        p = {"goal": goal, "pts": pts, "i": 0, "t": t}
        memory["path"] = p
    pts = p["pts"]
    # move our index forward to the closest upcoming waypoint
    i = p["i"]
    while i + 1 < len(pts) and _dist2d(pts[i + 1], (x, y)) <= _dist2d(pts[i], (x, y)):
        i += 1
    p["i"] = i
    # aim for the furthest waypoint (within ~28 m) we can walk to in a straight line
    target = pts[min(i + 1, len(pts) - 1)]
    for k in range(min(i + 7, len(pts) - 1), i, -1):
        if _dist2d(pts[k], (x, y)) < 28 and not gmap.blocked((x, y), pts[k], step=3.0, walk=True):
            target = pts[k]
            break
    return _unit(target[0] - x, target[1] - y)


def _steer(gmap, x, y, dx, dy, probe=15.0):
    if not gmap.blocked((x, y), (x + dx * probe, y + dy * probe), step=4.0, walk=True):
        return dx, dy
    for deg in (25, -25, 50, -50, 75, -75, 100, -100, 130, -130):
        rx, ry = _rotate(dx, dy, math.radians(deg))
        if not gmap.blocked((x, y), (x + rx * probe, y + ry * probe), step=4.0, walk=True):
            return rx, ry
    return dx, dy


def _cover_near_me(gmap, memory, x, y):
    spot = memory.get("cover_pt")
    if spot is not None and _dist2d(spot, (x, y)) < 60:
        return spot
    spot = None
    if gmap is not None:
        near = gmap.cover_near(x, y, 60.0)
        if near:
            spot = near[0][1]
    if spot is None:
        spot = (x, y, memory.get("move_yaw", 0.0))
    memory["cover_pt"] = spot
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
        memory["cover_pt"] = best
    return best
