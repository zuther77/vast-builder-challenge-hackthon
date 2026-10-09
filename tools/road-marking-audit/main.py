"""StreetAudit -- road marking audit dashboard (Flask app).

Routes:
    /              findings board (filter bar, stats strip, street cards)
    /report        per-street audit report (risk ranking + narrative)
    /ask           agent prompt window (plain-language Q&A over the corpus)
    /api/findings  JSON API backing the findings board
    /api/ask       JSON API backing the agent prompt window
    /health        health check for the K8s deploy

Backend selection: env var VSS_CLIENT (mock|live), default mock. All backend
access goes through vss_client; swapping in the live backend is a config
change, not a code change.
"""

import os
import statistics

from flask import Flask, jsonify, render_template, request

from vss_client import (
    CONDITION_ALIASES,
    CONDITIONS,
    CONDITION_RISK_WEIGHTS,
    MARKING_TYPES,
    NON_INTACT_CONDITIONS,
    OBJECT_CLASSES,
    TYPE_ALIASES,
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


_narrative_cache = {}


def street_narrative(entry):
    """Generate the narrative for one street card in the audit report.

    Uses Weights & Biases serverless inference if WANDB_API_KEY is present,
    falling back to deterministic template generation with query caching.
    """
    street = entry["street"]
    cache_key = f"{street}_{entry['total']}_{entry['risk']}"
    if cache_key in _narrative_cache:
        return _narrative_cache[cache_key]

    wandb_key = os.environ.get("WANDB_API_KEY")
    if wandb_key:
        try:
            import json
            import urllib.request
            prompt = (
                f"You are a municipal traffic engineer auditing road and cycling markings on {street} in New York City. "
                f"Data: {entry['total']} markings sighted, risk score {entry['risk']}, {entry['high_pedestrian']} high-pedestrian-exposure markings. "
                f"Write a concise 3-sentence audit summary explaining the findings and priority recommendation."
            )
            req = urllib.request.Request(
                "https://api.wandb.ai/v1/chat/completions",
                data=json.dumps({
                    "model": "meta-llama/llama-3.1-8b-instruct",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 120,
                    "temperature": 0.2,
                }).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {wandb_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                text = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                if text:
                    _narrative_cache[cache_key] = text
                    return text
        except Exception:
            pass

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

    res = " ".join(lines)
    _narrative_cache[cache_key] = res
    return res


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


# ---------------------------------------------------------------------------
# Agent prompt window: deterministic mock Q&A over the corpus.
# TODO(phase 2): replace agent_answer with a W&B serverless LLM call
# (WANDB_* env vars) over the matched captions, cached by question so
# identical questions return identical answers. Evidence rows stay the
# same: rank the findings the answer cites and show their clips.
# ---------------------------------------------------------------------------

RISK_INTENT_WORDS = ("worst", "repaint", "priorit", "risk", "first",
                     "urgent", "schedule")


def _detect_all(question, aliases, valid):
    """Every canonical value whose alias phrases occur in the question."""
    found = set()
    for phrase, canonical in aliases.items():
        if canonical in valid and phrase in question:
            found.add(canonical)
    return found


def _detect_street(question, findings):
    """Corpus street name mentioned in the question, or None."""
    streets = sorted({f["street"] for f in findings if f["street"] != "unknown"})
    for street in streets:
        if street.lower() in question:
            return street
    return None


def _evidence(findings, limit=5):
    """Top findings backing an answer: worst condition, then confidence."""
    ordered = sorted(findings,
                     key=lambda f: (CONDITION_RISK_WEIGHTS[f["condition"]],
                                    f["confidence"]),
                     reverse=True)
    return [{
        "type": f["type"],
        "condition": f["condition"],
        "street": f["street"],
        "camera_id": f["camera_id"],
        "captured_at": f["captured_at"],
        "start_sec": f["start_sec"],
        "note": f["note"],
    } for f in ordered[:limit]]


def _street_summary(street, street_findings):
    """Answer fragment summarizing one street's findings."""
    total = len(street_findings)
    non_intact = sum(1 for f in street_findings
                     if f["condition"] in NON_INTACT_CONDITIONS)
    risk = sum(CONDITION_RISK_WEIGHTS[f["condition"]] * f["person_count"]
               for f in street_findings)
    high_ped = sum(1 for f in street_findings if f["high_pedestrian"])
    return (
        "{street} has {total} marking sightings across {segments} "
        "segments; {non_intact} are non-intact ({pct:.0f}%) and {high_ped} "
        "sit on high-pedestrian-exposure segments. Street risk score is "
        "{risk:.0f} (marking condition weighted by pedestrian exposure)."
    ).format(street=street, total=total,
             segments=len({f["id"].rsplit("-", 1)[0]
                           for f in street_findings}),
             non_intact=non_intact,
             pct=100.0 * non_intact / total if total else 0.0,
             high_ped=high_ped, risk=risk)


def _risk_ranking(matched, limit=3):
    """Street names ranked by risk, formatted for an answer sentence."""
    by_street = {}
    for finding in matched:
        street = finding["street"]
        if street == "unknown":
            continue
        by_street[street] = by_street.get(street, 0) + (
            CONDITION_RISK_WEIGHTS[finding["condition"]]
            * finding["person_count"])
    ranked = sorted(by_street.items(), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join("{} (risk {:.0f})".format(street, risk)
                     for street, risk in ranked[:limit])


def agent_answer(question, findings, person_threshold):
    """Deterministic mock answer to a plain-language audit question.

    Intent detection: street name > marking type > condition > risk ranking,
    composed over the same findings the board and report show. Phase 2
    replaces this with a W&B serverless LLM over the matched captions.
    """
    question = " ".join(question.lower().split())
    street = _detect_street(question, findings)
    marking_types = _detect_all(question, TYPE_ALIASES, MARKING_TYPES)
    conditions = _detect_all(question, CONDITION_ALIASES, CONDITIONS)
    risk_intent = any(word in question for word in RISK_INTENT_WORDS)

    for finding in findings:
        finding["high_pedestrian"] = finding["person_count"] > person_threshold

    matched = findings
    if street:
        matched = [f for f in matched if f["street"] == street]
    if marking_types:
        matched = [f for f in matched if f["type"] in marking_types]
    if conditions:
        matched = [f for f in matched if f["condition"] in conditions]

    if matched:
        non_intact = sum(1 for f in matched
                         if f["condition"] in NON_INTACT_CONDITIONS)
        parts = []
        if street:
            parts.append(_street_summary(street, matched))
        elif risk_intent and not (marking_types or conditions):
            parts.append("By risk (marking condition weighted by pedestrian "
                         "exposure), the streets to repaint first are: {}."
                         .format(_risk_ranking(matched)))
        else:
            scope_bits = []
            for marking_type in sorted(marking_types):
                scope_bits.append(marking_type.replace("_", " "))
            for condition in sorted(conditions):
                scope_bits.append(condition.replace("_", " "))
            scope = " / ".join(scope_bits) if scope_bits else "the corpus"
            lead = "The audit has {total} sighting{s} in the current corpus" \
                if not scope_bits else \
                "The audit has {total} {scope} sighting{s} in the current corpus"
            parts.append((lead + ", {non_intact} of them non-intact.").format(
                total=len(matched), scope=scope,
                s="" if len(matched) == 1 else "s", non_intact=non_intact))
            by_street = {}
            for finding in matched:
                if finding["street"] != "unknown":
                    by_street.setdefault(finding["street"], []).append(finding)
            if by_street:
                ranked = sorted(by_street.items(),
                               key=lambda kv: -len(kv[1]))
                counts = ", ".join("{} ({})".format(s, len(fs))
                                   for s, fs in ranked[:3])
                parts.append("Most of them are on: {}.".format(counts))
        if risk_intent and not street and (marking_types or conditions):
            ranked = _risk_ranking(matched, limit=1)
            if ranked:
                parts.append("By risk, repaint {} first.".format(ranked))
        return " ".join(parts), _evidence(matched)

    # Nothing matched the detected intent.
    if street:
        return ("No sightings of {} were attributed in this corpus. The "
                "street may appear only with unknown attribution -- check "
                "the unattributed stat on the audit report.").format(street), []
    return ("I could not match that to the audit data. Ask me about a "
            "street (e.g. Don Mills Rd), a marking type (crosswalk, stop "
            "line, bus box...), a condition (faded, partial, missing), or "
            "which street to repaint first."), []


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


@app.route("/ask")
def ask():
    cameras, streets = filter_options()
    attributed = [s for s in streets if s != "unknown"]
    suggestions = [
        "Which street should be repainted first?",
        "Where are crosswalks faded or missing?",
    ]
    if attributed:
        suggestions.append("How is {} doing?".format(attributed[0]))
    if len(attributed) > 1:
        suggestions.append(
            "What did the audit find on {}?".format(attributed[-1]))
    return render_template(
        "ask.html",
        suggestions=suggestions,
        ask_data={"mode": client_mode(), "cond_class": COND_CLASS},
    )


@app.route("/api/ask", methods=["POST"])
def api_ask():
    body = request.get_json(silent=True) or {}
    question = (body.get("question") or "").strip()
    if not question:
        return jsonify({"error": "question is required"}), 400
    response = get_client().search(dict(NO_FILTERS))
    findings = flatten_findings(response)
    answer, evidence = agent_answer(question, findings,
                                     person_median(response))
    return jsonify({
        "answer": answer,
        "evidence": evidence,
        "mode": client_mode(),
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
