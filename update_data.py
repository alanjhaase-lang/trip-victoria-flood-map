#!/usr/bin/env python3
"""Fetch BOM river height bulletins for QLD, NSW and VIC, join station coordinates,
keep gauges within GAUGE_BUFFER_KM of the planned "Trip to Victoria" route
(route.json, built by build_route.py) and write data.json.

Standard library only.
"""
import datetime as dt
import html
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from corridor import Corridor, GAUGE_BUFFER_KM, haversine_km  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
BOM = "https://www.bom.gov.au"

# Bulletin times are local clock time in the issuing state (QLD has no daylight
# saving; NSW and VIC do).
STATE_TZ = {"QLD": "Australia/Brisbane", "NSW": "Australia/Sydney", "VIC": "Australia/Melbourne"}
PREFIX_STATE = {"IDQ": "QLD", "IDN": "NSW", "IDV": "VIC"}

# BOM river height bulletins whose basins the route crosses or runs beside
# (see README "Bulletins"). All share the same table layout.
BULLETINS = [
    ("IDQ60293", "QLD: Condamine, Myall Creek, Balonne, Maranoa"),
    ("IDQ60292", "QLD: Macintyre, Dumaresq, Weir, Moonie"),
    ("IDQ60286", "QLD: Stanley, Brisbane, Bremer, Lockyer"),
    ("IDQ60285", "QLD: Nerang, Coomera, Logan-Albert"),
    ("IDN60140", "NSW: Tweed, Brunswick, Richmond, Wilsons, Clarence"),
    ("IDN60141", "NSW: Bellinger, Nambucca, Macleay, Hastings, Manning, Hunter"),
    ("IDN60143", "NSW: Wyong, Hawkesbury-Nepean, Parramatta, Georges"),
    ("IDN60145", "NSW: Macintyre, Gwydir, Namoi, Peel, Castlereagh"),
    ("IDN60146", "NSW: Macquarie, Bogan, Lachlan"),
    ("IDN60147", "NSW: Murrumbidgee, Murray"),
    ("IDV60147", "VIC: Barwon, Moorabool, Leigh, Glenelg"),
    ("IDV60150", "VIC: Goulburn, Broken"),
    ("IDV60151", "VIC: Upper Murray, Mitta Mitta, Kiewa, Ovens, King"),
    ("IDV60201", "VIC: Werribee, Maribyrnong, Yarra, Dandenong, Bunyip"),
]


def bulletin_state(bid):
    return PREFIX_STATE.get(bid[:3], "QLD")


STATIONS_FILE = os.path.join(HERE, "stations.json")
DATA_FILE = os.path.join(HERE, "data.json")
SECTION3_URL = BOM + "/qld/flood/networks/section3.shtml"


def fetch(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def clean(cell):
    text = re.sub(r"<[^>]+>", " ", cell)
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def parse_bulletin(page, bulletin_id):
    """Return list of dict rows from a BOM river height bulletin page."""
    rows = []
    section = None
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S | re.I):
        th = re.findall(r"<th[^>]*>(.*?)</th>", tr, re.S | re.I)
        if th and len(th) == 1:
            section = clean(th[0])
            continue
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)
        if len(tds) < 9:
            continue
        m = re.search(r'href="([^"]*?\.(\d{6})\.plt\.shtml)"', tr)
        if not m:
            continue
        plot = m.group(1)
        if plot.startswith("/"):
            plot = BOM + plot
        tbl = re.search(r'href="([^"]*?\.tbl\.shtml)"', tr)
        height = clean(tds[3])
        try:
            height_val = float(height)
        except ValueError:
            height_val = None
        rows.append({
            "name": clean(tds[0]),
            "station_type": clean(tds[1]),
            "time": clean(tds[2]),
            "height_m": height_val,
            "height_raw": height,
            "datum": clean(tds[4]),
            "tendency": clean(tds[5]).lower(),
            "crossing": clean(tds[6]),
            "flood_class": clean(tds[7]),
            "station_no": m.group(2),
            "plot_url": plot,
            "table_url": (BOM + tbl.group(1)) if tbl and tbl.group(1).startswith("/") else (tbl.group(1) if tbl else None),
            "bulletin": bulletin_id,
            "section": section,
        })
    return rows


