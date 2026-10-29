#!/usr/bin/env python3
"""Fetch flood-related road closures for the "Trip to Victoria" corridor and
write closures.json (QLD + NSW + VIC) and cameras.json (QLDTraffic flood cameras).

  QLD  QLDTraffic events API (public key) -> static GeoJSON fallback
  NSW  Transport for NSW Live Traffic hazards (flood.json + flood-related incident.json), no key
  VIC  Transport Victoria Open Data "Unplanned disruptions" API v3 -- needs a free key in the
       VIC_OPENDATA_KEY environment variable (GitHub repo secret); skipped when unset.

Only events within ROAD_BUFFER_KM of the planned route (route.json) are kept; cameras
within CAMERA_BUFFER_KM.

Standard library only. A failed fetch keeps the previous file (marked stale).
Files are only rewritten when something other than generated_at* changes, so
the workflow doesn't commit on every run.

Data: QLDTraffic (c) State of Queensland, CC BY 4.0 AU; Live Traffic NSW (c) Transport for NSW,
CC BY 4.0; Transport Victoria open data (c) State of Victoria, CC BY 4.0.
"""
import concurrent.futures as cf
import datetime as dt
import email.utils
import json
import os
import re
import sys
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

from corridor import ROAD_BUFFER_KM, Corridor, densify
from update_data import UA

CORRIDOR = Corridor()
CAMERA_BUFFER_KM = 5.0

HERE = os.path.dirname(os.path.abspath(__file__))
CLOSURES_FILE = os.path.join(HERE, "closures.json")
CAMERAS_FILE = os.path.join(HERE, "cameras.json")
BNE = ZoneInfo("Australia/Brisbane")

# Public key from the QLDTraffic API specification v1.10, section 2.1.2
# (https://qldtraffic.qld.gov.au/media/moreDevelopers-and-Data/qldtraffic-website-api-specification-v1-10.pdf).
# It is shared by everyone and frequently returns HTTP 429, hence the fallback.
API_KEY = "3e83add325cbb69ac4d8e5bf433d770b"
EVENTS_API = "https://api.qldtraffic.qld.gov.au/v2/events?apikey=" + API_KEY
EVENTS_STATIC = "https://data.qldtraffic.qld.gov.au/events_v2.geojson"
CAMERAS_URL = "https://data.qldtraffic.qld.gov.au/floodcameras.geojson"
QLDTRAFFIC = "https://qldtraffic.qld.gov.au/"
ATTRIBUTION = "Road data \u00a9 State of Queensland (QLDTraffic), CC BY 4.0"
ATTRIBUTIONS = {
    "QLD": ATTRIBUTION,
    "NSW": "Road data \u00a9 Transport for NSW (Live Traffic NSW), CC BY 4.0",
    "VIC": "Road data \u00a9 State of Victoria (Department of Transport and Planning), CC BY 4.0",
}
NSW_FEEDS = ["https://www.livetraffic.com/traffic/hazards/flood.json",
             "https://www.livetraffic.com/traffic/hazards/incident.json"]
NSW_LINK = "https://www.livetraffic.com/"
VIC_URL = "https://api.opendata.transport.vic.gov.au/api/opendata/roads/disruptions/unplanned/v3"
VIC_LINK = "https://traffic.vicroads.vic.gov.au/"
LICENCE_URL = "https://creativecommons.org/licenses/by/4.0/"

# Rough Queensland bounding box (lon/lat). Records with any vertex outside it
# are treated as junk (mis-geocoded) and excluded.
QLD_BBOX = (137.9, -29.3, 153.65, -9.0)
# Camera image older than this is reported with its time ("may be offline").
CAM_STALE_HOURS = 3


def now_bne():
    return dt.datetime.now(BNE)


def fetch_json(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def parse_time(s):
    """ISO 8601 (any fraction length, Z or offset) -> aware datetime in Brisbane time, or None."""
    if not s or not isinstance(s, str):
        return None
    s = re.sub(r"(\.\d{6})\d+", r"\1", s.strip()).replace("Z", "+00:00")
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=BNE)
    return t.astimezone(BNE)


def iso(t):
    return t.isoformat(timespec="seconds") if t else None


def text(t):
    return t.strftime("%a %d %b %Y %I:%M %p AEST") if t else None


def tidy(s):
    if s is None:
        return None
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s or None


