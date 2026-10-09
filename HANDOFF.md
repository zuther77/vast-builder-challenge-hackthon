# HANDOFF — Road Marking Audit Dashboard

Working doc for any coding agent joining this build. Read this before touching code.
Keep it updated as the build progresses: status, decisions, and what changed.

---

## 1. What we're building

A dashboard/agent for the VAST Builders Challenge that answers:

> **"Which streets have missing or partial road markings?"**

over the team's pre-indexed dashcam video archive (Toronto PIE drives, plus any NYC
street footage confirmed in the index — verify on the VM, see §7).

### Marking taxonomy (fixed vocabulary — use these exact enums everywhere)

**Transverse markings** (crossing the road):
- `crosswalk` — pedestrian crosswalk (zebra/continental)
- `school_crossing` — school crossing (often paired with "SCHOOL" pavement text)
- `stop_line` — stop line
- `yield_line` — yield line (row of triangles)

**Words & symbols on the asphalt:**
- `word_text` — painted words (e.g. "SCHOOL", "BUS", "STOP", "SLOW")
- `bus_box` — bus stop box/text
- `hov_diamond` — HOV lane diamond
- `directional_arrow` — turn/lane arrows

**Condition enum** (per sighting):
- `intact` — clearly visible, full geometry
- `faded` — visible but worn/low contrast
- `partial` — geometry incomplete (chunks missing, worn through)
- `missing_remnant` — only traces/ghost remnants left

**Confidence**: 0.0–1.0 per sighting.

### Demo line (north star)

*"Show me every street segment where the crosswalk is faded or the stop line is gone"*
→ ranked clip evidence + generated per-street audit report.

---

## 2. Status

| Area | State |
|------|-------|
| Area | State |
|------|-------|
| Dashboard UI with **mocked backend** | ✅ DONE — `tools/road-marking-audit/`, verified |
| Caption parser (`parse_sightings`) | 🚧 SCAFFOLDED — alias tables + helpers in `vss_client.py`; function body still a stub. Design in §8.2 |
| `LiveVSSClient` | 🚧 DESIGNED, NOT IMPLEMENTED — stub raises `NotImplementedError`. Design in §8.3 |
| VM probe script (prompt validation) | ❌ NOT WRITTEN — spec in §8.1 |
| Re-ingest of corpus | ❌ NOT STARTED |
| W&B LLM report layer | ❌ NOT STARTED (TODO at `main.py:street_narrative`) |
| K8s deploy (`/app`) | ❌ NOT STARTED (will use `deploy-app-no-registry` skill) |

**Current phase: core implementation, to be finished on the VM.** The dashboard is
complete against mocks; the live wiring is designed (§8) but intentionally
unfinished. Do not rebuild the UI; pick up at §8.

---

## 3. Architecture

```
Browser
  └── Dashboard UI (Jinja templates + vanilla JS)
        └── Flask app (tools/road-marking-audit/main.py)
              └── vss_client.py   ← THE ONLY FILE THAT TALKS TO THE BACKEND
                    ├── MockVSSClient    (now: reads mock JSON)
                    └── LiveVSSClient    (later: JWT login + VSS REST, same interface)
```

Rules:
- All VSS knowledge lives in `vss_client.py`. UI never sees raw API shapes.
- Mock responses are shaped **exactly** like the real VSS API responses (see §4)
  so `LiveVSSClient` is a drop-in.
- Keep total app size under ~1 MiB (ConfigMap limit for the no-registry K8s deploy).
- Runtime: `python:3.12-slim` deployable; only dep: `flask` now, `requests` later.

### App layout

```
tools/road-marking-audit/
├── main.py              # Flask app, routes at /  (Ingress strips /app)
├── vss_client.py         # MockVSSClient / LiveVSSClient interface
├── mock_data.py          # Generates/loads mock segments, mock search responses
├── requirements.txt      # flask
└── templates/
    ├── base.html
    ├── index.html       # filter bar + street-grouped results
    └── report.html      # generated audit report view
```

### Pages

1. **`/` (Findings board)**
   - Filter bar: marking type (multi), condition (multi), camera, street,
     min confidence slider
   - Stats strip: total findings, streets audited, worst street, % non-intact
   - Results **grouped by street**: street card → counts per type × condition →
     finding rows (type chip, condition chip, confidence, camera, timestamp,
     evidence caption, clip placeholder)
