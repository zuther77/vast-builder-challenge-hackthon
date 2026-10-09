"""Deterministic mock corpus for the Road Marking Audit Dashboard.

Built once at import time with a fixed seed: identical output on every run
and every process. No runtime randomness.

Shape mirrors the real VSS search response (HANDOFF section 4):
    SEARCH_RESPONSE = {"results": [...], "chunk_results": [...],
                       "llm_synthesis": {...}}
Each result carries marking_sightings, the app's structured layer that the
phase-2 re-ingest will parse out of reasoning_content captions.

Corpus properties (HANDOFF section 4):
    - 2 cameras: pie_cam-3 (toronto), nyc_cam-1 (new_york)
    - 8 street names across the two locations
    - ~96 segments, each with 0-3 sightings
    - ~35-40% of sightings are non-intact
    - street attribution imperfect: some sightings are "unknown"
"""

import random
from datetime import datetime, timedelta, timezone

from vss_client import CONDITIONS, MARKING_TYPES, OBJECT_CLASSES

_SEED = 20260902
_rng = random.Random(_SEED)

CAMERAS = [
    {"camera_id": "pie_cam-3", "location": "toronto"},
    {"camera_id": "nyc_cam-1", "location": "new_york"},
]

_CAMERA_WEIGHTS = [0.7, 0.3]  # Toronto PIE drives are the primary corpus

_STREETS_BY_LOCATION = {
    "toronto": ["Don Mills Rd", "Lawrence Ave E", "Eglinton Ave E",
                "Victoria Park Ave", "Kingston Rd"],
    "new_york": ["5th Ave", "Queens Blvd", "Amsterdam Ave"],
}

_SETS_BY_LOCATION = {"toronto": 3, "new_york": 2}
_SEGMENT_TOTAL = 96

# Sighting counts per segment: mostly 1-2, sometimes 0 or 3.
_SIGHTING_COUNT_WEIGHTS = [0.12, 0.44, 0.32, 0.12]

# Condition distribution: ~38% of sightings are non-intact.
_CONDITION_WEIGHTS = [0.70, 0.14, 0.10, 0.06]

# Type distribution: crosswalks and stop lines dominate the drives.
_TYPE_WEIGHTS = [0.26, 0.08, 0.16, 0.05, 0.14, 0.09, 0.05, 0.17]

# Short human-readable phrases used to build captions and notes. Keyed by
# position against the taxonomy enums, which stay the single source of truth.
_TYPE_PHRASES = dict(zip(MARKING_TYPES, [
    "zebra crosswalk",
    "school crossing",
    "stop line",
    "yield line",
    "painted word text",
    "bus stop box",
    "HOV lane diamond",
    "directional lane arrow",
]))
_CONDITION_PHRASES = dict(zip(CONDITIONS, [
    "clearly visible with full geometry",
    "visible but worn, low contrast",
    "geometry incomplete, chunks missing",
    "only ghost remnants left",
]))

_STREET_SIGN_NOTES = [
    "street sign readable in frame",
    "intersection name visible on sign",
    "landmark matched to known block",
]

# Baseline pedestrian activity per street (deterministic, explains person
# counts in the exposure histogram: busier streets get more persons).
_STREET_BUSYNESS = {
    "Don Mills Rd": 6,
    "Lawrence Ave E": 4,
    "Eglinton Ave E": 7,
    "Victoria Park Ave": 8,
    "Kingston Rd": 5,
    "5th Ave": 12,
    "Queens Blvd": 9,
    "Amsterdam Ave": 10,
}

# Sightings that concentrate pedestrians around the marking.
_PEDESTRIAN_MARKINGS = {MARKING_TYPES[0], MARKING_TYPES[1]}  # crosswalk, school_crossing
_TRANSIT_MARKINGS = {MARKING_TYPES[5]}  # bus_box

_UNKNOWN_STREET_CHANCE = 0.15

_CAPTURE_BASE = datetime(2026, 9, 1, 7, 0, 0, tzinfo=timezone.utc)