# Australian road/place names in the feed are plain ASCII; anything else
# (e.g. a Vietnamese street name from a bad reverse geocode) is junk.
NAME_OK = re.compile(r"^[A-Za-z0-9 .,'&()/\-\u2019]+$")


def name_ok(s):
    return bool(s) and bool(NAME_OK.match(s)) and re.search(r"[A-Za-z]{2}", s) is not None


# ---- geometry -------------------------------------------------------------

def parts(g):
    """GeoJSON geometry -> (points, lines) as [lat, lon] lists."""
    pts, lines = [], []
    if not isinstance(g, dict):
        return pts, lines
    t, c = g.get("type"), g.get("coordinates")
    ll = lambda p: [round(float(p[1]), 5), round(float(p[0]), 5)]
    try:
        if t == "GeometryCollection":
            for sub in g.get("geometries") or []:
                p, l = parts(sub)
                pts += p
                lines += l
        elif t == "Point":
            pts.append(ll(c))
        elif t == "MultiPoint":
            pts += [ll(p) for p in c]
        elif t == "LineString":
            lines.append([ll(p) for p in c])
        elif t == "MultiLineString":
            lines += [[ll(p) for p in line] for line in c]
        elif t in ("Polygon", "MultiPolygon"):
            rings = c if t == "Polygon" else [r for poly in c for r in poly]
            lines += [[ll(p) for p in ring] for ring in rings]
    except (TypeError, ValueError, IndexError):
        return [], []
    # drop consecutive duplicate vertices and degenerate lines
    clean_lines = []
    for line in lines:
        out = [p for i, p in enumerate(line) if i == 0 or p != line[i - 1]]
        if len(out) >= 2:
            clean_lines.append(out)
        elif out:
            pts.append(out[0])
    return pts, clean_lines


def in_qld(lat, lon):
    return QLD_BBOX[0] <= lon <= QLD_BBOX[2] and QLD_BBOX[1] <= lat <= QLD_BBOX[3]


def corridor_for(pts, lines, buffer_km=ROAD_BUFFER_KM):
    """Corridor location of a closure (nearest vertex of its densified geometry), or None."""
    verts = list(pts)
    for line in lines:
        verts += densify(line, 0.5)
    return CORRIDOR.locate_many(verts, buffer_km)


def marker_point(pts, lines):
    """A representative [lat, lon]: the vertex nearest the centroid."""
    vs = pts + [p for line in lines for p in line]
    clat = sum(p[0] for p in vs) / len(vs)
    clon = sum(p[1] for p in vs) / len(vs)
    return min(vs, key=lambda p: (p[0] - clat) ** 2 + (p[1] - clon) ** 2)


# ---- closures -------------------------------------------------------------

def is_flood(p):
    return p.get("event_type") == "Flooding" or "flood" in str(p.get("event_due_to") or "").lower()


def severity(impact):
    """'closed' (road closed / all lanes blocked) or 'partial' (lanes affected,
    restricted, partial closures, etc.)."""
    it = (impact.get("impact_type") or "").lower()
    st = (impact.get("impact_subtype") or "").lower()
    if it == "closures" and "partial" not in st:
        return "closed"
    if it == "lanes blocked" and "all" in st:
        return "closed"
    return "partial"


def fetch_events():
    """(features, source dict). Official API first, static feed as fallback."""
    errors = []
    for name, url, shown in (("api", EVENTS_API, "https://api.qldtraffic.qld.gov.au/v2/events"),
                             ("fallback", EVENTS_STATIC, EVENTS_STATIC)):
        try:
            d = fetch_json(url, timeout=60)
            feats = d.get("features") if isinstance(d, dict) else None
            if not isinstance(feats, list) or not feats:
                raise ValueError("no features in response")
            print(f"events: {len(feats)} from {name} ({shown})")
            return feats, {"used": name, "url": shown, "feed_published": iso(parse_time(d.get("published"))),
                           "errors": errors}
        except urllib.error.HTTPError as e:
            errors.append(f"{name}: HTTP {e.code}")
        except Exception as e:  # noqa: BLE001 - any failure -> try the next source
            errors.append(f"{name}: {type(e).__name__}: {e}")
        print(f"WARN: events {errors[-1]}", file=sys.stderr)
    raise RuntimeError("; ".join(errors))