def dms(s):
    """'281960S' / '1521900E' -> signed decimal degrees."""
    s = s.strip()
    hemi, digits = s[-1], s[:-1]
    deg, mins, secs = int(digits[:-4]), int(digits[-4:-2]), int(digits[-2:])
    val = deg + mins / 60 + secs / 3600
    return -val if hemi in "SW" else val


def parse_section3(page):
    """BOM 'Index of Queensland river height stations' -> {station_no: info}."""
    out = {}
    pat = re.compile(r"^\s*(\d{6})\s+(\d{6})?\s+(.{30})\s+(.{23})\s+(.{20})\s+(\d{6,7}[NS])\s+(\d{7,8}[EW])",
                     re.M)
    for m in pat.finditer(html.unescape(page)):
        out[m.group(1)] = {
            "bom_name": m.group(3).strip(),
            "stream": m.group(4).strip(),
            "basin": m.group(5).strip(),
            "lat": round(dms(m.group(6)), 5),
            "lon": round(dms(m.group(7)), 5),
        }
    return out


NFGN_URL = ("https://hosting.wsapi.cloud.bom.gov.au/arcgis/rest/services/flood/"
            "National_Flood_Gauge_Network/FeatureServer/5/query")


def lookup_nfgn(station_nos):
    """BOM National Flood Gauge Network feature service -> {station_no: attrs}."""
    out = {}
    nums = sorted({int(n) for n in station_nos})
    for i in range(0, len(nums), 100):
        chunk = ",".join(str(n) for n in nums[i:i + 100])
        q = urllib.parse.urlencode({
            "where": f"bom_stn_num IN ({chunk})",
            "outFields": "bom_stn_num,awrc_stateid,name,lat,long,state,basin,agency",
            "returnGeometry": "false", "f": "json"})
        data = json.loads(fetch(NFGN_URL + "?" + q))
        for f in data.get("features", []):
            a = f["attributes"]
            if a.get("lat") is not None and a.get("long") is not None:
                out[str(a["bom_stn_num"]).zfill(6)] = a
    return out


# BOM state flood-warning map pages embed PopupRiver(name, stn, lat, lon, ...) per gauge.
REGIONAL_PAGES = (
    [f"/nsw/flood/{p}.shtml" for p in ("northwest", "centralwest", "southwest", "northcoast", "coffsharbour",
                                        "midnorth", "newcastle", "greatersydney", "sydney", "outerwest")] +
    [f"/vic/flood/{p}.shtml" for p in ("north_east", "goulburn_broken", "north_central", "corangamite",
                                        "port_phillip", "melbourne", "west_gippsland")])


def lookup_regional(station_nos):
    want, out = set(station_nos), {}
    pat = re.compile(r"PopupRiver\(\s*\"([^\"]*)\"\s*,\s*'(\d{6})'\s*,\s*'(-?[\d.]+)'\s*,\s*'(-?[\d.]+)'")
    for path in REGIONAL_PAGES:
        try:
            page = html.unescape(fetch(BOM + path, timeout=30))
        except Exception as e:
            print(f"WARN: {path} failed: {e}", file=sys.stderr)
            continue
        for name, no, lat, lon in pat.findall(page):
            if no in want and no not in out:
                out[no] = {"bom_name": name.strip(), "lat": round(float(lat), 5), "lon": round(float(lon), 5),
                           "source_url": BOM + path}
        if want <= set(out):
            break
    return out


