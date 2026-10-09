"""Road Marking Audit Dashboard -- Flask app.

Routes:
    /              findings board (filter bar, stats strip, street cards)
    /report        per-street audit report (risk ranking + narrative)
    /api/findings  JSON API backing the findings board
    /health        health check for the K8s deploy

Backend selection: env var VSS_CLIENT (mock|live), default mock. All backend
access goes through vss_client; swapping in the live backend is a config
change, not a code change.
"""

import os
import statistics

from flask import Flask, jsonify, render_template, request

from vss_client import (
    CONDITIONS,
    CONDITION_RISK_WEIGHTS,
    MARKING_TYPES,
    NON_INTACT_CONDITIONS,
    OBJECT_CLASSES,
    LiveVSSClient,
    MockVSSClient,
)

app = Flask(__name__)

# CSS class per condition, assigned by enum order so templates never hardcode
# condition strings. Colors live in base.html (.cond-0 ... .cond-3).
COND_CLASS = {c: "cond-{}".format(i) for i, c in enumerate(CONDITIONS)}


def client_mode():
    return os.environ.get("VSS_CLIENT", "mock").strip().lower()


def get_client():
    """Return the VSS client selected by VSS_CLIENT (mock|live)."""
    mode = client_mode()
    if mode == "live":
        return LiveVSSClient()
    if mode != "mock":
        raise ValueError("VSS_CLIENT must be 'mock' or 'live', got {!r}".format(mode))
    return MockVSSClient()


def parse_filters(args):
    """Turn query params into the filter dict consumed by VSSClient.search."""
    marking_types = [v for v in args.getlist("marking_type") if v in MARKING_TYPES]
    conditions = [v for v in args.getlist("condition") if v in CONDITIONS]
    camera = (args.get("camera") or "").strip()
    street = (args.get("street") or "").strip()
    raw_conf = (args.get("min_confidence") or "").strip()
    try:
        min_confidence = float(raw_conf) if raw_conf else 0.0
    except ValueError:
        raise ValueError("min_confidence must be a number between 0 and 1")
    if not 0.0 <= min_confidence <= 1.0:
        raise ValueError("min_confidence must be between 0 and 1")
    return {
        "marking_types": marking_types,
        "conditions": conditions,
        "camera": camera,
        "street": street,
        "min_confidence": min_confidence,
    }


NO_FILTERS = {
    "marking_types": [],
    "conditions": [],
    "camera": "",
    "street": "",
    "min_confidence": 0.0,
}


def flatten_findings(response):
    """Flatten a VSS search response into one UI finding per sighting."""
    findings = []
    for result in response["results"]:
        for order, sighting in enumerate(result["marking_sightings"]):
            findings.append({
                "id": "{}-{}-{}".format(result["camera_id"],
                                         int(result["best_match_start_sec"]),
                                         order),
                "type": sighting["type"],
                "condition": sighting["condition"],
                "confidence": sighting["confidence"],
                "street": sighting["street"],
                "note": sighting["note"],
                "camera_id": result["camera_id"],
                "location": result["location"],
                "captured_at": result["captured_at"],
                "caption": result["reasoning_content"],
                "source": result["source"],
                "start_sec": result["best_match_start_sec"],
                # Data-only swap point: once the live backend fills stream_url
                # the UI renders a real <video>; mock keeps None (placeholder).
                "stream_url": result.get("stream_url"),
                # Object exposure (YOLO counts) for the finding's segment.
                "person_count": result["object_counts"]["person"],
                "object_counts": result["object_counts"],
                # Triptych evidence: before / event / after clips. Sources
                # are segment N-1 / N / N+1 within the same set, clamped at
                # set boundaries (None when the neighbor does not exist).
                # stream_urls stay None in mock; the live client fills them
                # with /api/v1/videos/stream URLs so the swap is data-only.
                "before_source": result.get("before_source"),
                "event_source": result.get("event_source"),
                "after_source": result.get("after_source"),
                "before_stream_url": result.get("before_stream_url"),
                "event_stream_url": result.get("event_stream_url"),
                "after_stream_url": result.get("after_stream_url"),
                "before_captured_at": result.get("before_captured_at"),
                "event_captured_at": result.get("event_captured_at"),
                "after_captured_at": result.get("after_captured_at"),
                "before_start_sec": result.get("before_start_sec"),
                "event_start_sec": result.get("event_start_sec"),
                "after_start_sec": result.get("after_start_sec"),
            })
    return findings


