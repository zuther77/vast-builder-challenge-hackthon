"""VSS client interface and the fixed marking taxonomy.

This is the only file that talks to the video search backend. The UI never
sees raw API shapes; main.py flattens client responses into UI findings.

Mock responses mirror the real VSS search API contract (HANDOFF section 4)
so LiveVSSClient is a drop-in replacement for MockVSSClient.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# Taxonomy (HANDOFF section 1) -- single source of truth. Import these
# constants everywhere; never re-type the strings in templates or app code.
# ---------------------------------------------------------------------------

MARKING_TYPES = [
    "crosswalk",
    "school_crossing",
    "stop_line",
    "yield_line",
    "word_text",
    "bus_box",
    "hov_diamond",
    "directional_arrow",
]

CONDITIONS = [
    "intact",
    "faded",
    "partial",
    "missing_remnant",
]

NON_INTACT_CONDITIONS = ["faded", "partial", "missing_remnant"]

# Risk weight per condition (HANDOFF section 3, object exposure histogram):
# risk = marking condition weight x pedestrian exposure. Tunable.
CONDITION_RISK_WEIGHTS = dict(zip(CONDITIONS, [1, 2, 3, 4]))

# Object classes counted per segment (YOLO detections). Tunable and the
# single source of truth for mock object_counts and the exposure histogram.
OBJECT_CLASSES = [
    "person",
    "car",
    "truck",
    "bus",
    "bicycle",
    "motorcycle",
]

# Re-ingest tag separating audit rows from the base index. The live client
# scopes its broad corpus fetch to this tag.
AUDIT_TAG = "road_marking_audit"

# Broad query the live client uses to pull the audited corpus in one fetch.
# Sighting-level filters (type, condition, street, confidence) are applied
# locally after the fetch, exactly like the mock client.
DEFAULT_SEARCH_QUERY = "painted road markings on the asphalt"
DEFAULT_TOP_K = 100  # /search caps top_k at 100; tunable via VSS_TOP_K

# Caption aliases accepted by parse_sightings. Cosmos3-Reason may not echo
# the exact enum token even when prompted to; these map common phrasings to
# the canonical taxonomy. Keys are lowercase, whitespace-collapsed.
TYPE_ALIASES = {
    "crosswalk": "crosswalk",
    "pedestrian crosswalk": "crosswalk",
    "zebra": "crosswalk",
    "zebra crossing": "crosswalk",
    "continental crosswalk": "crosswalk",
    "bike lane": "crosswalk",  # Map bike lane markings if model outputs bike_lane / cycle track
    "bike_lane": "crosswalk",
    "bicycle lane": "crosswalk",
    "school crossing": "school_crossing",
    "school xing": "school_crossing",
    "stop line": "stop_line",
    "stop bar": "stop_line",
    "yield line": "yield_line",
    "shark teeth": "yield_line",
    "give way line": "yield_line",
    "word": "word_text",
    "words": "word_text",
    "word text": "word_text",
    "text": "word_text",
    "painted text": "word_text",
    "pavement text": "word_text",
    "bus box": "bus_box",
    "bus stop box": "bus_box",
    "bus stop": "bus_box",
    "hov diamond": "hov_diamond",
    "diamond": "hov_diamond",
    "hov": "hov_diamond",
    "arrow": "directional_arrow",
    "directional arrow": "directional_arrow",
    "turn arrow": "directional_arrow",
    "lane arrow": "directional_arrow",
}

CONDITION_ALIASES = {
    "intact": "intact",
    "good": "intact",
    "clear": "intact",
    "faded": "faded",
    "worn": "faded",
    "low contrast": "faded",
    "partial": "partial",
    "partially worn": "partial",
    "incomplete": "partial",
    "missing remnant": "missing_remnant",
    "missing": "missing_remnant",
    "remnant": "missing_remnant",
    "ghost": "missing_remnant",
    "absent": "missing_remnant",
}


def _normalize(text):
    """Lowercase, collapse whitespace, strip bullets/markdown noise."""
    cleaned = text.strip().strip("*-•> ").strip()
    return " ".join(cleaned.lower().split())


def _canonical(value, aliases, valid):
    """Map free-form text to a canonical enum value, or None."""
    normalized = _normalize(value)
    if normalized in valid:
        return normalized
    return aliases.get(normalized)


# Sighting-level filter keys understood by search(). Empty list / None / ""
# means "no constraint on this axis".
#   marking_types  -- list of MARKING_TYPES values
#   conditions     -- list of CONDITIONS values
#   camera         -- camera_id string
#   street         -- street name string (or "unknown")
#   min_confidence -- float 0.0-1.0


class VSSClient:
    """Interface shared by the mock and live clients."""

    name = "vss"

    def search(self, filters):
        """Return a VSS search response (HANDOFF section 4 shape).

        filters: dict as described in the module docstring.
        Returns: {"results": [...], "chunk_results": [...], "llm_synthesis": {...}}
        """
        raise NotImplementedError


class MockVSSClient(VSSClient):
    """Serves the deterministic mock corpus from mock_data.py.

    Filters are applied to the prebuilt corpus so the response shape and
    filtering semantics match what LiveVSSClient will ask the real API for.
    """

    name = "mock"

    def search(self, filters):
        import mock_data  # imported lazily; mock_data imports our enums

        filters = filters or {}
        marking_types = set(filters.get("marking_types") or [])
        conditions = set(filters.get("conditions") or [])
        camera = filters.get("camera") or ""
        street = filters.get("street") or ""
        min_confidence = float(filters.get("min_confidence") or 0.0)

        results = []
        for result in mock_data.SEARCH_RESPONSE["results"]:
            if camera and result["camera_id"] != camera:
                continue
            sightings = result["marking_sightings"]
            if marking_types:
                sightings = [s for s in sightings if s["type"] in marking_types]
            if conditions:
                sightings = [s for s in sightings if s["condition"] in conditions]
            if street:
                sightings = [s for s in sightings if s["street"] == street]
            if min_confidence > 0.0:
                sightings = [s for s in sightings if s["confidence"] >= min_confidence]
            if not sightings and (marking_types or conditions or street
                                  or min_confidence > 0.0):
                # Sighting-level filters were requested but nothing matched.
                continue
            results.append(dict(result, marking_sightings=list(sightings)))

        return {
            "results": results,
            "chunk_results": mock_data.build_chunk_results(results),
            "llm_synthesis": mock_data.LLM_SYNTHESIS,
        }


class LiveVSSClient(VSSClient):
    """Phase 2 client against the real VSS stack.

    Implements:
    - JWT login (POST /api/v1/auth/login) with token caching
    - Broad corpus fetch (POST /api/v1/search) with caching
    - Parses captions into marking_sightings using parse_sightings()
    - Fetches object counts from /api/v1/videos/detections (YOLO) with caching
    - Builds stream URLs (/api/v1/videos/stream?source=...&token=...)
    - Computes before/event/after triptych clips
    - Applies local sighting-level filtering identical to MockVSSClient
    - Injects custom transport for network-free testing
    """

    name = "live"

    def __init__(self, base_url=None, username=None, password=None, transport=None):
        self.base_url = (base_url or os.environ.get("VSS_URL") or os.environ.get("INGRESS_URL", "")).rstrip("/")
        self.internal_url = os.environ.get("VSS_INTERNAL_URL", "").rstrip("/") or self.base_url
        self.username = username or os.environ.get("VSS_USERNAME") or os.environ.get("USERNAME", "")
        self.password = password or os.environ.get("VSS_PASSWORD") or os.environ.get("PASSWORD", "")
        self.transport = transport  # Optional fn(method, url, headers, json_data) -> (status, data)

        if not self.transport and not (self.base_url and self.username and self.password):
            raise RuntimeError(
                "LiveVSSClient requires VSS_URL/INGRESS_URL, VSS_USERNAME/USERNAME, "
                "and VSS_PASSWORD/PASSWORD in env or passed to constructor."
            )

        self._token = None
        self._token_time = 0
        self._cached_corpus = None
        self._cache_time = 0
        self._cache_duration = int(os.environ.get("VSS_CACHE_SECONDS", "300"))
        self._detections_cache = {}

    def _http(self, method, path, data=None, headers=None):
        """Invoke transport or urllib.request."""
        url = f"{self.internal_url}{path}" if path.startswith("/") else f"{self.internal_url}/{path}"
        if self.transport:
            return self.transport(method, url, headers=headers or {}, json_data=data)

        import json
        import urllib.request
        import urllib.error

        h = dict(headers or {})
        req_body = None
        if data is not None:
            req_body = json.dumps(data).encode("utf-8")
            if "Content-Type" not in h:
                h["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=req_body, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return resp.getcode(), json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            try:
                err_data = json.loads(raw)
            except Exception:
                err_data = {"detail": raw}
            return e.code, err_data
        except Exception as e:
            return 500, {"detail": str(e)}

    def _login(self):
        """Authenticate and cache JWT."""
        import time
        now = time.time()
        if self._token and (now - self._token_time) < 3000:
            return self._token

        status, body = self._http(
            "POST",
            "/api/v1/auth/login",
            data={"username": self.username, "password": self.password},
        )
        if status != 200:
            raise RuntimeError(f"Login failed (HTTP {status}): {body}")

        self._token = body.get("access_token")
        self._token_time = now
        return self._token

    def _get_detections(self, source, token):
        """Fetch YOLO detections for source clip, cached."""
        if source in self._detections_cache:
            return self._detections_cache[source]

        import urllib.parse
        quoted = urllib.parse.quote(source, safe="")
        status, body = self._http(
            "GET",
            f"/api/v1/videos/detections?source={quoted}",
            headers={"Authorization": f"Bearer {token}"},
        )

        counts = {cls_name: 0 for cls_name in OBJECT_CLASSES}
        if status == 200 and isinstance(body, dict):
            # detections endpoint returns objects or detections list
            objs = body.get("objects") or body.get("detections") or []
            if isinstance(objs, dict):
                for k, v in objs.items():
                    if k in counts and isinstance(v, (int, float)):
                        counts[k] = int(v)
            elif isinstance(objs, list):
                for obj in objs:
                    label = obj.get("label") or obj.get("class")
                    if label in counts:
                        counts[label] += 1

        self._detections_cache[source] = counts
        return counts

    def _fetch_corpus(self):
        """Fetch and structure the full audit corpus once, caching for TTL."""
        import time
        now = time.time()
        if self._cached_corpus and (now - self._cache_time) < self._cache_duration:
            return self._cached_corpus

        token = self._login()
        headers = {"Authorization": f"Bearer {token}"}

        # 1. Broad search query
        top_k = int(os.environ.get("VSS_TOP_K", str(DEFAULT_TOP_K)))
        payload = {
            "query": DEFAULT_SEARCH_QUERY,
            "top_k": top_k,
            "min_similarity": 0.1,
            "include_public": True,
            # Scope to NYC cycling if configured, else default tag
            "metadata_filters": {"location": "new_york", "camera_id": "nyc_bike_gopro-1"}
            if os.environ.get("VSS_SCOPE_NYC_BIKE", "1") == "1" else {},
        }
        # If tag is supported
        audit_tag = os.environ.get("VSS_AUDIT_TAG", "")
        if audit_tag:
            payload["tags"] = [audit_tag]

        status, body = self._http("POST", "/api/v1/search", data=payload, headers=headers)
        if status != 200:
            # Fallback query if broad search failed
            status, body = self._http(
                "POST",
                "/api/v1/search",
                data={"query": "cyclist bike riding street road markings", "top_k": top_k, "include_public": True},
                headers=headers,
            )
            if status != 200:
                raise RuntimeError(f"VSS search failed (HTTP {status}): {body}")

        raw_results = body.get("results") or []
        import urllib.parse

        # Map sources to results for triptych neighbor lookup
        by_source = {r.get("source"): r for r in raw_results if r.get("source")}
        enriched_results = []

        for r in raw_results:
            source = r.get("source", "")
            captions = r.get("reasoning_content") or ""
            sightings = parse_sightings(captions)

            # Metadata resolution with fallbacks
            meta = r.get("metadata") or {}
            camera_id = r.get("camera_id") or meta.get("camera_id") or "nyc_bike_gopro-1"
            location = r.get("location") or meta.get("location") or "new_york"
            captured_at = r.get("captured_at") or meta.get("captured_at") or ""
            orig_vid = r.get("original_video") or meta.get("original_video") or ""
            start_sec = r.get("best_match_start_sec", 0.0)

            # Build event stream URL
            stream_url = None
            if source:
                quoted = urllib.parse.quote(source, safe="")
                stream_url = f"{self.base_url}/api/v1/videos/stream?source={quoted}&token={token}"

            # Detections: use object_counts already in search response if present
            object_counts = {c: 0 for c in OBJECT_CLASSES}
            raw_counts = r.get("object_counts")
            if raw_counts:
                try:
                    parsed_counts = json.loads(raw_counts) if isinstance(raw_counts, str) else raw_counts
                    if isinstance(parsed_counts, dict):
                        for k, v in parsed_counts.items():
                            if k in object_counts and isinstance(v, (int, float)):
                                object_counts[k] = int(v)
                except Exception:
                    pass
            elif source:
                object_counts = self._get_detections(source, token)

            # Triptych neighbor arithmetic (e.g. seg_0005 -> seg_0004, seg_0006)
            before_source, after_source = None, None
            before_stream, after_stream = None, None
            before_start, after_start = None, None

            if "_segment_" in source:
                try:
                    prefix, rest = source.rsplit("_segment_", 1)
                    seg_num_str, total_str = rest.split("_of_", 1)
                    total_ext = total_str.split(".")[0]
                    seg_num = int(seg_num_str)
                    total_segs = int(total_ext)

                    if seg_num > 1:
                        b_source = f"{prefix}_segment_{seg_num - 1:03d}_of_{total_segs:03d}.mp4"
                        if b_source in by_source:
                            before_source = b_source
                            b_quoted = urllib.parse.quote(b_source, safe="")
                            before_stream = f"{self.base_url}/api/v1/videos/stream?source={b_quoted}&token={token}"
                            before_start = max(0.0, float(start_sec) - 5.0)

                    if seg_num < total_segs:
                        a_source = f"{prefix}_segment_{seg_num + 1:03d}_of_{total_segs:03d}.mp4"
                        if a_source in by_source:
                            after_source = a_source
                            a_quoted = urllib.parse.quote(a_source, safe="")
                            after_stream = f"{self.base_url}/api/v1/videos/stream?source={a_quoted}&token={token}"
                            after_start = float(start_sec) + 5.0
                except Exception:
                    pass

            item = {
                "source": source,
                "original_video": orig_vid,
                "camera_id": camera_id,
                "location": location,
                "captured_at": captured_at,
                "similarity_score": r.get("similarity_score", 0.0),
                "reasoning_content": captions,
                "marking_sightings": sightings,
                "object_counts": object_counts,
                "stream_url": stream_url,
                "best_match_start_sec": start_sec,
                "before_source": before_source,
                "before_stream_url": before_stream,
                "before_start_sec": before_start,
                "after_source": after_source,
                "after_stream_url": after_stream,
                "after_start_sec": after_start,
            }
            enriched_results.append(item)

        # Build chunk_results grouped by original_video
        grouped_chunks = {}
        for res in enriched_results:
            ov = res.get("original_video") or "unknown_video"
            if ov not in grouped_chunks:
                grouped_chunks[ov] = {
                    "original_video": ov,
                    "preview_source": res.get("source"),
                    "best_match_start_sec": res.get("best_match_start_sec", 0.0),
                    "matched_segment_count": 0,
                }
            grouped_chunks[ov]["matched_segment_count"] += 1

        self._cached_corpus = {
            "results": enriched_results,
            "chunk_results": list(grouped_chunks.values()),
            "llm_synthesis": body.get("llm_synthesis") or {"response": "Audit summary over fetched segments."},
        }
        self._cache_time = now
        return self._cached_corpus

    def search(self, filters):
        """Filter the cached corpus locally, with identical semantics to MockVSSClient."""
        corpus = self._fetch_corpus()
        filters = filters or {}
        marking_types = set(filters.get("marking_types") or [])
        conditions = set(filters.get("conditions") or [])
        camera = filters.get("camera") or ""
        street = filters.get("street") or ""
        min_confidence = float(filters.get("min_confidence") or 0.0)

        results = []
        for result in corpus["results"]:
            if camera and result["camera_id"] != camera:
                continue
            sightings = result["marking_sightings"]
            if marking_types:
                sightings = [s for s in sightings if s["type"] in marking_types]
            if conditions:
                sightings = [s for s in sightings if s["condition"] in conditions]
            if street:
                sightings = [s for s in sightings if s["street"] == street]
            if min_confidence > 0.0:
                sightings = [s for s in sightings if s["confidence"] >= min_confidence]
            if not sightings and (marking_types or conditions or street or min_confidence > 0.0):
                continue
            results.append(dict(result, marking_sightings=list(sightings)))

        # Regroup chunk results
        grouped_chunks = {}
        for r in results:
            ov = r.get("original_video") or "unknown_video"
            if ov not in grouped_chunks:
                grouped_chunks[ov] = {
                    "original_video": ov,
                    "preview_source": r.get("source"),
                    "best_match_start_sec": r.get("best_match_start_sec", 0.0),
                    "matched_segment_count": 0,
                }
            grouped_chunks[ov]["matched_segment_count"] += 1

        return {
            "results": results,
            "chunk_results": list(grouped_chunks.values()),
            "llm_synthesis": corpus.get("llm_synthesis"),
        }


def _parse_confidence(val):
    """Parse float confidence clamped to [0.0, 1.0], defaulting to 0.5."""
    try:
        c = float(val)
        return max(0.0, min(1.0, c))
    except (ValueError, TypeError):
        return 0.5


def parse_sightings(reasoning_content):
    """Parse marking_sightings out of a Cosmos3-Reason caption (phase 2).

    Phase 2 re-ingests the corpus with the audit prompt (HANDOFF section 6),
    which emits plain-text findings in the form
    TYPE | CONDITION | CONFIDENCE | STREET | NOTE (or structured JSON).
    This parser turns that into the structured sighting dicts the app
    consumes, and belongs here so LiveVSSClient stays a drop-in.

    Args:
        reasoning_content: caption string from a search result.

    Returns:
        List of sighting dicts with keys type, condition, confidence,
        note, street.
    """
    if not reasoning_content or not isinstance(reasoning_content, str):
        return []

    stripped = reasoning_content.strip()
    if not stripped:
        return []

    # Strip markdown code blocks if present (e.g. ```json ... ```)
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        # Remove first line if it starts with ```
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        # Remove last line if it starts with ```
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()

    # 1. Try JSON first if caption starts with [ or {
    if stripped.startswith(("[", "{")):
        try:
            import json
            data = json.loads(stripped)
            items = []
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                for key in ("findings", "sightings", "results", "markings"):
                    if isinstance(data.get(key), list):
                        items = data[key]
                        break
                if not items and "type" in data:
                    items = [data]

            sightings = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                raw_type = str(item.get("type", ""))
                raw_cond = str(item.get("condition", ""))
                canon_type = _canonical(raw_type, TYPE_ALIASES, MARKING_TYPES)
                canon_cond = _canonical(raw_cond, CONDITION_ALIASES, CONDITIONS)
                if not canon_type or not canon_cond:
                    continue
                conf = _parse_confidence(item.get("confidence", 0.5))
                street = str(item.get("street") or "unknown").strip() or "unknown"
                note = str(item.get("note") or "").strip()
                sightings.append({
                    "type": canon_type,
                    "condition": canon_cond,
                    "confidence": conf,
                    "note": note,
                    "street": street,
                })
            if sightings or isinstance(data, (list, dict)):
                return sightings
        except Exception:
            pass

    # 2. Line format: TYPE | CONDITION | CONFIDENCE | STREET | NOTE
    sightings = []
    lines = stripped.splitlines()
    for line in lines:
        cleaned_line = line.strip().strip("*-•> ").strip()
        if not cleaned_line:
            continue
        parts = [p.strip() for p in cleaned_line.split("|")]
        if len(parts) >= 4:
            raw_type = parts[0]
            raw_cond = parts[1]
            canon_type = _canonical(raw_type, TYPE_ALIASES, MARKING_TYPES)
            canon_cond = _canonical(raw_cond, CONDITION_ALIASES, CONDITIONS)
            if not canon_type or not canon_cond:
                continue
            conf = _parse_confidence(parts[2])
            street = parts[3].strip() or "unknown"
            note = " | ".join(parts[4:]).strip() if len(parts) >= 5 else ""
            sightings.append({
                "type": canon_type,
                "condition": canon_cond,
                "confidence": conf,
                "note": note,
                "street": street,
            })

    return sightings