def build_closures(feats):
    now = now_bne()
    out, excluded = [], []
    for f in feats:
        p = (f or {}).get("properties") or {}
        if not is_flood(p):
            continue
        pts, lines = parts(f.get("geometry"))
        verts = pts + [v for line in lines for v in line]
        if not verts:
            continue
        hit = corridor_for(pts, lines)
        if not hit:
            continue
        rs = p.get("road_summary") or {}
        road = tidy(rs.get("road_name"))
        why = None
        if not all(in_qld(*v) for v in verts):
            why = "geometry outside Queensland"
        elif not name_ok(road):
            why = f"implausible road name {road!r}"
        elif (p.get("status") or "Published") != "Published":
            why = f"status {p.get('status')}"
        else:
            end = parse_time((p.get("duration") or {}).get("end"))
            if end and end < now:
                why = f"ended {iso(end)}"
        if why:
            excluded.append({"id": p.get("id"), "road": road, "locality": tidy(rs.get("locality")),
                             "lga": tidy(rs.get("local_government_area")), "reason": why})
            continue
        imp = p.get("impact") or {}
        src = p.get("source") or {}
        upd = parse_time(p.get("last_updated"))
        start = parse_time((p.get("duration") or {}).get("start"))
        link = p.get("web_link") if str(p.get("web_link") or "").startswith("https://") else QLDTRAFFIC
        out.append({
            "id": p.get("id"),
            "road": road,
            "description": tidy(p.get("description")),
            "locality": tidy(rs.get("locality")),
            "lga": tidy(rs.get("local_government_area")),
            "event_type": p.get("event_type"),
            "event_subtype": tidy(p.get("event_subtype")),
            "due_to": tidy(p.get("event_due_to")),
            "impact": tidy(imp.get("impact_subtype")) or tidy(imp.get("impact_type")),
            "impact_type": tidy(imp.get("impact_type")),
            "severity": severity(imp),
            "direction": tidy(imp.get("direction")),
            "advice": tidy(p.get("advice")),
            "information": tidy(p.get("information")),
            "status": p.get("status"),
            "start": iso(start),
            "last_updated": iso(upd),
            "last_updated_text": text(upd),
            "provided_by": tidy(src.get("provided_by")),
            "source_link": link,
            "api_url": p.get("url"),
            "state": "QLD",
            **corridor_fields(hit),
            "marker": marker_point(pts, lines),
            "geometry": {"points": pts, "lines": lines},
        })
    return out, excluded


def corridor_fields(hit):
    return {"distance_km": hit["dist_km"], "days": hit["days"], "legs": hit["legs"],
            "nearest_day": hit["nearest_day"], "leg_km": hit["leg_km"]}


def sort_items(items):
    order = {d["id"]: i for i, d in enumerate(CORRIDOR.route["days"])}
    items.sort(key=lambda c: (order.get(c.get("nearest_day"), 99), c.get("leg_km") or 0))
    return items


def day_counts(items, extra=None):
    res = []
    for d in CORRIDOR.route["days"]:
        mine = [x for x in items if d["id"] in (x.get("days") or [])]
        row = {"id": d["id"], "name": d["name"], "count": len(mine)}
        if extra:
            row.update(extra(mine))
        res.append(row)
    return res


# ---- NSW (Live Traffic) ---------------------------------------------------

def decode_polyline(s):
    """Google encoded polyline -> [[lat, lon], ...] (precision 5)."""
    pts, i, lat, lon = [], 0, 0, 0
    while i < len(s):
        vals = []
        for _ in range(2):
            shift = result = 0
            while True:
                b = ord(s[i]) - 63
                i += 1
                result |= (b & 0x1f) << shift
                shift += 5
                if b < 0x20:
                    break
            vals.append(~(result >> 1) if result & 1 else result >> 1)
        lat += vals[0]
        lon += vals[1]
        pts.append([round(lat / 1e5, 5), round(lon / 1e5, 5)])
    return pts


def strip_html(s):
    return tidy(re.sub(r"<[^>]+>", " ", s or "").replace("&nbsp;", " ").replace("&amp;", "&"))


def nsw_is_flood(p):
    if (p.get("mainCategory") or "").upper() == "FLOODING":
        return True
    blob = json.dumps({k: p.get(k) for k in ("displayName", "headline", "adviceA", "adviceB", "otherAdvice",
                                               "subCategoryA", "subCategoryB", "roads")}).lower()
    return "flood" in blob