2. **`/report` (Audit report)**
   - The "agent" output: per-street ranked findings, severity score, narrative.
   - Now: canned mock text. Later: W&B serverless LLM over matched captions.

### Evidence clips

Mock phase: clip player area shows a placeholder frame with the timestamp + caption.
Real phase: `<video>` pointed at `$INGRESS_URL/api/v1/videos/stream?source=…&token=…`
(JWT passed as query param; skill `retrieval/videos`). The template should already
carry a `stream_url` field so the swap is data-only.

### Triptych evidence (adopted from ARCHITECTURE_REFERENCE playbook example 2)

Each finding row expands to a before / event / after strip — three consecutive
segment clips (~5s each). Markings persist across neighboring segments, so three
corroborating clips both improve the evidence and damp single-segment VLM noise.
Mock: three placeholders with timestamps (`source` of segment N-1 / N / N+1 in the
same set, clamped at set boundaries). Real phase: `/videos/stream` per source —
the data carries all three `stream_url`s from day one.

### Object exposure histogram (adopted from playbook example 3)

A bar histogram of top detected object classes per filtered view (YOLO counts:
person, car, truck, bus, bicycle, motorcycle). Mock: per-segment
`object_counts`. Real: `/dashboard/stats` `objects[]` for the global view and
`/videos/detections` per segment when drill-down is wired.
The report's severity score becomes **risk = marking condition × pedestrian
exposure**, with a "high-pedestrian exposure" flag on findings that cross a
threshold (default: person count above the corpus median).

---

## 4. Mock data contract (mirrors real VSS API)

Mock `search` response follows the real `POST /api/v1/search` shape
(see `.cursor/skills/retrieval/search/SKILL.md`):

```jsonc
{
  "results": [
    {
      "source": "s3://team-x-vss-chunks-segments/pie/set03/seg_0042.mp4",
      "similarity_score": 0.71,
      "reasoning_content": "Faded zebra crosswalk at Don Mills Rd ...",
      "original_video": "s3://.../pie_set03.mp4",
      "best_match_start_sec": 612.0,
      "camera_id": "pie_cam-3",
      "location": "toronto",
      "captured_at": "2026-09-02T14:03:11Z",
      "tags": ["live_driving", "road_marking_audit"],
      "marking_sightings": [          // our structured layer, parsed from captions later
        {
          "type": "crosswalk",
          "condition": "faded",
          "confidence": 0.72,
          "note": "zebra stripes worn at center",
          "street": "Don Mills Rd"
        }
      ]
    }
  ],
  "chunk_results": [ { "original_video": "...", "best_match_start_sec": 612.0,
                       "matched_segment_count": 3, "preview_source": "..." } ],
  "llm_synthesis": { "response": "..." }
}
```

Mock corpus to generate (in `mock_data.py`, deterministic — no randomness at runtime):
- 2 cameras/locations: `pie_cam-3` (toronto), `nyc_cam-1` (new_york, pending index confirm)
- ~8 plausible street names across locations
- ~60–120 segments, each 0–3 sightings, distribution skewed so ~30–40% of
  sightings are non-intact (faded/partial/missing_remnant)
- Street attribution must be present but visibly imperfect (some `street: "unknown"`)
  to reflect the real weak link.

`marking_sightings` is our app's structured layer. In the real build it will be
**parsed out of `reasoning_content`** (the Cosmos3-Reason caption produced by our
re-ingest prompt, §6), then stored/passed through. The parser belongs in
`vss_client.py` behind the same interface.

---

## 5. Real integration plan (phase 2 — not started)

1. **Probe first (on the VM)**: call Cosmos3-Reason directly (skill `gpu/model-smoke-test`,
   port 8001) on ~10 Toronto + NYC segments with the audit prompt. Refine until
   structured output is reliable. **Do not mass re-ingest before this passes.**
2. Confirm NYC footage in the index: `GET /api/v1/dashboard/stats`,
   `GET /api/v1/metadata/values?field=location`, `videos/explore`.
3. **Re-ingest** one PIE set (skill `ingest/reingest-videos`) with the validated prompt;
   one team member owns this. Check caption quality, then scale to remaining sets.
