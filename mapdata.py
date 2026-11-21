"""Map helper (owner: Wasilah). Reads /arena/v2/world.json once at match start.

Gives movement.py three things:
  - in_building(x, y)        : is this spot inside a building footprint?
  - blocked(a, b)            : does a building stand between points a and b? (2D line check)
  - cover_near(x, y, r)      : "cover spots" = points 5 m outside building walls, with the
                               direction facing away from the wall (back to the wall = no rear hits)
If the map can't be loaded, `ok` is False and movement.py falls back to map-free behaviour.
"""
import json
import math
import os

CELL = 25.0          # grid cell size (m) for fast lookups
MIN_HEIGHT = 8.0     # buildings shorter than this don't hide a 9 m mech
COVER_STEP = 12.0    # one cover spot every N meters of wall
COVER_OFFSET = 5.0   # how far from the wall the spot is (mech radius is 2.5 m)


def _ring_pts(flat):
    return [(flat[i], flat[i + 1]) for i in range(0, len(flat) - 1, 2)]


def _inside(px, py, pts):
    """Classic ray-casting point-in-polygon."""
    inside = False
    j = len(pts) - 1
    for i in range(len(pts)):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / ((yj - yi) or 1e-9) + xi:
            inside = not inside
        j = i
    return inside


class MapData:
    def __init__(self, map_dir):
        self.ok = False
        self.prisms = []      # (minx, miny, maxx, maxy, ring_pts, holes_pts_list)
        self.grid = {}        # (cx, cy) -> [prism index]
        self.cover = []       # (x, y, face_yaw)
        self.cover_grid = {}  # (cx, cy) -> [cover index]
        try:
            with open(os.path.join(map_dir, "v2", "world.json")) as f:
                world = json.load(f)
            self._load(world)
            self.ok = len(self.prisms) > 0
        except Exception as e:  # missing file, bad format... -> run without the map
            import sys
            print(f"mapdata: could not load map: {e!r}", file=sys.stderr, flush=True)

    # ---------- building ----------
    def _load(self, world):
        for pr in world.get("prisms", []):
            if pr.get("kind") not in ("building", "stadium"):
                continue  # bridges: you can walk and shoot under them
            if pr.get("top", 0) - pr.get("base", 0) < MIN_HEIGHT:
                continue
            ring = _ring_pts(pr.get("ring", []))
            if len(ring) < 3:
                continue
            holes_raw = pr.get("holes") or []
            if holes_raw and not isinstance(holes_raw[0], list):
                holes_raw = [holes_raw]
            holes = [_ring_pts(h) for h in holes_raw if len(h) >= 6]
            xs = [p[0] for p in ring]
            ys = [p[1] for p in ring]
            idx = len(self.prisms)
            self.prisms.append((min(xs), min(ys), max(xs), max(ys), ring, holes))
            for cx in range(int(min(xs) // CELL), int(max(xs) // CELL) + 1):
                for cy in range(int(min(ys) // CELL), int(max(ys) // CELL) + 1):
                    self.grid.setdefault((cx, cy), []).append(idx)
        for idx, (_, _, _, _, ring, _) in enumerate(self.prisms):
            self._add_cover(ring)

    def _add_cover(self, ring):
        # polygon orientation decides which side of each edge is "outside"
        area = sum(ring[i][0] * ring[(i + 1) % len(ring)][1] - ring[(i + 1) % len(ring)][0] * ring[i][1]
                   for i in range(len(ring)))
        ccw = area > 0
        for i in range(len(ring)):
            (x1, y1), (x2, y2) = ring[i], ring[(i + 1) % len(ring)]
            ex, ey = x2 - x1, y2 - y1
            L = math.hypot(ex, ey)
            if L < 4:
                continue
            # outward normal: right of the edge for CCW polygons
            nx, ny = (ey / L, -ex / L) if ccw else (-ey / L, ex / L)
            n = max(1, int(L // COVER_STEP))
            for k in range(n):
                f = (k + 0.5) / n
                px = x1 + ex * f + nx * COVER_OFFSET
                py = y1 + ey * f + ny * COVER_OFFSET
                if self.in_building(px, py):
                    continue  # spot lands inside another building
                ci = len(self.cover)
                self.cover.append((px, py, math.atan2(ny, nx)))
                self.cover_grid.setdefault((int(px // CELL), int(py // CELL)), []).append(ci)

    # ---------- queries ----------
    def in_building(self, x, y):
        for idx in self.grid.get((int(x // CELL), int(y // CELL)), ()):
            minx, miny, maxx, maxy, ring, holes = self.prisms[idx]
            if minx <= x <= maxx and miny <= y <= maxy and _inside(x, y, ring):
                if not any(_inside(x, y, h) for h in holes):
                    return True
        return False

    def blocked(self, a, b, step=4.0):
        """True if a building stands on the straight 2D line between a and b."""
        d = math.hypot(b[0] - a[0], b[1] - a[1])
        n = int(d // step)
        for i in range(1, n):
            f = i / n
            if self.in_building(a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f):
                return True
        return False

    def cover_near(self, x, y, r):
        """Cover spots within r meters, nearest first: list of (dist, (x, y, face_yaw))."""
        out = []
        c = int(r // CELL) + 1
        bx, by = int(x // CELL), int(y // CELL)
        for cx in range(bx - c, bx + c + 1):
            for cy in range(by - c, by + c + 1):
                for ci in self.cover_grid.get((cx, cy), ()):
                    p = self.cover[ci]
                    d = math.hypot(p[0] - x, p[1] - y)
                    if d <= r:
                        out.append((d, p))
        out.sort(key=lambda t: t[0])
        return out
