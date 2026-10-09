# Road Marking Audit Dashboard

Flask dashboard answering "Which streets have missing or partial road
markings?" over the team's pre-indexed dashcam video archive. Currently runs
against a fully mocked backend that mirrors the real VSS search API shapes,
so swapping in the live backend is a config change, not a rewrite.

## Run

```
cd tools/road-marking-audit
pip install -r requirements.txt
python main.py
```

Serves on http://localhost:8000 (override with the PORT env var).

- `/` - findings board: filter bar (marking type, condition, camera, street,
  min confidence), stats strip, object exposure histogram, results grouped
  into per-street cards with finding rows. Clicking a finding row expands its
  before / event / after triptych evidence strip (three consecutive segment
  clips within the same set, clamped at set boundaries).
- `/report` - audit report: per-street ranked findings with
  risk = condition weight x pedestrian exposure, high-pedestrian-exposure
  flags, and a narrative summary.
- `/api/findings` - JSON API the board's JS calls. Accepts repeated
  `marking_type` and `condition` params plus `camera`, `street`,
  `min_confidence`; returns `{"findings": [...], "stats": {...},
  "histogram": [...]}` where histogram carries YOLO object counts aggregated
  over the segments in the current view (each segment counted once).
- `/health` - returns `{"status": "ok"}`.

## Backend selection

`VSS_CLIENT` env var selects the client (default `mock`):

```
VSS_CLIENT=mock python main.py   # mocked corpus (default)
VSS_CLIENT=live python main.py   # phase 2; stub raises NotImplementedError
```

## What is mocked

- `mock_data.py` builds a deterministic corpus at import (fixed seed, no
  runtime randomness): 2 cameras (pie_cam-3/toronto, nyc_cam-1/new_york),
  8 street names, 96 segments with 0-3 sightings each, ~35-40% of sightings
  non-intact, some sightings attributed to street "unknown" to reflect the
  real weak link (no GPS; street attribution comes from captions).
- `MockVSSClient.search()` filters that corpus and returns responses in the
  real VSS search contract shape (results with `marking_sightings`,
  `chunk_results`, `llm_synthesis`), so `LiveVSSClient` is a drop-in.
- Each result also carries YOLO-style `object_counts` (person, car, truck,
  bus, bicycle, motorcycle). Person counts are explainable: a per-street
  busyness baseline, +6 for crosswalk / school_crossing sightings, +4 for
  bus_box sightings, plus seeded jitter.
- Clip evidence areas are triptychs: before / event / after sources are the
  neighboring segments in the same set, with `before_stream_url` /
  `event_stream_url` / `after_stream_url` (None in mock) plus per-clip
  timestamps. The UI renders placeholders and switches to real `<video>` tags
  purely from the data once the live backend fills the stream URLs.
- `/report` narratives are canned text generated from street stats; street
  ranking uses risk = `CONDITION_RISK_WEIGHTS` (vss_client.py, tunable) x the
  segment's person count, summed per street. Findings on segments above the
  corpus median person count are flagged high-pedestrian-exposure.

## Phase 2 swap points

1. `vss_client.LiveVSSClient` - implement JWT login (`POST /auth/login`) and
   search (`POST /api/v1/search` with `metadata_filters`,
   `tags: ["road_marking_audit"]`); set `VSS_CLIENT=live`. `requests` gets
   added to requirements at that point.
2. `vss_client.parse_sightings()` - stub now; will parse `marking_sightings`
   out of Cosmos3-Reason captions produced by the re-ingest audit prompt.
3. Triptych + event stream URLs - fill `before/event/after_stream_url` with
   real playback URLs in the live client; no template changes needed.
   Object counts come from `/dashboard/stats` `objects[]` globally and
   `/videos/detections` per segment for drill-down.
4. `main.py street_narrative()` - marked TODO; replace the canned text with
   a W&B serverless LLM call over the matched captions, with caching so the
   same query returns the same report.
5. K8s deploy via `deploy-app-no-registry` (Ingress strips `/app`); the app
   must stay under ~1 MiB total (ConfigMap limit), hence inline CSS/JS and
   no external assets.

## Notes

- Python 3.12 stdlib + flask only; no network calls in mock mode; no secrets.
- The taxonomy enums in `vss_client.py` (MARKING_TYPES, CONDITIONS) are the
  single source of truth; templates and app code import them and never
  hardcode the strings.