4. Wire `LiveVSSClient`: JWT login (`POST /auth/login`), search
   (`POST /search` with `metadata_filters`, `tags: ["road_marking_audit"]`),
   parse `marking_sightings` from captions, playback URLs from `/videos/stream`.
5. **W&B LLM report**: send matched captions per street to W&B serverless inference
   (`WANDB_*` env) → ranked severity report. Caching layer recommended (same
   query → same report).
6. Deploy on K8s with `deploy-app-no-registry` (Ingress path `/app`).

Key API notes (from skills; full detail in `.cursor/skills/`):
- Everything under `/api/v1` needs `Authorization: Bearer` JWT except `metadata/ingest-config`.
- `/videos/stream` + `/videos/playback-url` take the JWT as `?token=` query param.
- No `/reports`, `/alerts`, `/tags`, `/locations` routes exist. Reports =
  `POST /videos/synthesize`; Q&A = `POST /agent/ask`; filters via `/metadata/*`.
- Credentials/endpoints only from env vars / `/config/<team>.config` on the VM.
  **Never hardcode or print secrets.** This repo must stay secret-free.

---

## 6. Re-ingest audit prompt (DRAFT — validate before use)

> You are auditing road markings for a city infrastructure review. For each clip,
> find all painted road markings. Check specifically for: pedestrian crosswalks
> (zebra/continental stripes), school crossings, stop lines, yield lines, painted
> words (SCHOOL, BUS, STOP, SLOW), bus stop boxes, HOV lane diamonds, and
> directional/turn arrows. For each marking seen, report: (1) marking type using
> these exact labels: crosswalk, school_crossing, stop_line, yield_line, word_text,
> bus_box, hov_diamond, directional_arrow; (2) condition using exactly one of:
> intact, faded, partial, missing_remnant; (3) your confidence 0.0-1.0; (4) a short
> note on what you see. Also report any visible street name signs, intersection
> names, or landmarks that identify the street, and state the street name if known,
> else "unknown". If the pavement area is clearly visible and a marking you would
> expect at that location (e.g. a stop line at a stop sign) is absent, report that
> as a finding with type and condition missing_remnant. Structure the answer as
> plain text listing findings like: `TYPE | CONDITION | CONFIDENCE | STREET | NOTE`.

Validation criteria (from the probe): correct type on ≥8/10 known-marking clips;
condition rating stable across re-runs; street attribution present when a sign
is legible in frame.

---

## 7. Known weak links & mitigations (design around these)

| Risk | Mitigation (already reflected in the design) |
|------|----------------------------------------------|
| No GPS; "which street" attribution is fuzzy | Captions must record street signs; UI groups by street but tolerates `unknown`; drill-down via clip playback |
| VLM condition classification is noisy | Structured enum + confidence per sighting; aggregate multiple segments per street before declaring a finding; W&B LLM second pass for severity |
| Re-ingest is slow (minutes/video) | Probe 10 clips directly first; re-ingest one PIE set, validate, then scale |
| NYC footage may not be in the index | Verify on VM before committing scope; Toronto alone is enough to demo |
| Pivot path if condition ratings fail | Reframe as *marking inventory* (type + presence + clip evidence) — lower risk, still demoable. The UI filters should work for either framing |
| ConfigMap 1 MiB limit | No assets >tiny; no videos in repo; single small JS/CSS in templates |

---

## 8. Core implementation — where it stands and how to finish it (on the VM)

The live wiring was started on a laptop without VM access and deliberately
left at a safe stopping point: **the mock app is untouched and verified; the
parser scaffolding is in `vss_client.py`; the live client and parser bodies
are stubs.** Everything below is designed so the VM agent implements rather
than re-decides.

### 8.1 VM probe script (write first, run before any re-ingest)

`tools/road-marking-audit/probe.py` — CLI, run on the VM (requests lib):
1. Check env: `INGRESS_URL`, `USERNAME`, `PASSWORD`, `COSMOS3_REASON_URL`,
   `GPU_BEARER_TOKEN` (gpu skill says bearer is required on the model endpoints).
2. Login → JWT. Pull ~10 candidate segments via `POST /api/v1/search` with seed
   queries ("crosswalk", "stop line", "painted words on the road") so the probe
   clips actually contain markings; fall back to `/videos/explore`.
