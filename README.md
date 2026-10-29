# Trip to Victoria: flood & road-closure corridor map

**Live map:** https://alanjhaase-lang.github.io/trip-victoria-flood-map/

This is a self-updating static web map (Leaflet + OpenStreetMap) for one road trip: Westbrook QLD → Dubbo → Albury → Geelong,
then back via Boronia, Beechworth, Gundagai, Gosford and Byron Bay. It was planned for a low-clearance car (Tesla Model Y).
The map shows:

- Bureau of Meteorology **river gauges near the route**, with flood class, tendency and how far each gauge is from the next flood level.
- **Flood-related road closures near the route**.
- The **route**, overnight stops, Tesla Superchargers and home.
- **RainViewer rain radar**.

It is adapted from the Westbrook map (`alanjhaase-lang/flood-map`) and uses the same marker colours, popups, radar and live BOM relay.

> **Disclaimer:** This is an unofficial map made for one trip. Gauge and road data can be delayed, incomplete or wrong, and some
> closures are not shown (council roads, and Victoria unless an API key is set up). **Do not rely on it for safety decisions.**
> Never drive into floodwater. Check [BOM warnings](https://www.bom.gov.au/australia/warnings/),
> [QLDTraffic](https://qldtraffic.qld.gov.au/), [Live Traffic NSW](https://www.livetraffic.com/) and
> [VicTraffic](https://traffic.vicroads.vic.gov.au/). Call the SES on 132 500 for flood help, or 000 in an emergency.
>
> The disclaimer appears on the first visit. Dismissing it is remembered in `localStorage` key `tripmap.disclaimer.v1`, and the *more* link reopens it.

## Using the map

The panel in the top right has these controls:

- **Trip leg selector:** Whole trip, Outbound, Return, or any single day (for example *Out day 2: Dubbo → Albury*).
  - The **◀ day / day ▶** buttons step through the days. **Fit** zooms to the selected leg or day.
  - Clicking a route line also selects that day.
  - Selecting a leg or day fades the rest of the route. It also filters gauges, closures and cameras to the ones near that leg or day.
  - The selected leg or day is stored in localStorage. You can link straight to a day with `#view=out2`, `#view=ret3`, and so on.
- **Summary line:** the number of gauges in flood and roads closed for the current selection.
- **State checkboxes:** QLD / NSW / VIC, with gauge and in-flood counts for each state.
- **Layer checkboxes:**
  - River gauges, with *In flood only*.
  - Flood road closures, with per-state source status and links.
  - QLD flood cameras.
  - Rain radar, with opacity and a 2-hour loop.

Day legs come from the My Map. A day ends at an overnight stop or at home:

| Day | Route | km |
|---|---|---|
| out1 | Westbrook → Dubbo | 703 |
| out2 | Dubbo → Albury | 546 |
| out3 | Albury → Geelong | 388 |
| ret1 | Geelong → Boronia → Beechworth → Gundagai | 609 |
| ret2 | Gundagai → Gosford | 448 |
| ret3 | Gosford → Byron Bay | 689 |
| ret4 | Byron Bay → (Coffs Harbour SC) → Westbrook | 732 |

## Corridor buffers

These are set in `corridor.py` and `update_roads.py`. Distance is measured to the nearest point of the route polyline, per day.
The polyline is simplified to 150 m for the distance calculation.

| Layer | Buffer | Where it is set |
|---|---|---|
| River gauges | **25 km** | `GAUGE_BUFFER_KM` |
| Flood road closures (any vertex of the closure within) | **10 km** | `ROAD_BUFFER_KM` |
| QLD flood cameras | **5 km** | `CAMERA_BUFFER_KM` |

An item can belong to more than one day, because the outbound and return routes share roads. Each item in the JSON carries these fields:

- `days` and `legs`
- `distance_km`
- `nearest_day`
- `leg_km`: the approximate km along that day's leg

## Data sources (feeds wired)

| What | Source | Notes |
|---|---|---|
| Route, stops | Google My Map `mid=1qbtFZr0yQwFXtvUWUALYAr6sXxRSvDc` (KML export) | Built by hand with `build_route.py` into `route.json` and `route-stops.json` |
| Gap fill | [OSRM](https://router.project-osrm.org/) public router | Geelong (Malone St) → 386 Dorset Rd Boronia is **not drawn in the My Map**. It is filled automatically and shown dashed |
| Stop addresses | My Map addresses, else OpenStreetMap Nominatim reverse geocode | Cached in `geocode_cache.json` |
| Gauge readings | BOM river height bulletins, 14 in total: QLD IDQ60293, 60292, 60286, 60285; NSW IDN60140, 60141, 60143, 60145, 60146, 60147; VIC IDV60147, 60150, 60151, 60201 | Bulletin times are in each state's local time (NSW/VIC observe daylight saving). Each station carries `tz` and the page converts per state |
| Gauge coordinates | BOM NFGN ArcGIS service, then BOM QLD section 3 pages, then BOM NSW/VIC regional flood pages (`PopupRiver`) | Stations not found are retried after 7 days |
| Flood levels | BOM gauge plot pages | Cached in `flood_levels.json`. New stations are filled gradually (`LEVELS_NEW_PER_RUN`, default 60 per run) |
| QLD closures + cameras | [QLDTraffic](https://qldtraffic.qld.gov.au/) API v2 events (shared public key, often 429), falling back to `data.qldtraffic.qld.gov.au/events_v2.geojson`, plus `floodcameras.geojson` | © State of Queensland, CC BY 4.0 |
| NSW closures | [Live Traffic NSW](https://www.livetraffic.com/) public hazard feeds `traffic/hazards/flood.json` and `incident.json` | Keyless. Only flood-related items are kept. Severity is "closed" when any lane extent is Closed |
| VIC closures | Transport Victoria Open Data, *Unplanned disruptions – roads v3* (`api.opendata.transport.vic.gov.au/.../roads/disruptions/unplanned/v3`) | **Optional, needs a free API key** (see below). Without one, VIC is shown as "not checked" |
| Radar | [RainViewer](https://www.rainviewer.com/) public API | Past 2 h, free tier |

If a state's road feed fails, the previous items for that state are carried forward and marked stale. The other states are unaffected.

## Known gaps

- **VIC road closures** have no keyless feed. Until `VIC_OPENDATA_KEY` is set, the VIC days (out3, ret1) show no VIC closures. Check
  VicTraffic. The VIC parser is defensive but has **not yet been tested against a live response**. Check the Actions log after adding the key.
- **NSW** only covers what Live Traffic NSW publishes as hazards. Local council road closures (common in floods) are often missing.
- **Live gauge readings between snapshots:** the shared Cloudflare Worker relay currently allows only QLD bulletins
  (IDQ60285/86/87/93). Until the allowlist is extended (see below), NSW and VIC gauges and IDQ60292 come from the 15-minute GitHub snapshot
  only. The page says "N of 14 bulletins live".
- A few gauges have no coordinates anywhere BOM publishes them, so they are not mapped. They are listed in `data.json` → `stations_without_coordinates`.
- Many gauges (especially small NSW/VIC ones) have no official flood levels. They show the bulletin class only, or grey.
- **My Map quirks, mirrored as drawn:**
  - The return directions are missing Geelong → Boronia (auto-filled, dashed).
  - Return day 4 goes from Byron Bay *south* to the Coffs Harbour Supercharger and then north via the Gold Coast and Ipswich (about 460 km of backtracking).
  - If either is not intended, fix the My Map and rebuild the route (below).

## Refreshing

- **Automatic:** `.github/workflows/update.yml` runs every 15 minutes (GitHub may delay schedules). It runs `update_data.py` and
  `update_roads.py`, and commits `data.json`, `stations.json`, `flood_levels.json`, `closures.json` and `cameras.json` only when they
  change. You can also run it manually: Actions → *Update gauge and road data* → *Run workflow*. The page reloads the data every 5 minutes.
- **When the My Map changes** (new stops or route), rebuild the route locally and push:

  ```bash
  python3 build_route.py           # downloads the My Map KML; or: python3 build_route.py --offline (uses trip.kml)
  python3 update_data.py && python3 update_roads.py
  git add trip.kml route.json route-stops.json geocode_cache.json data.json stations.json flood_levels.json closures.json cameras.json
  git commit -m "Rebuild route from My Map" && git push
  ```

  Check `route.json` → `filled_gap`. If the directions are complete it will be null; otherwise the gap is filled by OSRM.
- **Local preview:** `python3 -m http.server 8000`, then open http://localhost:8000/.

Only the Python standard library is used (3.10+). No secrets are stored in the repo.

## Setting up VIC closures (optional)

1. Register (free) at https://opendata.transport.vic.gov.au/ and subscribe to the *Unplanned Disruptions – Roads* API to get a key.
2. In GitHub, go to repo **Settings → Secrets and variables → Actions → New repository secret**. Set the name to `VIC_OPENDATA_KEY` and the value to your key.
3. Run the workflow manually and check that the log for the roads step shows VIC `ok`.

The key is used only server-side, in GitHub Actions. It is never written to the repo or the page.

## Extending the live BOM relay (optional, for live NSW/VIC readings)

The page uses the existing Worker `https://qld-flood-bom-proxy.alanjhaase.workers.dev`, whose source is in
`alanjhaase-lang/sunshine-coast-flood-map` → `bom-proxy/`. The Worker's origin allowlist already includes `https://alanjhaase-lang.github.io`,
so only paths need adding. The change is **additive**: the four existing QLD paths stay, so the Twin Waters and Westbrook maps are unaffected.

```bash
cd sunshine-coast-flood-map
git apply ../trip-victoria-flood-map/bom-proxy-allowlist.patch   # adds 11 /fwo/ID*.html paths to ALLOWED_PATHS
cd bom-proxy && npx wrangler login && npx wrangler deploy
git commit -am "bom-proxy: allow Trip to Victoria bulletins" && git push
```

Verify the change:

- `curl -H 'Origin: https://alanjhaase-lang.github.io' -I https://qld-flood-bom-proxy.alanjhaase.workers.dev/fwo/IDN60147.html` should return `200`.
- The trip map should say "live from BOM via map relay" without "N of 14".

Until then, the map works fine from the 15-minute snapshot.

## Files

| File | Purpose |
|---|---|
| `index.html` | The map (Leaflet, no build step) |
| `corridor.py` | Route corridor geometry: buffers, simplification, distance to each day leg |
| `build_route.py` | My Map KML → `route.json` (day polylines, stops) and `route-stops.json` (ordered stops) |
| `update_data.py` | BOM bulletins → `data.json`. Caches in `stations.json` and `flood_levels.json` |
| `update_roads.py` | QLD/NSW/VIC closures → `closures.json`, and QLD cameras → `cameras.json` |
| `bom-proxy-allowlist.patch` | Optional Worker allowlist change (see above) |
