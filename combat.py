"""Combat (owner: teammate). Starter version - improve freely.

decide_combat(obs, memory) -> dict with ONLY: aim, fire, reload

- Picks a target (enemy mech; NPC only if it is shooting us; crate if nothing else around).
- Leads moving targets for the gun, lobs the mortar, holds/releases the laser, fires lightning up close.
- With no target: faces whoever last hit us (so hits land on front armor), else faces where we walk.
"""
import math

ENGAGE_RANGE = 70.0      # shoot enemies closer than this (further = stay quiet, just watch). v2: was 130
CRATE_RANGE = 40.0       # shoot crates this close when nothing else is around


def _d3(a, b):
    return math.dist(a, b)


def _d2(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _angdiff(a, b):
    return abs((a - b + math.pi) % (2 * math.pi) - math.pi)


def _aim_at(src, dst):
    yaw = math.atan2(dst[1] - src[1], dst[0] - src[0])
    pitch = math.atan2(dst[2] - src[2], _d2(src, dst))
    return yaw, pitch


def _lead(src, tpos, tvel, speed):
    """Where to aim so a bullet at `speed` meets a target moving at `tvel`."""
    p = list(tpos)
    for _ in range(3):
        tt = _d3(src, p) / max(speed, 1.0)
        p = [tpos[i] + tvel[i] * tt for i in range(3)]
    return p


def _mortar_pitch(dist, dz, v, g):
    """Low-arc launch angle to hit a point `dist` away and `dz` higher. None if out of range."""
    v2 = v * v
    disc = v2 * v2 - g * (g * dist * dist + 2 * dz * v2)
    if disc < 0 or dist < 1:
        return None
    return math.atan((v2 - math.sqrt(disc)) / (g * dist))


def decide_combat(obs, memory):
    me = obs["self"]
    pos = me["pos"]
    t = obs["t"]
    stats = me.get("stats", {})
    muzzle = [pos[0], pos[1], pos[2] + stats.get("muzzle_height", 7.0)]
    weapons = me["weapons"]
    w0 = weapons[0] if len(weapons) > 0 else None
    w1 = weapons[1] if len(weapons) > 1 else None
    out = {"fire": [False, False]}

    # Who has shot us recently?
    attackers = set()
    for ev in obs.get("events", []):
        if ev.get("type") == "damaged" and ev.get("by") is not None:
            attackers.add(ev["by"])
            memory.setdefault("attackers", {})[ev["by"]] = t
    for k, when in list(memory.get("attackers", {}).items()):
        if t - when < 5:
            attackers.add(k)

    # ---- choose target ----
    target, tkind = None, None
    best = 1e9
    for m in obs["mechs"]:
        d = _d3(pos, m["pos"])
        if m["kind"] == "mech":
            if d > ENGAGE_RANGE and m["id"] not in attackers:
                continue
            # prefer close + weak targets, and whoever is shooting us
            hp = m.get("hp", 1000)
            score = d + hp * 0.03 - (60 if m["id"] in attackers else 0)
        else:  # NPC: only fight back
            if m["id"] not in attackers:
                continue
            score = d + 80
        if score < best:
            target, tkind, best = m, m["kind"], score

    if target is None and not obs["mechs"]:
        crates = [c for c in obs.get("crates", []) if _d3(pos, c["pos"]) < CRATE_RANGE]
        if crates:
            c = min(crates, key=lambda c: _d3(pos, c["pos"]))
            target, tkind = {"pos": c["pos"], "vel": [0, 0, 0], "id": c["id"]}, "crate"

    # ---- no target: face the threat / walking direction ----
    if target is None:
        if t - memory.get("hit_t", -99) < 3.0:
            out["aim"] = [memory["hit_bearing"], 0.0]
        elif "move_yaw" in memory:
            out["aim"] = [memory["move_yaw"], 0.0]
        # top up the gun while nothing is happening
        if w0 and w0["kind"] == "gun" and w0.get("ammo", 99) < w0["stats"].get("mag", 12) * 0.5 and w0.get("reload", 0) == 0:
            out["reload"] = True
        return out

    # ---- aim + fire ----
    height = 4.5 if tkind == "mech" else (1.75 if tkind == "npc" else 0.5)
    tpos = [target["pos"][0], target["pos"][1], target["pos"][2] + height]
    tvel = target.get("vel", [0, 0, 0])
    dist = _d3(muzzle, tpos)
    cur_yaw, cur_pitch = me["aim"]
    tol = max(0.02, math.atan2(2.0, max(dist, 1.0)))  # allowed aim error (rad)

    # Pick which weapon decides the aim this tick
    plan = "gun"
    if w1:
        k = w1["kind"]
        if k == "lightning" and dist < w1["stats"].get("range", 45) - 3:
            plan = "lightning"
        elif k == "laser" and dist < w1["stats"].get("range", 280):
            plan = "laser"
        elif k == "mortar" and 25 < dist < 220 and w1.get("cooldown", 0) <= 0:
            plan = "mortar"

    if plan == "mortar":
        st = w1["stats"]
        v, g = st.get("speed", 72), st.get("gravity", 22)
        tube = [pos[0], pos[1], pos[2] + stats.get("height", 9.0) + 0.5]
        lp = _lead(tube, tpos, tvel, v * 0.7)
        pitch = _mortar_pitch(_d2(tube, lp), lp[2] - tube[2], v, g)
        yaw = math.atan2(lp[1] - tube[1], lp[0] - tube[0])
        if pitch is None:
            plan = "gun"
        else:
            out["aim"] = [yaw, pitch]
            if _angdiff(cur_yaw, yaw) < 0.04 and abs(cur_pitch - pitch) < 0.03:
                out["fire"][1] = True
    if plan != "mortar":
        speed = w0["stats"].get("speed", 252) if w0 else 252
        aim_pt = _lead(muzzle, tpos, tvel, speed) if plan == "gun" else tpos
        yaw, pitch = _aim_at(muzzle, aim_pt)
        out["aim"] = [yaw, pitch]
        aligned = _angdiff(cur_yaw, yaw) < tol and abs(cur_pitch - pitch) < tol * 1.5

        # slot 0: gun
        if w0 and w0["kind"] == "gun" and aligned and dist < w0["stats"].get("range", 230):
            out["fire"][0] = True

        # slot 1
        if w1 and plan == "lightning":
            cone = math.radians(w1["stats"].get("cone_deg", 30)) / 2
            if _angdiff(cur_yaw, yaw) < cone and me["energy"] > 8:
                out["fire"][1] = True
        elif w1 and plan == "laser":
            charge = w1.get("charge", 0)
            full = w1["stats"].get("charge", 0.75)
            if w1.get("cooldown", 0) <= 0:
                # hold to charge; let go (fire=False) once charged and lined up
                if charge >= full * 0.9 and aligned:
                    out["fire"][1] = False
                else:
                    out["fire"][1] = True

    return out