3. Fetch each segment's bytes via `/api/v1/videos/stream?source=…&token=…`.
4. For each clip × prompt variant (the §6 line-format prompt AND a JSON-output
   variant) × 2 runs: `POST $COSMOS3_REASON_URL/v1/chat/completions` with
   content = text prompt + `video_url` data URI (base64 mp4), `temperature: 0`.
5. Print a comparison table and write `probe_results.json`: raw outputs, the
   sightings parsed by `parse_sightings`, and a stability check (identical
   parsed output across the 2 runs).

**Gate before re-ingest** (§6): correct type on ≥8/10 clips; condition stable
across runs; street attribution present when a sign is legible. If the
condition gate fails, pivot to marking-inventory framing (§7) — no UI changes
needed.

### 8.2 `parse_sightings` (stub → implement in `vss_client.py`)

Scaffolding already in place: `TYPE_ALIASES` (28 entries), `CONDITION_ALIASES`
(14), `_normalize()`, `_canonical()`. Implement the body to:
- Try JSON first if the caption starts with `[`/`{` (list, or dict with a
  `findings` key); map `type/condition/confidence/street/note` fields through
  `_canonical` — unknown type or condition → drop the item, don't guess.
- Else scan lines, split on `|`, strip bullets/markdown via `_normalize`;
  keep lines with ≥4 fields where fields 0-1 canonicalize. Missing confidence
  → 0.5 clamped to [0,1]; missing street → "unknown"; field 4+ → note.
- Return the mock sighting shape: `{type, condition, confidence, note, street}`.
- A caption with no parseable findings returns `[]` — the UI handles empty
  segments (mock corpus includes them).

### 8.3 `LiveVSSClient` (stub → implement in `vss_client.py`)

Constructor takes `(base_url=None, username=None, password=None,
transport=None)` defaulting from env (`INGRESS_URL`, `USERNAME`, `PASSWORD`);
raise a clear `RuntimeError` if missing. Key decisions, already made:
- **One broad fetch, local filtering.** `search()` must have identical
  semantics to `MockVSSClient.search()` (read it). Fetch once:
  `POST /api/v1/search {query: DEFAULT_SEARCH_QUERY, top_k: DEFAULT_TOP_K,
  min_similarity: 0.1, tags: [AUDIT_TAG]}`, cache for `VSS_CACHE_SECONDS`
  (default 300), then apply the sighting-level filters locally exactly like
  the mock. The UI cannot tell the clients apart.
- **Inject a `transport(method, url, headers, json) -> (status, data)`** so
  tests run without network; default wraps `requests`. Use it for everything
  including login (`POST /api/v1/auth/login` → `access_token`, cached).
- **Enrich each raw result to the §4 contract**: `marking_sightings` via
  `parse_sightings(reasoning_content)`; `object_counts` via
  `GET /api/v1/videos/detections?source=…` (404 → all-zero counts, per the
  retrieval/videos skill; cache per source); `stream_url` =
  `/api/v1/videos/stream?source=<url-quoted>&token=<JWT>`.
- **Metadata fallbacks**: camera_id/location/captured_at may sit at the top
  level or in a `metadata` sub-dict — read `result.get(x) or
  (result.get("metadata") or {}).get(x) or default`. Verify actual shape on
  the VM and simplify.
- **Triptych**: event = the segment itself; before/after = filename
  arithmetic (`seg_0042.mp4` ± 1, preserve zero padding) **looked up in the
  fetched corpus only** — if the neighbor wasn't returned by the broad fetch,
  the slot stays `None` (never fabricate stream URLs). Note: real segment
  numbering may be non-contiguous (skips) — check on the VM; if so, prefer the
  parent timeline (`/tools/segments?original_video=`) for neighbors.
- Build `chunk_results` locally per `original_video` (mirror
  `mock_data.build_chunk_results`); pass through the real `llm_synthesis`
  if present.
- `top_k` caps at 100 per the API — if the re-ingested corpus exceeds that,
  either paginate queries by camera/metadata filters or accept the cap for
  the demo. Decide on the VM once counts are known.

### 8.4 Tests to write with the fake transport (no network)

`tests/test_core.py`, stdlib `unittest`:
- `parse_sightings`: line format, alias phrasings, junk lines dropped,
  JSON mode, empty caption, invalid confidence.