def lookup_authoritative(station_nos, names):
    """Coordinates from BOM NFGN, then BOM Qld section-3 index, then BOM NSW/VIC
    regional flood map pages. Stations still unknown get a negative cache entry
    (lat None, checked date) so they are only retried weekly."""
    found = {}
    try:
        nf = lookup_nfgn(station_nos)
    except Exception as e:
        print(f"WARN: NFGN lookup failed: {e}", file=sys.stderr)
        nf = {}
    for no, a in nf.items():
        found[no] = {"station_no": no, "name": names.get(no), "bom_name": a.get("name"),
                     "awrc": a.get("awrc_stateid"), "lat": round(a["lat"], 5),
                     "lon": round(a["long"], 5), "source": "bom_nfgn", "geocoded": False}
    rest = [n for n in station_nos if n not in found]
    if rest:
        try:
            idx = parse_section3(fetch(SECTION3_URL))
        except Exception as e:
            print(f"WARN: section3 lookup failed: {e}", file=sys.stderr)
            idx = {}
        for no in rest:
            if no in idx:
                i = idx[no]
                found[no] = {"station_no": no, "name": names.get(no), "bom_name": i["bom_name"],
                             "awrc": None, "lat": i["lat"], "lon": i["lon"],
                             "source": "bom_section3", "geocoded": False}
    rest = [n for n in station_nos if n not in found]
    if rest:
        for no, i in lookup_regional(rest).items():
            found[no] = {"station_no": no, "name": names.get(no), "bom_name": i["bom_name"], "awrc": None,
                         "lat": i["lat"], "lon": i["lon"], "source": "bom_regional_map",
                         "source_url": i["source_url"], "geocoded": False}
    today = dt.datetime.now(ZoneInfo("Australia/Brisbane")).date().isoformat()
    for no in station_nos:
        if no not in found:
            found[no] = {"station_no": no, "name": names.get(no), "lat": None, "lon": None,
                         "source": "not_found", "checked": today}
    return found


# ---------------------------------------------------------------------------
# Official flood classification levels (minor / moderate / major)
# ---------------------------------------------------------------------------
FLOOD_LEVELS_FILE = os.path.join(HERE, "flood_levels.json")
FLOOD_CLASSES = ("minor", "moderate", "major")
LEVELS_NEW_PER_RUN = int(os.environ.get("LEVELS_NEW_PER_RUN", "60"))      # stations with no cached entry fetched per run
LEVELS_REFRESH_PER_RUN = 8    # oldest cached entries re-checked per run ...
LEVELS_MAX_AGE_DAYS = 30      # ... once they are older than this


def parse_flood_levels(page):
    """'Flood Classification Levels' block of a BOM station plot/table page.

    Returns {"minor", "moderate", "major", "datum", "raw"}; levels are floats or
    None. BOM prints AHD levels as e.g. 'Minor: 2.50 m AHD' and local gauge
    height (LGH) levels with no suffix, so datum is 'AHD', 'LGH' or None (no
    levels / mixed). Returns None if the page has no classification block."""
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", page, flags=re.S | re.I)
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text)))
    m = re.search(r"Flood Classification Levels:(.*?)(?:Crossing Levels|Data from|Data as Table|$)", text)
    if not m:
        return None
    block = m.group(1)
    out = {"minor": None, "moderate": None, "major": None, "datum": None,
           "raw": "Flood Classification Levels:" + block.rstrip()}
    datums = set()
    for cls, val, ahd in re.findall(r"\b(Minor|Moderate|Major)\s*:\s*(-?\d+(?:\.\d+)?)\s*m\b(\s*AHD)?",
                                    block, re.I):
        out[cls.lower()] = float(val)
        datums.add("AHD" if ahd.strip() else "LGH")
    out["datum"] = datums.pop() if len(datums) == 1 else None
    return out


def load_flood_levels():
    if os.path.exists(FLOOD_LEVELS_FILE):
        with open(FLOOD_LEVELS_FILE) as f:
            return json.load(f).get("levels", {})
    return {}


def save_flood_levels(levels):
    payload = {
        "note": "Flood classification levels (metres) keyed by BOM station number. Most entries "
                "are scraped from the 'Flood Classification Levels' line of each station's BOM "
                "river height plot page (source='bom_station_plot_page', source_url). "
                "datum='AHD' when BOM prints 'm AHD', otherwise 'LGH' (local gauge height, the "
                "same gauge datum as the bulletin reading). Levels are null where BOM publishes "
                "none. Entries with source='user' are community estimates set by the map "
                "maintainer (not official BOM); the updater never overwrites them. "
                "checked = date last fetched or set (AEST).",
        "levels": dict(sorted(levels.items())),
    }
    with open(FLOOD_LEVELS_FILE, "w") as f:
        json.dump(payload, f, indent=1, ensure_ascii=False)
        f.write("\n")