LLM_SYNTHESIS = {
    "response": (
        "Mock synthesis: the corpus contains road marking sightings across "
        "Toronto PIE and NYC drives, grouped by street for the audit board. "
        "Replace with the real llm_synthesis field from the VSS search API."
    )
}


def _captured_at():
    offset = timedelta(
        days=_rng.randrange(0, 12),
        hours=_rng.randrange(7, 12),
        minutes=_rng.randrange(0, 60),
        seconds=_rng.randrange(0, 60),
    )
    return (_CAPTURE_BASE + offset).strftime("%Y-%m-%dT%H:%M:%SZ")


def _caption(street, sightings):
    """Build the Cosmos3-Reason-style caption for a segment."""
    where = street if street != "unknown" else "street (unattributed)"
    if not sightings:
        return (
            "Pavement clearly visible but no painted road markings found in "
            "this segment on {}.".format(where)
        )
    parts = []
    for s in sightings:
        parts.append("{} {}".format(
            _TYPE_PHRASES[s["type"]], _CONDITION_PHRASES[s["condition"]]))
    return "Segment on {}: {}.".format(where, "; ".join(parts))


def _build_corpus():
    results = []
    for index in range(_SEGMENT_TOTAL):
        camera = _rng.choices(CAMERAS, weights=_CAMERA_WEIGHTS)[0]
        location = camera["location"]
        set_number = index % _SETS_BY_LOCATION[location] + 1
        segment_id = "seg_{:04d}".format(index)
        source = "s3://team-x-vss-chunks-segments/{}/set{:02d}/{}.mp4".format(
            location, set_number, segment_id)
        original_video = "s3://team-x-vss-chunks-segments/{}/{}_set{:02d}.mp4".format(
            location, location, set_number)
        street = _rng.choice(_STREETS_BY_LOCATION[location])

        sightings = []
        for _ in range(_rng.choices(
                range(0, 4), weights=_SIGHTING_COUNT_WEIGHTS)[0]):
            if _rng.random() < _UNKNOWN_STREET_CHANCE:
                sighting_street = "unknown"
            else:
                sighting_street = street
            sighting = {
                "type": _rng.choices(MARKING_TYPES, weights=_TYPE_WEIGHTS)[0],
                "condition": _rng.choices(CONDITIONS,
                                          weights=_CONDITION_WEIGHTS)[0],
                "confidence": round(_rng.uniform(0.42, 0.98), 2),
                "note": "",
                "street": sighting_street,
            }
            phrase = "{} {}".format(_TYPE_PHRASES[sighting["type"]],
                                    _CONDITION_PHRASES[sighting["condition"]])
            if sighting_street != "unknown" and _rng.random() < 0.5:
                phrase += " ({})".format(_rng.choice(_STREET_SIGN_NOTES))
            sighting["note"] = phrase
            sightings.append(sighting)

        results.append({
            "source": source,
            "similarity_score": round(_rng.uniform(0.55, 0.95), 2),
            "reasoning_content": _caption(street, sightings),
            "original_video": original_video,
            "best_match_start_sec": round(_rng.uniform(60.0, 7200.0), 1),
            "camera_id": camera["camera_id"],
            "location": location,
            "captured_at": _captured_at(),
            "tags": ["live_driving", "road_marking_audit"],
            "marking_sightings": sightings,
            # Phase 2: LiveVSSClient fills this with the /videos/stream
            # playback URL (JWT as ?token=). Mock stays None and the UI
            # renders the placeholder frame.
            "stream_url": None,
            # Internal; stripped after the object-counts post-pass.
            "_street": street,
        })
    return results


