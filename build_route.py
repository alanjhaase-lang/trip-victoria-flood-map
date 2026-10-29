#!/usr/bin/env python3
"""Build route.json (and route-stops.json) from Alan's Google My Map "Trip to Victoria".

  python3 build_route.py            # download the KML from Google My Maps, then build
  python3 build_route.py --offline  # use the committed trip.kml

The My Map has two pin folders ("Westbrook to Geelong", "Victoria to Westbrook") and two
Google "Directions" layers whose LineStrings are the planned driving route. The return
directions layer starts at 386 Dorset Rd, so the short 6 Malone St (Geelong) -> 386 Dorset Rd
gap is filled with an OSRM driving route (flagged "filled": true in route.json).
Run again whenever the My Map changes; the workflow does not rebuild the route automatically.
Standard library only.
"""
import datetime as dt
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

from corridor import haversine_km, simplify

HERE = os.path.dirname(os.path.abspath(__file__))
MID = "1qbtFZr0yQwFXtvUWUALYAr6sXxRSvDc"
KML_URL = f"https://www.google.com/maps/d/kml?mid={MID}&forcekml=1"
VIEWER = f"https://www.google.com/maps/d/viewer?mid={MID}"
KML_FILE = os.path.join(HERE, "trip.kml")
OSRM = "https://router.project-osrm.org/route/v1/driving/{},{};{},{}?overview=full&geometries=geojson"
K = "{http://www.opengis.net/kml/2.2}"
UA = "Mozilla/5.0 (X11; Linux x86_64) trip-victoria-flood-map"

OVERNIGHT = re.compile(r"motel|resort|apartment|cabins|voco|discovery parks|hotel", re.I)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def parse_kml(raw):
    root = ET.fromstring(raw)
    folders = []
    for fo in root.iter(K + "Folder"):
        pins, lines = [], []
        for pm in fo.findall(K + "Placemark"):
            name = (pm.findtext(K + "name") or "").strip()
            desc = (pm.findtext(K + "description") or "").strip()
            pt = pm.find(".//" + K + "Point")
            ls = pm.find(".//" + K + "LineString")
            if pt is not None:
                lon, lat = map(float, pt.findtext(K + "coordinates").strip().split(",")[:2])
                pins.append({"name": name, "desc": desc, "lat": lat, "lon": lon})
            elif ls is not None:
                pts = [list(map(float, c.split(",")[:2])) for c in ls.findtext(K + "coordinates").split()]
                lines.append({"name": name, "coords": [[la, lo] for lo, la in pts]})
        folders.append({"name": (fo.findtext(K + "name") or "").strip(), "pins": pins, "lines": lines})
    title = (root.findtext(f"{K}Document/{K}name") or "").strip()
    return title, folders


def nearest_idx(line, lat, lon, start=0):
    best, bi = None, None
    for i in range(start, len(line)):
        d = haversine_km(lat, lon, line[i][0], line[i][1])
        if best is None or d < best:
            best, bi = d, i
    return bi, best


def cumkm(line):
    out = [0.0]
    for a, b in zip(line, line[1:]):
        out.append(out[-1] + haversine_km(a[0], a[1], b[0], b[1]))
    return out


GEOCODE_CACHE = os.path.join(HERE, "geocode_cache.json")
NOMINATIM = "https://nominatim.openstreetmap.org/reverse?format=jsonv2&zoom=18&addressdetails=1&lat={}&lon={}"


def full_address(addr, p):
    """Keep the My Map address if it already has a state; otherwise reverse-geocode (cached)."""
    if addr and re.search(r"\b(QLD|NSW|VIC)\b", addr):
        return addr
    cache = json.load(open(GEOCODE_CACHE)) if os.path.exists(GEOCODE_CACHE) else {}
    key = f"{p['lat']:.5f},{p['lon']:.5f}"
    if key not in cache:
        import time
        time.sleep(1.1)  # Nominatim usage policy: max 1 request/second
        try:
            a = json.loads(fetch(NOMINATIM.format(p["lat"], p["lon"]))).get("address", {})
        except Exception as e:
            print(f"WARN: reverse geocode failed for {p['name']}: {e}", file=sys.stderr)
            return addr
        states = {"Queensland": "QLD", "New South Wales": "NSW", "Victoria": "VIC"}
        street = " ".join(x for x in (a.get("house_number"), a.get("road")) if x)
        town = a.get("suburb") or a.get("town") or a.get("city") or a.get("village") or a.get("hamlet") or ""
        cache[key] = {"street": street, "town": town, "state": states.get(a.get("state"), a.get("state", "")),
                      "postcode": a.get("postcode", "")}
        with open(GEOCODE_CACHE, "w") as f:
            json.dump(cache, f, indent=1, sort_keys=True)
    c = cache[key]
    street = addr or c["street"]
    tail = " ".join(x for x in (c["state"], c["postcode"]) if x)
    return ", ".join(x for x in (street, f"{c['town']} {tail}".strip()) if x) or None