def fetch_flood_levels(station_no, plot_url):
    lv = parse_flood_levels(fetch(plot_url))
    today = dt.datetime.now(ZoneInfo("Australia/Brisbane")).date().isoformat()
    entry = {"minor": None, "moderate": None, "major": None, "datum": None}
    if lv:
        entry.update({k: lv[k] for k in ("minor", "moderate", "major", "datum")})
    entry.update(source="bom_station_plot_page", source_url=plot_url, checked=today)
    return entry


def update_flood_levels(levels, rows, new_limit=LEVELS_NEW_PER_RUN,
                        refresh_limit=LEVELS_REFRESH_PER_RUN, workers=6):
    """Fetch levels for stations without a cached entry, plus a few stale ones.
    Returns True if anything changed. Failures keep the old entry."""
    from concurrent.futures import ThreadPoolExecutor
    urls = {}
    for r in rows:
        urls.setdefault(r["station_no"], r["plot_url"])
    def is_user(n):
        return (levels.get(n) or {}).get("source") == "user"

    new = [n for n in sorted(urls) if n not in levels][:new_limit]
    today = dt.datetime.now(ZoneInfo("Australia/Brisbane")).date()
    def age(n):
        try:
            return (today - dt.date.fromisoformat(levels[n].get("checked", ""))).days
        except ValueError:
            return 10 ** 6
    # Never re-fetch / overwrite maintainer-set community estimates.
    old = sorted((n for n in urls if n in levels and not is_user(n)
                  and age(n) > LEVELS_MAX_AGE_DAYS),
                 key=age, reverse=True)[:refresh_limit]
    todo = new + old
    if not todo:
        return False
    def one(n):
        try:
            return n, fetch_flood_levels(n, urls[n])
        except Exception as e:
            print(f"WARN: flood levels {n} failed: {e}", file=sys.stderr)
            return n, None
    changed = False
    with ThreadPoolExecutor(workers) as ex:
        for n, entry in ex.map(one, todo):
            if entry is None:
                continue
            if (levels.get(n) or {}).get("source") == "user":
                continue  # keep community estimates
            prev = levels.get(n)
            strip = lambda e: {k: v for k, v in (e or {}).items() if k != "checked"}
            if prev is None or strip(prev) != strip(entry):
                print(f"flood levels {n}: {strip(prev) or None} -> {strip(entry)}")
            levels[n] = entry
            changed = True
    return changed


def bulletin_class(fc):
    k = (fc or "").strip().lower()
    if k.startswith("below"):
        return "Below Flood Level"
    for c in FLOOD_CLASSES:
        if k.startswith(c):
            return c.capitalize()
    return None