def compute_histogram(findings):
    """Object exposure histogram over the segments in the current view.

    Each segment counts once even if several of its sightings match the
    filters. Ordered by count descending so the JS only renders bars.
    """
    totals = {obj: 0 for obj in OBJECT_CLASSES}
    seen = set()
    for finding in findings:
        if finding["source"] in seen:
            continue
        seen.add(finding["source"])
        for obj in OBJECT_CLASSES:
            totals[obj] += finding["object_counts"][obj]
    return [{"object_class": obj, "count": totals[obj]}
            for obj in sorted(totals, key=totals.get, reverse=True)]


def person_median(response):
    """Corpus median of per-segment person counts (high-exposure threshold).

    The high-pedestrian-exposure flag marks findings on segments above this
    median (HANDOFF section 3).
    """
    counts = [r["object_counts"]["person"] for r in response["results"]]
    return statistics.median(counts) if counts else 0.0


def compute_stats(findings):
    """Stats strip values over the current (filtered) finding set."""
    total = len(findings)
    streets = sorted({f["street"] for f in findings if f["street"] != "unknown"})
    non_intact_by_street = {}
    total_by_street = {}
    non_intact = 0
    for finding in findings:
        street = finding["street"]
        total_by_street[street] = total_by_street.get(street, 0) + 1
        if finding["condition"] in NON_INTACT_CONDITIONS:
            non_intact += 1
            non_intact_by_street[street] = non_intact_by_street.get(street, 0) + 1
    worst_street = None
    if non_intact_by_street:
        worst_street = max(non_intact_by_street,
                           key=lambda s: (non_intact_by_street[s],
                                          total_by_street[s], s))
    return {
        "total_findings": total,
        "streets_audited": len(streets),
        "worst_street": worst_street,
        "pct_non_intact": round(100.0 * non_intact / total, 1) if total else 0.0,
    }


def filter_options():
    """Camera and street options for the filter bar, derived from the corpus."""
    findings = flatten_findings(get_client().search(dict(NO_FILTERS)))
    cameras = []
    seen = set()
    for finding in findings:
        if finding["camera_id"] not in seen:
            seen.add(finding["camera_id"])
            cameras.append({"id": finding["camera_id"],
                            "location": finding["location"]})
    streets = sorted({f["street"] for f in findings})
    return cameras, streets


def street_narrative(entry):
    """Canned mock narrative for one street's audit section.

    TODO(phase 2): replace this canned text with a W&B serverless LLM call
    (WANDB_* env vars) over the matched captions for this street, and cache
    by query so identical requests return identical reports.
    """
    street = entry["street"]
    findings = entry["findings"]
    counts = entry["counts"]
    segments = entry["segments"]
    non_intact = entry["total"] - counts["intact"]
    worst = entry["findings"][0] if findings else None
    lines = [
        "{} was audited across {} dashboard camera segments covering {} "
        "marking sightings; {} are non-intact ({:.0f}%) and {} sit on "
        "high-pedestrian-exposure segments.".format(
            street, segments, entry["total"], non_intact,
            entry["pct_non_intact"], entry["high_pedestrian"]),
        "Street risk score is {:.0f}: marking condition weighted by "
        "pedestrian exposure in the corroborating clips.".format(
            entry["risk"]),
    ]
    if worst:
        worst_suffix = ""
        if worst["high_pedestrian"]:
            worst_suffix = " on a high-pedestrian-exposure segment" \
                " ({} persons detected)".format(worst["person_count"])
        lines.append(
            "The most severe finding is a {} ({}, confidence {:.2f}), "
            "captured by {} at {}{}.".format(
                worst["type"].replace("_", " "),
                worst["condition"].replace("_", " "),
                worst["confidence"], worst["camera_id"],
                worst["captured_at"], worst_suffix))
    # Tier thresholds are per-finding average risk: a worn marking on a
    # high-pedestrian street lands in the high tier.
    avg_risk = entry["risk"] / entry["total"] if entry["total"] else 0.0
    if avg_risk >= 22.0:
        lines.append(
            "Prioritize repainting on this street: markings are worn "
            "through or missing on streets with heavy foot traffic and "
            "would likely fail a night or wet-road visibility check.")
    elif avg_risk >= 10.0:
        lines.append(
            "Schedule this street for repainting in the next maintenance "
            "cycle; wear is visible but most geometry is still intact.")
    else:
        lines.append(
            "Markings on this street are largely intact; no immediate "
            "action required.")
    return " ".join(lines)