def ms_time(v):
    try:
        return dt.datetime.fromtimestamp(int(v) / 1000, BNE)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def build_nsw():
    out, seen, src = [], set(), []
    for url in NSW_FEEDS:
        d = fetch_json(url)
        feats = d.get("features") if isinstance(d, dict) else None
        if not isinstance(feats, list):
            raise ValueError(f"{url}: no features list")
        src.append({"url": url, "features": len(feats)})
        for f in feats:
            p = (f or {}).get("properties") or {}
            fid = f.get("id")
            if fid in seen or p.get("ended") or not nsw_is_flood(p):
                continue
            pts, _ = parts(f.get("geometry"))
            lines = [ln for ln in (decode_polyline(e.get("coords") or "") for e in p.get("encodedPolylines") or [])
                     if len(ln) >= 2]
            if not pts and not lines:
                continue
            hit = corridor_for(pts, lines)
            if not hit:
                continue
            seen.add(fid)
            roads = p.get("roads") or [{}]
            r0 = roads[0] or {}
            extents = [(l.get("extent") or "") for r in roads for l in (r.get("impactedLanes") or [])]
            closed = any(e.lower() == "closed" for e in extents)
            road = tidy(r0.get("mainStreet"))
            where = " ".join(x for x in (r0.get("locationQualifier"), r0.get("crossStreet")) if x and x.strip())
            upd = ms_time(p.get("lastUpdated"))
            out.append({
                "id": f"nsw-{fid}",
                "road": road,
                "description": tidy(" ".join(x for x in (p.get("displayName"), p.get("headline"), where) if x)),
                "locality": tidy(r0.get("suburb")),
                "lga": tidy(r0.get("region")),
                "event_type": "Flooding" if (p.get("mainCategory") or "").upper() == "FLOODING" else p.get("mainCategory"),
                "event_subtype": tidy(p.get("subCategoryA")) if p.get("subCategoryA") not in (None, "null") else None,
                "due_to": "Flooding",
                "impact": ", ".join(sorted({e for e in extents if e})) or None,
                "severity": "closed" if closed else "partial",
                "advice": tidy(" ".join(x for x in (p.get("adviceA"), p.get("adviceB")) if x and x.strip())),
                "information": strip_html(p.get("otherAdvice")),
                "start": iso(ms_time(p.get("created"))),
                "last_updated": iso(upd),
                "last_updated_text": text(upd),
                "provided_by": "Transport for NSW",
                "source_link": NSW_LINK,
                "state": "NSW",
                **corridor_fields(hit),
                "marker": marker_point(pts, lines),
                "geometry": {"points": pts, "lines": lines},
            })
    return out, src


# ---- VIC (Transport Victoria open data, needs a key) -----------------------

def _first(p, *keys):
    for k in keys:
        v = p.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def build_vic(key):
    req = urllib.request.Request(VIC_URL + "?page=1&limit=0",
                                 headers={"User-Agent": UA, "Accept": "application/json", "KeyID": key})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read().decode("utf-8", errors="replace"))
    feats = d.get("features") if isinstance(d, dict) else None
    if not isinstance(feats, list):
        raise ValueError("no features list in VIC response")
    out = []
    for f in feats:
        p = (f or {}).get("properties") or {}
        blob = json.dumps(p).lower()
        if "flood" not in blob:
            continue
        pts, lines = parts(f.get("geometry"))
        if not pts and not lines:
            continue
        hit = corridor_for(pts, lines)
        if not hit:
            continue
        impact = _first(p, "impact", "impactType", "closureType", "status") or {}
        impact_txt = json.dumps(impact).lower() if not isinstance(impact, str) else impact.lower()
        closed = ("closed" in impact_txt or "closure" in impact_txt) and "partial" not in impact_txt and "lane" not in impact_txt
        upd = parse_time(_first(p, "lastUpdated", "last_updated", "updated", "modified"))
        road = _first(p, "closedRoadName", "roadName", "road_name", "road", "name")
        out.append({
            "id": f"vic-{_first(p, 'id', 'eventId', 'event_id') or f.get('id')}",
            "road": tidy(road if isinstance(road, str) else json.dumps(road)),
            "description": tidy(_first(p, "description", "details", "eventType", "event_type")),
            "locality": tidy(_first(p, "locality", "suburb", "town")),
            "lga": tidy(_first(p, "lga", "municipality", "region")),
            "event_type": tidy(_first(p, "eventType", "event_type", "type")),
            "due_to": "Flooding",
            "impact": tidy(impact if isinstance(impact, str) else json.dumps(impact)),
            "severity": "closed" if closed else "partial",
            "advice": None,
            "information": None,
            "start": iso(parse_time(_first(p, "created", "startTime", "start"))),
            "last_updated": iso(upd),
            "last_updated_text": text(upd),
            "provided_by": "Department of Transport and Planning (VIC)",
            "source_link": VIC_LINK,
            "state": "VIC",
            **corridor_fields(hit),
            "marker": marker_point(pts, lines),
            "geometry": {"points": pts, "lines": lines},
        })
    return out, [{"url": VIC_URL, "features": len(feats)}]