def flood_status(row, lv):
    """Flood-class fields for one bulletin row given its cached levels entry.

    delta_m = next_level_m - height_m (positive = metres still to go)."""
    out = {"flood_levels": None, "computed_class": None, "next_class": None,
           "next_level_m": None, "delta_m": None, "above_major_m": None,
           "class_check": None, "flood_levels_note": None}
    have = lv and any(lv.get(c) is not None for c in FLOOD_CLASSES)
    if not have:
        out["flood_levels_note"] = "no official flood levels published by BOM for this gauge"
        return out
    if lv.get("datum") != row.get("datum"):
        # e.g. levels in AHD but bulletin height in local gauge height: a
        # delta would be meaningless, so show nothing rather than a wrong number.
        out["flood_levels_note"] = (f"datum mismatch: levels {lv.get('datum') or 'unknown'}, "
                                    f"reading {row.get('datum') or 'unknown'}; not compared")
        return out
    out["flood_levels"] = {c: lv.get(c) for c in FLOOD_CLASSES}
    out["flood_levels"].update(
        datum=lv["datum"],
        source=lv.get("source"),
        source_url=lv.get("source_url"),
        note=lv.get("note"),
    )
    if lv.get("source") == "user":
        out["flood_levels_note"] = lv.get("note") or (
            "Community estimate (not official BOM flood classification levels)")
    h = row.get("height_m")
    if h is None:
        return out
    known = [(c, lv[c]) for c in FLOOD_CLASSES if lv.get(c) is not None]
    current = "Below Flood Level"
    for c, level in known:
        if round(h - level, 3) >= 0:
            current = c.capitalize()
    out["computed_class"] = current
    nxt = next(((c, level) for c, level in known if round(h - level, 3) < 0), None)
    if nxt:
        out["next_class"] = nxt[0].capitalize()
        out["next_level_m"] = nxt[1]
        out["delta_m"] = round(nxt[1] - h, 2)
    elif lv.get("major") is not None:
        out["above_major_m"] = round(h - lv["major"], 2)
    bc = bulletin_class(row.get("flood_class"))
    if bc is None:
        out["class_check"] = "no bulletin class"
    else:
        out["class_check"] = "agrees" if bc == current else f"differs (bulletin: {bc})"
    return out


def load_stations():
    if os.path.exists(STATIONS_FILE):
        with open(STATIONS_FILE) as f:
            return {s["station_no"]: s for s in json.load(f)["stations"]}
    return {}


def save_stations(stations):
    payload = {
        "note": "Station coordinate lookup keyed by BOM station number. "
                "source=bom_nfgn: BOM National Flood Gauge Network feature service (" + NFGN_URL + "). "
                "source=bom_section3: BOM 'Index of Queensland River Height Stations' (" + SECTION3_URL + "). "
                "source=bom_regional_map: BOM NSW/VIC flood warning map page (source_url). "
                "source=not_found: no BOM coordinates yet (retried weekly). "
                "source=geocoded_*: name-based fallback (OpenStreetMap Nominatim), approximate; geocoded=true.",
        "stations": sorted(stations.values(), key=lambda s: s["station_no"]),
    }
    with open(STATIONS_FILE, "w") as f:
        json.dump(payload, f, indent=1, ensure_ascii=False)
        f.write("\n")