def _attach_object_counts(segments):
    """Deterministic YOLO-style object_counts per segment.

    Explainable formula: person count = street busyness baseline, plus
    bonuses for pedestrian-crossing markings (crosswalk / school_crossing)
    and transit markings (bus_box), plus small seeded jitter. Vehicle
    classes scale with the same baseline. Runs as a post-pass so the
    sighting distributions built above stay untouched.
    """
    for segment in segments:
        present = {s["type"] for s in segment["marking_sightings"]}
        busy = _STREET_BUSYNESS[segment["_street"]]
        persons = busy + _rng.randrange(0, 4)
        if present & _PEDESTRIAN_MARKINGS:
            persons += 6
        if present & _TRANSIT_MARKINGS:
            persons += 4
        segment["object_counts"] = {
            "person": persons,
            "car": 10 + busy + _rng.randrange(0, 18),
            "truck": _rng.randrange(0, 7),
            "bus": (2 + _rng.randrange(0, 3)
                    if present & _TRANSIT_MARKINGS
                    else _rng.randrange(0, 3)),
            "bicycle": (2 + _rng.randrange(0, 3)
                        if present & _PEDESTRIAN_MARKINGS
                        else _rng.randrange(0, 3)),
            "motorcycle": _rng.randrange(0, 3),
        }


def _attach_triptych(segments):
    """Attach before/event/after evidence fields per segment.

    Neighbors are the adjacent segments within the same set (same
    original_video), clamped at set boundaries: the first segment of a set
    has no before clip, the last has no after clip. Each clip carries its
    source, stream_url (None in mock; the live client fills
    /api/v1/videos/stream URLs) and captured_at / start_sec timestamps, so
    the live swap stays data-only.
    """
    by_set = {}
    for segment in segments:
        by_set.setdefault(segment["original_video"], []).append(segment)
    for group in by_set.values():
        for position, segment in enumerate(group):
            before = group[position - 1] if position > 0 else None
            after = group[position + 1] if position + 1 < len(group) else None
            for role, clip in (("before", before), ("event", segment),
                               ("after", after)):
                segment["{}_source".format(role)] = (
                    clip["source"] if clip else None)
                segment["{}_stream_url".format(role)] = (
                    clip.get("stream_url") if clip else None)
                segment["{}_captured_at".format(role)] = (
                    clip["captured_at"] if clip else None)
                segment["{}_start_sec".format(role)] = (
                    clip["best_match_start_sec"] if clip else None)


def build_chunk_results(results):
    """Aggregate search results per original video, VSS chunk_results shape."""
    by_video = {}
    for result in results:
        by_video.setdefault(result["original_video"], []).append(result)
    chunk_results = []
    for original_video, rows in by_video.items():
        chunk_results.append({
            "original_video": original_video,
            "best_match_start_sec": rows[0]["best_match_start_sec"],
            "matched_segment_count": len(rows),
            "preview_source": rows[0]["source"],
        })
    return chunk_results


def corpus_stats():
    """Sanity stats for the generated corpus (used by tests/README checks)."""
    sightings = [s for r in SEGMENTS for s in r["marking_sightings"]]
    non_intact = [s for s in sightings if s["condition"] != "intact"]
    unknown = [s for s in sightings if s["street"] == "unknown"]
    persons = sorted(r["object_counts"]["person"] for r in SEGMENTS)
    return {
        "segments": len(SEGMENTS),
        "sightings": len(sightings),
        "non_intact_pct": round(100.0 * len(non_intact) / len(sightings), 1)
        if sightings else 0.0,
        "unknown_street": len(unknown),
        "person_median": persons[len(persons) // 2],
        "full_triptych": sum(1 for r in SEGMENTS
                             if r["before_source"] and r["after_source"]),
    }


SEGMENTS = _build_corpus()
_attach_object_counts(SEGMENTS)
_attach_triptych(SEGMENTS)
for _segment in SEGMENTS:
    del _segment["_street"]
SEARCH_RESPONSE = {
    "results": SEGMENTS,
    "chunk_results": build_chunk_results(SEGMENTS),
    "llm_synthesis": LLM_SYNTHESIS,
}

if __name__ == "__main__":
    # Deterministic build check: run twice, output must be identical.
    print(corpus_stats())
