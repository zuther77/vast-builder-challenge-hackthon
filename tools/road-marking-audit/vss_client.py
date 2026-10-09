"""VSS client interface and the fixed marking taxonomy.

This is the only file that talks to the video search backend. The UI never
sees raw API shapes; main.py flattens client responses into UI findings.

Mock responses mirror the real VSS search API contract (HANDOFF section 4)
so LiveVSSClient is a drop-in replacement for MockVSSClient.
"""

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
    """Phase 2 client against the real VSS stack. Not implemented yet.

    Will do: JWT login (POST /auth/login), search (POST /api/v1/search with
    metadata_filters and tags ["road_marking_audit"]), parse marking_sightings
    from captions via parse_sightings(), and build playback URLs from
    /api/v1/videos/stream (JWT passed as ?token= query param).
    """

    name = "live"

    def search(self, filters):
        raise NotImplementedError(
            "LiveVSSClient is phase 2: wire JWT login and POST /api/v1/search "
            "before enabling VSS_CLIENT=live."
        )


def parse_sightings(reasoning_content):
    """Parse marking_sightings out of a Cosmos3-Reason caption (phase 2).

    Phase 2 re-ingests the corpus with the audit prompt (HANDOFF section 6),
    which emits plain-text findings in the form
    TYPE | CONDITION | CONFIDENCE | STREET | NOTE.
    This parser turns that into the structured sighting dicts the app
    consumes, and belongs here so LiveVSSClient stays a drop-in.

    Args:
        reasoning_content: caption string from a search result.

    Returns:
        List of sighting dicts with keys type, condition, confidence,
        note, street.
    """
    raise NotImplementedError(
        "parse_sightings arrives in phase 2 with the re-ingested captions."
    )