def main():
    corridor = Corridor()
    loc_cache = {}

    def locate(no):
        if no not in loc_cache:
            s = stations.get(no)
            loc_cache[no] = corridor.locate(s["lat"], s["lon"], GAUGE_BUFFER_KM) if s and s.get("lat") is not None else None
        return loc_cache[no]

    stations = load_stations()
    old = None
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE) as f:
                old = json.load(f)
        except (ValueError, OSError):
            old = None
    rows, used, carried, failed = [], [], [], []
    for bid, desc in BULLETINS:
        url = f"{BOM}/fwo/{bid}.html"
        try:
            r = parse_bulletin(fetch(url), bid)
            if not r:
                raise ValueError("no rows parsed")
        except Exception as e:  # keep going if one bulletin fails
            print(f"WARN: {bid} failed: {e}", file=sys.stderr)
            failed.append(bid)
            # Keep the previous readings for this bulletin rather than
            # silently dropping a whole region from the map.
            prev = [x for x in (old or {}).get("stations", []) if x.get("bulletin") == bid]
            if prev:
                carried.extend(prev)
                used.append({"id": bid, "description": desc, "url": url, "rows": len(prev),
                             "stale": True})
            continue
        print(f"{bid}: {len(r)} rows")
        used.append({"id": bid, "description": desc, "url": url, "rows": len(r)})
        rows.extend(r)
    if len(failed) == len(BULLETINS):
        raise SystemExit("all bulletins failed; data.json left untouched")

    # New station numbers we have no coordinates for: look them up in BOM
    # sources (never geocoded automatically) and cache in stations.json.
    today = dt.datetime.now(ZoneInfo("Australia/Brisbane")).date()

    def retry(no):
        s = stations[no]
        if s.get("lat") is not None:
            return False
        try:
            return (today - dt.date.fromisoformat(s.get("checked", ""))).days >= 7
        except ValueError:
            return True
    missing = sorted({r["station_no"] for r in rows if r["station_no"] not in stations or retry(r["station_no"])})
    if missing:
        names = {r["station_no"]: r["name"] for r in rows}
        found = lookup_authoritative(missing, names)
        if found:
            stations.update(found)
            save_stations(stations)
            print(f"looked up {len(found)} station(s): "
                  f"{sum(1 for v in found.values() if v.get('lat') is not None)} found")

    # Official flood classification levels, cached in flood_levels.json.
    flevels = load_flood_levels()
    try:
        mine = [r for r in rows if locate(r["station_no"]) is not None]
        if update_flood_levels(flevels, mine):
            save_flood_levels(flevels)
    except Exception as e:  # never let this stop the gauge update
        print(f"WARN: flood level update failed: {e}", file=sys.stderr)

    seen, out, nocoord = set(), [], []
    for r in rows:
        if r["station_no"] in seen:  # same gauge listed in two bulletins
            continue
        seen.add(r["station_no"])
        s = stations.get(r["station_no"])
        if not s or s.get("lat") is None:
            nocoord.append(f'{r["station_no"]} {r["name"]}')
            continue
        loc = locate(r["station_no"])
        if loc is None:
            continue
        st = bulletin_state(r["bulletin"])
        r.update(lat=s["lat"], lon=s["lon"], coord_source=s["source"],
                 geocoded=bool(s.get("geocoded")), state=st, tz=STATE_TZ[st],
                 distance_km=loc["dist_km"], days=loc["days"], legs=loc["legs"],
                 nearest_day=loc["nearest_day"], leg_km=loc["leg_km"])
        r.update(flood_status(r, flevels.get(r["station_no"])))
        out.append(r)
    for r in carried:  # previous readings of a bulletin that failed this run
        if r["station_no"] not in seen:
            seen.add(r["station_no"])
            r.update(flood_status(r, flevels.get(r["station_no"])))
            out.append(r)
    day_order = {d["id"]: i for i, d in enumerate(corridor.route["days"])}
    out.sort(key=lambda x: (day_order.get(x.get("nearest_day"), 99), x.get("leg_km") or 0))
    in_flood = lambda x: re.match(r"(minor|moderate|major)", (x.get("flood_class") or "").lower())
    days = []
    for d in corridor.route["days"]:
        mine = [x for x in out if d["id"] in (x.get("days") or [])]
        days.append({"id": d["id"], "name": d["name"], "count": len(mine),
                     "in_flood": sum(1 for x in mine if in_flood(x))})
    states = []
    for st in ("QLD", "NSW", "VIC"):
        mine = [x for x in out if x.get("state") == st]
        states.append({"id": st, "count": len(mine), "in_flood": sum(1 for x in mine if in_flood(x))})

    now = dt.datetime.now(ZoneInfo("Australia/Brisbane"))
    data = {
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_at_text": now.strftime("%a %d %b %Y %I:%M %p AEST"),
        "buffer_km": GAUGE_BUFFER_KM,
        "time_note": "Bulletin 'time' is local time in the gauge's state (tz field): QLD AEST all year, "
                     "NSW/VIC AEDT during daylight saving.",
        "days": days,
        "states": states,
        "bulletins": used,
        "stations_without_coordinates": nocoord,
        "flood_levels_source": "BOM station plot pages ('Flood Classification Levels'), cached in "
                               "flood_levels.json; delta_m = next_level_m - height_m (metres to go)",
        "count": len(out),
        "stations": out,
    }
    # Avoid a commit every run when only the timestamp would change.
    if old is not None:
        strip = lambda d: {k: v for k, v in d.items() if not k.startswith("generated_at")}
        if strip(old) == strip(data):
            print("no change in gauge data; data.json left untouched")
            return
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
        f.write("\n")
    print(f"wrote {DATA_FILE}: {len(out)} stations (" +
          ", ".join(f'{x["id"]}={x["count"]}' for x in states) + "); "
          f"{len(nocoord)} bulletin stations lacked coordinates")


if __name__ == "__main__":
    main()