def build_report(findings, person_threshold):
    """Assemble the per-street ranked audit report over all findings.

    risk = sum over attributed findings of
        CONDITION_RISK_WEIGHTS[condition] * person_count(segment)
    Streets are ranked by risk; findings on segments whose person count is
    above the corpus median are flagged high-pedestrian-exposure.
    """
    by_street = {}
    unknown_count = 0
    for finding in findings:
        if finding["street"] == "unknown":
            unknown_count += 1
            continue
        by_street.setdefault(finding["street"], []).append(finding)

    streets = []
    for street, street_findings in by_street.items():
        for finding in street_findings:
            finding["high_pedestrian"] = (finding["person_count"]
                                          > person_threshold)
        ordered = sorted(
            street_findings,
            key=lambda f: (CONDITION_RISK_WEIGHTS[f["condition"]],
                           f["confidence"]),
            reverse=True)
        counts = {c: 0 for c in CONDITIONS}
        for finding in street_findings:
            counts[finding["condition"]] += 1
        risk = round(sum(
            CONDITION_RISK_WEIGHTS[f["condition"]] * f["person_count"]
            for f in street_findings), 1)
        non_intact = sum(counts[c] for c in NON_INTACT_CONDITIONS)
        entry = {
            "street": street,
            "findings": ordered,
            "counts": counts,
            "total": len(street_findings),
            "non_intact": non_intact,
            "pct_non_intact": round(100.0 * non_intact / len(street_findings), 1),
            "risk": risk,
            "high_pedestrian": sum(1 for f in street_findings
                                   if f["high_pedestrian"]),
            "segments": len({f["id"].rsplit("-", 1)[0]
                             for f in street_findings}),
        }
        entry["narrative"] = street_narrative(entry)
        streets.append(entry)

    streets.sort(key=lambda s: (-s["risk"], -s["non_intact"], s["street"]))
    for rank, entry in enumerate(streets, start=1):
        entry["rank"] = rank
    return {
        "streets": streets,
        "unattributed": unknown_count,
        "total_findings": len(findings),
    }


@app.context_processor
def inject_globals():
    return {"client_mode": client_mode()}


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/findings")
def api_findings():
    try:
        filters = parse_filters(request.args)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    findings = flatten_findings(get_client().search(filters))
    return jsonify({
        "findings": findings,
        "stats": compute_stats(findings),
        "histogram": compute_histogram(findings),
    })


@app.route("/")
def index():
    cameras, streets = filter_options()
    page_data = {
        "marking_types": MARKING_TYPES,
        "conditions": CONDITIONS,
        "non_intact_conditions": NON_INTACT_CONDITIONS,
        "cond_class": COND_CLASS,
        "cameras": cameras,
        "streets": streets,
    }
    return render_template("index.html", page_data=page_data)


@app.route("/report")
def report():
    response = get_client().search(dict(NO_FILTERS))
    findings = flatten_findings(response)
    report_data = build_report(findings, person_median(response))
    return render_template(
        "report.html",
        streets=report_data["streets"],
        unattributed=report_data["unattributed"],
        total_findings=report_data["total_findings"],
        cond_class=COND_CLASS,
        condition_labels={c: c.replace("_", " ") for c in CONDITIONS},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")),
            debug=False)