# ---- cameras --------------------------------------------------------------

def image_info(url):
    """HEAD the image: (ok, last_modified datetime or None, note)."""
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA,
                                     "Referer": "https://alanjhaase-lang.github.io/"})
        with urllib.request.urlopen(req, timeout=15) as r:
            lm = r.headers.get("Last-Modified")
            t = email.utils.parsedate_to_datetime(lm).astimezone(BNE) if lm else None
            return True, t, None
    except Exception as e:  # noqa: BLE001
        return False, None, f"{type(e).__name__}: {e}"


def build_cameras(feats):
    now = now_bne()
    cams = []
    for f in feats:
        p = (f or {}).get("properties") or {}
        pts, _ = parts(f.get("geometry"))
        if not pts or not in_qld(*pts[0]):
            continue
        hit = CORRIDOR.locate(pts[0][0], pts[0][1], CAMERA_BUFFER_KM)
        if not hit:
            continue
        url = str(p.get("image_url") or "").strip()
        if url.startswith("http://"):
            url = "https://" + url[len("http://"):]
        if not url.startswith("https://"):
            continue
        cams.append({
            "id": p.get("id"),
            "name": tidy(p.get("description")),
            "direction": tidy(p.get("direction")),
            "locality": tidy(p.get("locality")),
            "district": tidy(p.get("district")),
            "image_url": url,
            "image_sourced_from": tidy(p.get("image_sourced_from")),
            "info": tidy(p.get("extra_info")),
            **corridor_fields(hit),
            "lat": pts[0][0], "lon": pts[0][1],
        })
    with cf.ThreadPoolExecutor(8) as ex:
        infos = list(ex.map(lambda c: image_info(c["image_url"]), cams))
    for c, (ok, t, note) in zip(cams, infos):
        c["image_ok"] = ok
        if note:
            c["image_error"] = note
        # Only record the image time when it is old: images refresh every
        # 30 min, so a precise time would change (and commit) on every run.
        # The browser always loads the live image.
        if t and now - t > dt.timedelta(hours=CAM_STALE_HOURS):
            c["image_time"] = iso(t.replace(second=0))
            c["image_time_text"] = text(t)
            c["image_stale"] = True
        else:
            c["image_stale"] = False if t else None
    return sort_items(cams)


# ---- output ---------------------------------------------------------------

def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def strip(d):
    return {k: v for k, v in (d or {}).items() if not k.startswith("generated_at")}


def write_if_changed(path, data, old):
    if old is not None and strip(old) == strip(data):
        print(f"no change; {os.path.basename(path)} left untouched")
        return False
    with open(path, "w") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
        f.write("\n")
    print(f"wrote {os.path.basename(path)}")
    return True


def mark_stale(path, old, err):
    """Keep the previous data but flag it stale (written once, not every run)."""
    if old is None:
        print(f"WARN: no previous {os.path.basename(path)} to keep", file=sys.stderr)
        return
    if old.get("stale"):
        print(f"{os.path.basename(path)} already marked stale")
        return
    old["stale"] = True
    old["stale_since"] = iso(now_bne())
    old["stale_reason"] = err
    with open(path, "w") as f:
        json.dump(old, f, indent=1, ensure_ascii=False)
        f.write("\n")
    print(f"marked {os.path.basename(path)} stale: {err}")


def stamp():
    t = now_bne()
    return {"generated_at": iso(t), "generated_at_text": text(t)}