def stop_type(name):
    n = name.lower()
    if n == "home":
        return "home"
    if "supercharg" in n:  # My Map pins are named "Supercharging"
        return "supercharger"
    if OVERNIGHT.search(name):
        return "overnight"
    return "stop"


def short_place(addr):
    m = re.search(r",\s*([A-Za-z .'-]+?)\s+(QLD|NSW|VIC)\b", addr or "")
    return m.group(1).strip() if m else None


def main():
    offline = "--offline" in sys.argv
    if offline:
        raw = open(KML_FILE, "rb").read()
    else:
        raw = fetch(KML_URL)
        with open(KML_FILE, "wb") as f:
            f.write(raw)
    title, folders = parse_kml(raw)
    pin_f = [f for f in folders if f["pins"] and not f["lines"]]
    dir_f = [f for f in folders if f["lines"]]
    assert len(pin_f) == 2 and len(dir_f) == 2, [f["name"] for f in folders]
    out_pins, ret_pins = pin_f
    out_dir, ret_dir = dir_f
    # addresses: directions-layer waypoints are named by address
    addr_pts = [p for f in dir_f for p in f["pins"]]

    def address_for(p):
        best = min(addr_pts, key=lambda a: haversine_km(p["lat"], p["lon"], a["lat"], a["lon"]))
        d = haversine_km(p["lat"], p["lon"], best["lat"], best["lon"])
        return best["name"] if d < 0.4 and re.search(r"\d", best["name"]) else None

    out_line = out_dir["lines"][0]["coords"]
    ret_line = ret_dir["lines"][0]["coords"]
    filled = None
    # return directions start somewhere other than the first return pin -> fill the gap
    first = ret_pins["pins"][0]
    if haversine_km(first["lat"], first["lon"], ret_line[0][0], ret_line[0][1]) > 1.0:
        url = OSRM.format(first["lon"], first["lat"], ret_line[0][1], ret_line[0][0])
        try:
            r = json.loads(fetch(url))["routes"][0]
            gap = [[la, lo] for lo, la in r["geometry"]["coordinates"]]
            filled = {"from": first["name"], "to": "start of return directions layer",
                      "km": round(r["distance"] / 1000, 1), "source": "OSRM (router.project-osrm.org)"}
        except Exception as e:  # fall back to a straight line rather than nothing
            print(f"WARN: OSRM gap fill failed: {e}", file=sys.stderr)
            gap = [[first["lat"], first["lon"]]]
            filled = {"from": first["name"], "to": "start of return directions layer",
                      "source": "straight line (OSRM failed)"}
        prev = None
        if os.path.exists(os.path.join(HERE, "route.json")) and filled["source"].startswith("straight"):
            prev = json.load(open(os.path.join(HERE, "route.json"))).get("gap_coords")
        gap = prev or gap
        ret_line = gap + ret_line
        gap_coords = gap
    else:
        gap_coords = None

    legs_def = [("out", "Outbound: Westbrook \u2192 Geelong", "#1565c0", out_line, out_pins["pins"], out_dir["pins"]),
                ("ret", "Return: Geelong \u2192 Westbrook", "#7b1fa2", ret_line, ret_pins["pins"],
                 ([first] if gap_coords else []) + ret_dir["pins"])]
    legs, days, stops = [], [], []
    all_pins = out_pins["pins"] + ret_pins["pins"]
    seq = 0
    for leg_id, leg_name, color, line, pins, waypoints in legs_def:
        cum = cumkm(line)
        # Every pass of the line within 0.5 km of a waypoint/pin is a stop (the return trip
        # passes the Coffs Harbour Supercharger twice). Ordered by distance along the line.
        located = []
        for p in waypoints + pins:
            near = [i for i in range(len(line)) if haversine_km(p["lat"], p["lon"], line[i][0], line[i][1]) < 0.5]
            groups = []
            for i in near:
                if groups and cum[i] - cum[groups[-1][-1]] < 20:
                    groups[-1].append(i)
                else:
                    groups.append([i])
            if not groups:
                i, d = nearest_idx(line, p["lat"], p["lon"])
                print(f"WARN: {p['name']} is {d:.1f} km from the {leg_id} line", file=sys.stderr)
                groups = [[i]]
            for g in groups:
                i = min(g, key=lambda k: haversine_km(p["lat"], p["lon"], line[k][0], line[k][1]))
                located.append((i, p))
        located.sort(key=lambda x: cum[x[0]])
        merged = []  # merge waypoint + pin describing the same place/pass
        for i, p in located:
            if merged and abs(cum[i] - cum[merged[-1][0]]) < 1.0 and \
                    haversine_km(p["lat"], p["lon"], merged[-1][1][0]["lat"], merged[-1][1][0]["lon"]) < 0.4:
                merged[-1][1].append(p)
            else:
                merged.append((i, [p]))
        leg_stops = []
        for i, ps in merged:
            named = [p for p in ps if not re.match(r"^\d", p["name"])]
            addrs = [p["name"] for p in ps if re.match(r"^\d", p["name"])]
            if not addrs:  # e.g. return "Home" pin -> outbound "18 Westfield Dr" waypoint
                addrs = [a["name"] for a in addr_pts if re.match(r"^\d", a["name"])
                         and haversine_km(a["lat"], a["lon"], ps[0]["lat"], ps[0]["lon"]) < 0.4]
            if not named:  # e.g. "6 Malone St" -> the Bay View Apartments pin
                named = [p for p in all_pins if not re.match(r"^\d", p["name"])
                         and haversine_km(p["lat"], p["lon"], ps[0]["lat"], ps[0]["lon"]) < 0.4]
            name = named[0]["name"] if named else addrs[0]
            desc = next((p["desc"] for p in ps if p["desc"]), None)
            p0 = (named or ps)[0]
            seq += 1
            address = full_address(addrs[0] if addrs else None, p0)
            if stop_type(name) == "supercharger":
                name = f"Tesla Supercharger \u2013 {short_place(address) or 'unknown'}"
            st = {"seq": seq, "name": name, "address": address,
                  "lat": round(p0["lat"], 6), "lon": round(p0["lon"], 6), "type": stop_type(name),
                  "leg": "outbound" if leg_id == "out" else "return", "leg_km": round(cum[i]), "note": desc}
            leg_stops.append((i, st))
        # The first return stop is the Geelong accommodation (same place as Bay View Apartments)
        if leg_id == "ret" and leg_stops[0][1]["type"] == "stop":
            leg_stops[0][1]["type"] = "overnight"
            leg_stops[0][1]["note"] = leg_stops[0][1]["note"] or "Geelong accommodation (Bay View Apartments, Unit 610)"
        stops.extend(st for _, st in leg_stops)
        # day legs end at each overnight stop / home
        breaks = [0] + [i for i, st in leg_stops if st["type"] in ("overnight", "home") and 0 < i] + [len(line) - 1]
        breaks = sorted(set(breaks))
        names = {i: st for i, st in leg_stops}
        start_name = leg_stops[0][1]
        n = 0
        for a, b in zip(breaks, breaks[1:]):
            if cum[b] - cum[a] < 5:
                continue
            n += 1
            fa = names.get(a) or min(leg_stops, key=lambda x: abs(x[0] - a))[1]
            fb = names.get(b) or min(leg_stops, key=lambda x: abs(x[0] - b))[1]
            def place(st):
                return short_place(st["address"]) or st["name"]
            seg = line[a:b + 1]
            days.append({"id": f"{leg_id}{n}", "leg": leg_id,
                         "name": f"{'Out' if leg_id == 'out' else 'Return'} day {n}: {place(fa)} \u2192 {place(fb)}",
                         "from": fa["name"], "to": fb["name"], "km_start": round(cum[a], 1),
                         "km": round(cum[b] - cum[a]),
                         "coords": [[round(x, 5), round(y, 5)] for x, y in simplify(seg, 0.03)]})
        legs.append({"id": leg_id, "name": leg_name, "color": color, "km": round(cum[-1])})

    now = dt.datetime.now(ZoneInfo("Australia/Brisbane"))
    route = {"title": title, "source": {"mid": MID, "viewer": VIEWER, "kml": KML_URL},
             "built_at": now.isoformat(timespec="seconds"),
             "note": "Built by build_route.py from the Google My Map. Day legs end at overnight stops. "
                     "coords are [lat, lon], simplified to ~30 m.",
             "filled_gap": filled, "gap_coords": gap_coords,
             "legs": legs, "days": days, "stops": stops}
    with open(os.path.join(HERE, "route.json"), "w") as f:
        json.dump(route, f, separators=(",", ":"), ensure_ascii=False)
    with open(os.path.join(HERE, "route-stops.json"), "w") as f:
        json.dump([{k: s[k] for k in ("seq", "name", "address", "lat", "lon", "type", "leg", "leg_km", "note")}
                   for s in stops], f, indent=1, ensure_ascii=False)
        f.write("\n")
    for d in days:
        print(f"{d['id']:5} {d['km']:5} km  {d['name']}  ({len(d['coords'])} pts)")
    for s in stops:
        print(f"{s['seq']:3} {s['leg']:8} {s['leg_km']:5} km {s['type']:12} {s['name']} | {s['address']}")
    if filled:
        print("gap filled:", filled)


if __name__ == "__main__":
    main()
