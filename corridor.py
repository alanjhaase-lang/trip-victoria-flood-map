"""Route-corridor geometry helpers shared by the update scripts (stdlib only).

route.json (built by build_route.py from the Google My Map KML) holds the
planned route as day legs. Everything is filtered by distance to those lines.
"""
import json
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROUTE_FILE = os.path.join(HERE, "route.json")

# Buffers (km) from the planned route polyline.
GAUGE_BUFFER_KM = 25.0     # river gauges: upstream/downstream gauges still matter for crossings
ROAD_BUFFER_KM = 10.0      # road closures: roads you might drive or detour onto

R_EARTH = 6371.0088


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R_EARTH * math.asin(math.sqrt(a))


def _xy(lat, lon, lat0):
    k = math.pi / 180 * R_EARTH
    return lon * k * math.cos(math.radians(lat0)), lat * k


def simplify(coords, tol_km):
    """Douglas-Peucker on [[lat, lon], ...] (iterative)."""
    if len(coords) < 3:
        return list(coords)
    lat0 = sum(c[0] for c in coords) / len(coords)
    pts = [_xy(c[0], c[1], lat0) for c in coords]
    keep = [False] * len(coords)
    keep[0] = keep[-1] = True
    stack = [(0, len(coords) - 1)]
    while stack:
        i, j = stack.pop()
        (x1, y1), (x2, y2) = pts[i], pts[j]
        dx, dy = x2 - x1, y2 - y1
        L2 = dx * dx + dy * dy
        best, bi = -1.0, None
        for k in range(i + 1, j):
            px, py = pts[k]
            if L2 == 0:
                d = math.hypot(px - x1, py - y1)
            else:
                t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / L2))
                d = math.hypot(px - x1 - t * dx, py - y1 - t * dy)
            if d > best:
                best, bi = d, k
        if bi is not None and best > tol_km:
            keep[bi] = True
            stack.append((i, bi))
            stack.append((bi, j))
    return [c for c, k in zip(coords, keep) if k]


class Corridor:
    """Distance from a point to each day leg of the route."""

    def __init__(self, route=None, tol_km=0.15):
        if route is None:
            with open(ROUTE_FILE) as f:
                route = json.load(f)
        self.route = route
        self.days = []
        for d in route["days"]:
            line = simplify(d["coords"], tol_km)
            lats = [c[0] for c in line]
            lons = [c[1] for c in line]
            # cumulative chainage (km from start of the day / start of the leg)
            cum = [0.0]
            for a, b in zip(line, line[1:]):
                cum.append(cum[-1] + haversine_km(a[0], a[1], b[0], b[1]))
            self.days.append({"id": d["id"], "leg": d["leg"], "name": d["name"], "line": line,
                              "cum": cum, "km0": d.get("km_start", 0.0),
                              "bbox": (min(lats), max(lats), min(lons), max(lons))})

    def _day_dist(self, day, lat, lon, max_km):
        s, n, w, e = day["bbox"]
        dlat = max_km / 111.0
        dlon = max_km / (111.0 * max(0.2, math.cos(math.radians(lat))))
        if lat < s - dlat or lat > n + dlat or lon < w - dlon or lon > e + dlon:
            return None, None
        px, py = _xy(lat, lon, lat)
        best, chain = None, None
        line, cum = day["line"], day["cum"]
        for i in range(len(line) - 1):
            a, b = line[i], line[i + 1]
            if (abs(a[0] - lat) > dlat and abs(b[0] - lat) > dlat and (a[0] - lat) * (b[0] - lat) > 0):
                continue
            x1, y1 = _xy(a[0], a[1], lat)
            x2, y2 = _xy(b[0], b[1], lat)
            dx, dy = x2 - x1, y2 - y1
            L2 = dx * dx + dy * dy
            t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / L2))
            d = math.hypot(px - x1 - t * dx, py - y1 - t * dy)
            if best is None or d < best:
                best, chain = d, cum[i] + t * (cum[i + 1] - cum[i])
        return best, chain

    def locate(self, lat, lon, max_km):
        """Return dict(dist_km, days=[ids within max_km], legs=[...], nearest_day, leg_km)
        or None when the point is farther than max_km from every day leg."""
        hits = []
        for day in self.days:
            d, ch = self._day_dist(day, lat, lon, max_km)
            if d is not None and d <= max_km:
                hits.append((d, ch, day))
        if not hits:
            return None
        hits.sort(key=lambda h: h[0])
        d, ch, day = hits[0]
        return {"dist_km": round(d, 1),
                "days": [h[2]["id"] for h in hits],
                "legs": sorted({h[2]["leg"] for h in hits}),
                "nearest_day": day["id"],
                "leg_km": round(day["km0"] + ch, 0)}

    def locate_many(self, points, max_km):
        """Nearest result over several [lat, lon] points (e.g. closure line vertices)."""
        best = None
        days, legs = set(), set()
        for lat, lon in points:
            r = self.locate(lat, lon, max_km)
            if r:
                days.update(r["days"])
                legs.update(r["legs"])
                if best is None or r["dist_km"] < best["dist_km"]:
                    best = r
        if best:
            best = dict(best, days=[d["id"] for d in self.days if d["id"] in days], legs=sorted(legs))
        return best


def densify(line, step_km=1.0):
    """Add points so consecutive vertices are <= step_km apart ([[lat, lon], ...])."""
    out = []
    for a, b in zip(line, line[1:]):
        out.append(a)
        d = haversine_km(a[0], a[1], b[0], b[1])
        n = int(d // step_km)
        for k in range(1, n + 1):
            t = k / (n + 1)
            out.append([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t])
    if line:
        out.append(line[-1])
    return out