def update_closures():
    """Each state is fetched independently; a failed state keeps its previous
    items (flagged stale) so one bad feed never blanks the others."""
    old = load(CLOSURES_FILE) or {}
    prev = {st: [c for c in old.get("closures", []) if c.get("state", "QLD") == st] for st in ("QLD", "NSW", "VIC")}
    items, excluded, sources = [], [], {}

    def run(st, fn):
        try:
            got, info = fn()
            items.extend(got)
            sources[st] = dict(info, ok=True, stale=False, count=len(got))
        except Exception as e:  # noqa: BLE001
            print(f"WARN: {st} closures failed: {e}", file=sys.stderr)
            was = (old.get("sources") or {}).get(st) or {}
            items.extend(prev[st])
            sources[st] = {"ok": False, "stale": bool(prev[st]), "error": str(e)[:300],
                           "stale_since": was.get("stale_since") or iso(now_bne()), "count": len(prev[st])}

    def qld():
        feats, source = fetch_events()
        got, exc = build_closures(feats)
        excluded.extend(exc)
        return got, source

    run("QLD", qld)
    run("NSW", lambda: (lambda r: (r[0], {"feeds": r[1]}))(build_nsw()))
    key = os.environ.get("VIC_OPENDATA_KEY", "").strip()
    if key:
        run("VIC", lambda: (lambda r: (r[0], {"feeds": r[1]}))(build_vic(key)))
    else:
        sources["VIC"] = {"ok": False, "stale": False, "count": 0, "not_configured": True,
                          "note": "No VIC_OPENDATA_KEY secret: VIC closures not checked. See VicTraffic.",
                          "link": VIC_LINK}
    if not any(v.get("ok") for v in sources.values()):
        print("WARN: every closures feed failed; closures.json left untouched", file=sys.stderr)
        return False
    sort_items(items)
    data = dict(stamp(), **{
        "stale": any(v.get("stale") for v in sources.values()),
        "sources": sources,
        "attributions": ATTRIBUTIONS,
        "licence": LICENCE_URL,
        "buffer_km": ROAD_BUFFER_KM,
        "filter": f"Flood-related events (QLD: event_type 'Flooding' or due to flood; NSW: Live Traffic FLOODING "
                  f"hazards + incidents mentioning flood; VIC: disruptions mentioning flood) with any part within "
                  f"{ROAD_BUFFER_KM:g} km of the planned route. severity 'closed' = road closed / all lanes "
                  f"blocked, 'partial' = anything less. Coordinates [lat, lon].",
        "days": day_counts(items, lambda m: {
            "closed": sum(1 for x in m if x["severity"] == "closed"),
            "partial": sum(1 for x in m if x["severity"] == "partial")}),
        "count": len(items),
        "excluded": excluded,
        "closures": items,
    })
    write_if_changed(CLOSURES_FILE, data, old or None)
    print("closures: " + ", ".join(f"{k}={'ok' if v.get('ok') else 'FAIL/skip'}:{v.get('count')}"
                                   for k, v in sources.items()) + f"; excluded {len(excluded)}")
    return True


def update_cameras():
    old = load(CAMERAS_FILE)
    try:
        d = fetch_json(CAMERAS_URL)
        feats = d.get("features") if isinstance(d, dict) else None
        if not isinstance(feats, list) or not feats:
            raise ValueError("no features in response")
        cams = build_cameras(feats)
    except Exception as e:  # noqa: BLE001
        print(f"WARN: cameras failed: {e}", file=sys.stderr)
        mark_stale(CAMERAS_FILE, old, str(e))
        return False
    data = dict(stamp(), **{
        "stale": False,
        "source": {"url": CAMERAS_URL},
        "attribution": ATTRIBUTION,
        "licence": LICENCE_URL,
        "note": "Images load live from image_url. image_time is only recorded when the image is older than "
                f"{CAM_STALE_HOURS} h at the last check (image_stale=true); image_ok = image URL answered HTTP 200.",
        "buffer_km": CAMERA_BUFFER_KM,
        "days": day_counts(cams),
        "count": len(cams),
        "cameras": cams,
    })
    write_if_changed(CAMERAS_FILE, data, old)
    print(f"cameras: {len(cams)} within {CAMERA_BUFFER_KM:g} km of the route")
    return True


def main():
    ok1 = update_closures()
    ok2 = update_cameras()
    if not (ok1 or ok2):
        raise SystemExit("all road fetches failed; previous files kept")


if __name__ == "__main__":
    main()