- `LiveVSSClient` with a fake transport: login cached; contract shape
  matches §4 (all keys `flatten_findings` in `main.py` consumes); local
  filtering mirrors mock semantics; detections 404 → zeros; stream_url
  contains quoted source + token; triptych neighbor arithmetic with
  boundary clamping.
- Parity test: run the same assertions over `MockVSSClient` output so mock
  and live can't drift.

### 8.5 Remaining VM sequence (after §8.1-8.4)

Re-ingest with the validated prompt (§6, `tags: ["road_marking_audit"]`) →
set `VSS_CLIENT=live` and sanity-check the dashboard against the real index →
replace the canned narrative at `main.py:street_narrative` with W&B serverless
inference (`WANDB_*` env; cache by query) → deploy with the
`deploy-app-no-registry` skill (ConfigMap from `tools/road-marking-audit/`,
~76 KiB + new code, far under the 1 MiB limit; add `requests` to
`requirements.txt`).

## 9. Conventions for agents

- Python 3.12, stdlib + flask only (requests added in phase 2).
- Deterministic mock data (seeded / precomputed literal) — no runtime randomness.
- The taxonomy enums in §1 are the single source of truth; import from
  `vss_client.py` constants, never re-type strings in templates.
- No secrets, no real credentials, no team names in this repo.
- Match existing repo style; minimal diffs; no new top-level dirs outside
  `tools/road-marking-audit/`.

## 10. Changelog

- 2026-10-09: HANDOFF created. Dashboard build started (mock backend) via sub-agent.
- 2026-10-09: Dashboard built and verified. 96 mock segments / 147 sightings /
  38.1% non-intact / 18 `unknown`-street sightings, deterministic (seed 20260902).
  Verified: `/health`, `/`, `/report` 200; filtering (condition, marking_type,
  camera, min_confidence, street) all correct; bad params → 400;
  `LiveVSSClient.search()` raises `NotImplementedError`. Build deviations
  (accepted):
  1. `stream_url: null` added to mock search results so the live swap is data-only
     (template already renders `<video>` when it is set).
  2. `/api/findings` returns a flattened findings list + stats, not the raw
     search contract — keeps "UI never sees raw API shapes" true; the client-internal
     shape is still the §4 contract.
  3. `/report` narrative is templated from each street's real counts (canned for
     now), TODO for W&B LLM in `main.py:street_narrative`. Ranked report excludes
     `unknown`-street sightings (shown as an unattributed stat instead).
  Phase 2 next: probe prompt on VM → re-ingest → LiveVSSClient → W&B report →
  K8s deploy. See §5 and §6.
- 2026-10-09 (build round 2): Triptych evidence + object exposure histogram
  implemented against the mock backend. Triptych: `before/event/after_source`
  + `*_stream_url` (null in mock) + per-clip timestamps on every search
  result, set-adjacent neighbors clamped at set boundaries (86/96 segments
  have all three clips). Board rows expand on click. Object exposure:
  deterministic per-segment `object_counts` (person/car/truck/bus/bicycle/
  motorcycle; person = street busyness + 6 for crosswalk/school_crossing + 4
  for bus_box + jitter), CSS-only histogram in `/api/findings.histogram`
  for the filtered view. Report ranking switched severity → risk =
  `CONDITION_RISK_WEIGHTS` (in `vss_client.py`: intact 1 / faded 2 / partial 3 /
  missing_remnant 4) x segment person count; findings above the corpus median
  person count (12) flagged high-pedestrian-exposure. Determinism re-verified
  across two server processes; corpus sighting distributions unchanged.
- 2026-10-09 (build round 3, halted per team decision): Core implementation
  started on the laptop, stopped at a safe point for VM handoff. Added to
  `vss_client.py`: `AUDIT_TAG`, `DEFAULT_SEARCH_QUERY`, `DEFAULT_TOP_K`,
  `TYPE_ALIASES` (28), `CONDITION_ALIASES` (14), `_normalize`, `_canonical`.
  `parse_sightings` and `LiveVSSClient` bodies are **still stubs** (verified:
  module imports clean, mock app behavior unchanged). Designs for finishing
  them, the probe script spec, and the test plan are in §8 — implement there,
  don't re-decide. HANDOFF sections renumbered (Conventions → §9, this
  changelog → §10). Verification: `vss_client.py` imports clean (28 type
  aliases, 14 condition aliases); mock app behavior unchanged.
