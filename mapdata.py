"""Map helper (owner: Wasilah). Reads /arena/v2/world.json once at match start.

Gives movement.py these things:
  - in_building(x, y)        : is this spot inside a building tall enough to hide behind (8 m+)?
  - blocked(a, b)            : does such a building stand between points a and b? (2D line check)
  - blocked(a, b, walk=True) : same, but counting EVERY wall a mech can't step over (1.2 m+), for steering
  - cover_near(x, y, r)      : "cover spots" = points 5 m outside building walls, with the
                               direction facing away from the wall (back to the wall = no rear hits)
If the map can't be loaded, `ok` is False and movement.py falls back to map-free behaviour.
"""
import json
import math
import os

CELL = 25.0          # grid cell size (m) for fast lookups
MIN_HEIGHT = 8.0     # buildings shorter than this don't hide a 9 m mech
WALK_HEIGHT = 1.5    # anything taller than this is a wall when walking (step_up is 1.2 m)
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
        self.grid = {}        # (cx, cy) -> [prism index], tall buildings (hide / block shots)
        self.walls = []       # every building/stadium a mech can't walk over
        self.wall_grid = {}
        self.cover = []       # (x, y, face_yaw)
        self.cover_grid = {}  # (cx, cy) -> [cover index]
        self.nav = None
        try:
            with open(os.path.join(map_dir, "v2", "world.json")) as f:
                world = json.load(f)
            self._load(world)
            self.ok = len(self.prisms) > 0
            self.nav = None
            if self.ok and "bounds" in world:
                self.nav = Nav(self, world["bounds"])
        except Exception as e:  # missing file, bad format... -> run without the map
            import sys
            print(f"mapdata: could not load map: {e!r}", file=sys.stderr, flush=True)

    # ---------- building ----------
    def _load(self, world):
        for pr in world.get("prisms", []):
            if pr.get("kind") not in ("building", "stadium"):
                continue  # bridges: you can walk and shoot under them
            h = pr.get("top", 0) - pr.get("base", 0)
            if h < WALK_HEIGHT:
                continue
            ring = _ring_pts(pr.get("ring", []))
            if len(ring) < 3:
                continue
            holes_raw = pr.get("holes") or []
            if holes_raw and not isinstance(holes_raw[0], list):
                holes_raw = [holes_raw]
            holes = [_ring_pts(hh) for hh in holes_raw if len(hh) >= 6]
            xs = [p[0] for p in ring]
            ys = [p[1] for p in ring]
            entry = (min(xs), min(ys), max(xs), max(ys), ring, holes)
            cells = [(cx, cy) for cx in range(int(min(xs) // CELL), int(max(xs) // CELL) + 1)
                     for cy in range(int(min(ys) // CELL), int(max(ys) // CELL) + 1)]
            wi = len(self.walls)
            self.walls.append(entry)
            for c in cells:
                self.wall_grid.setdefault(c, []).append(wi)
            if h < MIN_HEIGHT:
                continue
            idx = len(self.prisms)
            self.prisms.append(entry)
            for c in cells:
                self.grid.setdefault(c, []).append(idx)
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
                if self.in_building(px, py, walk=True):
                    continue  # spot lands inside another building
                ci = len(self.cover)
                self.cover.append((px, py, math.atan2(ny, nx)))
                self.cover_grid.setdefault((int(px // CELL), int(py // CELL)), []).append(ci)

    # ---------- queries ----------
    def in_building(self, x, y, walk=False):
        grid, items = (self.wall_grid, self.walls) if walk else (self.grid, self.prisms)
        for idx in grid.get((int(x // CELL), int(y // CELL)), ()):
            minx, miny, maxx, maxy, ring, holes = items[idx]
            if minx <= x <= maxx and miny <= y <= maxy and _inside(x, y, ring):
                if not any(_inside(x, y, h) for h in holes):
                    return True
        return False

    def blocked(self, a, b, step=4.0, walk=False):
        """True if a building stands on the straight 2D line between a and b."""
        d = math.hypot(b[0] - a[0], b[1] - a[1])
        n = int(d // step)
        for i in range(1, n):
            f = i / n
            if self.in_building(a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, walk):
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


# ---------------- pathfinding (GPS-style routes around buildings) ----------------
import heapq

NAV_CELL = 4.0  # meters per grid square


class Nav:
    """A walkability grid over the whole map + A* search. Built once at match start."""

    def __init__(self, gmap, bounds):
        self.g = gmap
        self.minx, self.miny = bounds["minX"], bounds["minY"]
        self.nx = int((bounds["maxX"] - self.minx) // NAV_CELL) + 1
        self.ny = int((bounds["maxY"] - self.miny) // NAV_CELL) + 1
        blocked = bytearray(self.nx * self.ny)
        for j in range(self.ny):
            cy = self.miny + (j + 0.5) * NAV_CELL
            for i in range(self.nx):
                if gmap.in_building(self.minx + (i + 0.5) * NAV_CELL, cy, walk=True):
                    blocked[j * self.nx + i] = 1
        # grow walls by one square so the 2.5 m-wide mech doesn't scrape corners
        grown = bytearray(blocked)
        for j in range(self.ny):
            for i in range(self.nx):
                if blocked[j * self.nx + i]:
                    for dj in (-1, 0, 1):
                        for di in (-1, 0, 1):
                            ii, jj = i + di, j + dj
                            if 0 <= ii < self.nx and 0 <= jj < self.ny:
                                grown[jj * self.nx + ii] = 2 if not blocked[jj * self.nx + ii] else 1
        self.raw = blocked
        self.grid = grown  # 0 free, 2 near a wall (allowed but costly), 1 wall

    def cell(self, x, y):
        i = int((x - self.minx) // NAV_CELL)
        j = int((y - self.miny) // NAV_CELL)
        return min(max(i, 0), self.nx - 1), min(max(j, 0), self.ny - 1)

    def center(self, i, j):
        return (self.minx + (i + 0.5) * NAV_CELL, self.miny + (j + 0.5) * NAV_CELL)

    def _free_near(self, i, j):
        if self.grid[j * self.nx + i] != 1:
            return i, j
        for r in range(1, 6):
            for dj in range(-r, r + 1):
                for di in range(-r, r + 1):
                    ii, jj = i + di, j + dj
                    if 0 <= ii < self.nx and 0 <= jj < self.ny and self.grid[jj * self.nx + ii] != 1:
                        return ii, jj
        return None

    def path(self, a, b, max_expand=6000):
        """List of waypoints from a to b, or None if not found within the budget."""
        s = self._free_near(*self.cell(a[0], a[1]))
        e = self._free_near(*self.cell(b[0], b[1]))
        if s is None or e is None:
            return None
        nx, grid = self.nx, self.grid
        start, goal = s[1] * nx + s[0], e[1] * nx + e[0]
        gx, gy = e
        came = {start: -1}
        cost = {start: 0.0}
        heap = [(0.0, start)]
        steps = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                 (1, 1, 1.414), (1, -1, 1.414), (-1, 1, 1.414), (-1, -1, 1.414))
        expanded = 0
        while heap:
            _, cur = heapq.heappop(heap)
            if cur == goal:
                break
            expanded += 1
            if expanded > max_expand:
                return None
            ci, cj = cur % nx, cur // nx
            base = cost[cur]
            for di, dj, w in steps:
                ii, jj = ci + di, cj + dj
                if not (0 <= ii < nx and 0 <= jj < self.ny):
                    continue
                k = jj * nx + ii
                v = grid[k]
                if v == 1:
                    continue
                if di and dj and (grid[cj * nx + ii] == 1 or grid[jj * nx + ci] == 1):
                    continue  # don't cut wall corners diagonally
                nc = base + w * (1.6 if v == 2 else 1.0)
                if nc < cost.get(k, 1e18):
                    cost[k] = nc
                    came[k] = cur
                    h = math.hypot(ii - gx, jj - gy)
                    heapq.heappush(heap, (nc + 1.3 * h, k))
        if goal not in came:
            return None
        cells = []
        k = goal
        while k != -1:
            cells.append(self.center(k % nx, k // nx))
            k = came[k]
        cells.reverse()
        return cells
