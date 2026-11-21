      """Main bot (v8, farm, fight, endgame): joins movement (where to go) and combat (what to shoot) into one action.

Owner: Wasilah (integration). movement.py = Wasilah, combat.py = teammate.
"""
import math
import random

import mechfight as mf
import combat
import mapdata
import movement

# Upgrade preference: survival first. Words are matched against the upgrade's id/name/desc.
UPGRADE_WORDS = [
    ("armor", 10), ("damage taken", 10), ("fire rate", 9), ("rate", 6), ("damage", 8),
    ("hp", 8), ("health", 8), ("regen", 7), ("reload", 6), ("lightning", 5), ("cooldown", 5),
    ("sensor", 3), ("vision", 3), ("speed", 3), ("energy", 3),
]


UPGRADE_DEFS = {}  # id -> full definition from hello.config.upgrades (has the exact stat paths)


def pick_upgrade(offer):
    best_i, best_score = 0, -1
    for i, ch in enumerate(offer.get("choices", [])):
        d = UPGRADE_DEFS.get(ch.get("id"), {})
        paths = " ".join(e.get("path", "") for e in d.get("effects", []))
        text = f"{ch.get('id', '')} {ch.get('name', '')} {ch.get('desc', '')} {paths}".lower()
        score = sum(w for word, w in UPGRADE_WORDS if word in text)
        if score > best_score:
            best_i, best_score = i, score
    return best_i


class Bot:
    name = "glitch-v8"

    def __init__(self, hello):
        cfg = hello["config"]
        random.seed(hello.get("seed", 0))
        for u in cfg.get("upgrades", []) or []:
            if isinstance(u, dict) and "id" in u:
                UPGRADE_DEFS[u["id"]] = u
        # `memory` is shared by movement and combat and lives for the whole match
        self.memory = {
            "cfg": cfg,
            "slot": hello.get("slot"),
            "reach": cfg.get("loot", {}).get("pickup_radius", 4.0),
        }
        # Load the map once (buildings -> cover spots). If it fails, movement runs without it.
        self.memory["map"] = mapdata.MapData(hello.get("map", {}).get("dir", "/arena"))
        mf.log(f"map loaded: {self.memory['map'].ok}, buildings={len(self.memory['map'].prisms)}, "
               f"cover spots={len(self.memory['map'].cover)}")

    def act(self, obs):
        mem = self.memory
        action = {"tick": obs["tick"]}

        # 1) Movement first (it also records the move direction for combat to use)
        try:
            action.update(movement.decide_move(obs, mem))
        except Exception as e:  # never crash: fall back to walking toward the safe zone
            mf.log("movement error:", repr(e))
            me, z = obs["self"], obs["zone"]
            dx, dy = z["next_center"][0] - me["pos"][0], z["next_center"][1] - me["pos"][1]
            n = math.hypot(dx, dy) or 1.0
            action.update(move=[dx / n, dy / n], boost=not me["in_zone"])

        # 2) Combat (aim / fire / reload)
        try:
            action.update(combat.decide_combat(obs, mem))
        except Exception as e:
            mf.log("combat error:", repr(e))

        # 3) Upgrades
        if obs.get("offer"):
            action["pick"] = pick_upgrade(obs["offer"])

        mf.draw.label(f"{mem.get('mode', '?')} | hp {obs['self']['hp']:.0f}")
        return action


mf.run(Bot)
