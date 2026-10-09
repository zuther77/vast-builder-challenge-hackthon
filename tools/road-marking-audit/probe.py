#!/usr/bin/env python3
"""VM probe script for Cosmos3-Reason road marking prompt validation.

Tests prompt variants against candidate dashcam video clips directly on the
Cosmos3-Reason VLM (port 8001) before running any large-scale corpus re-ingestion.

Usage:
    python probe.py [--clips N] [--dry-run] [--output PATH]
"""

import argparse
import base64
import glob
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

# Import parse_sightings and taxonomy from vss_client
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from vss_client import MARKING_TYPES, CONDITIONS, parse_sightings


# ---------------------------------------------------------------------------
# Prompt variants (HANDOFF sections 6 and 8.1)
# ---------------------------------------------------------------------------

PROMPT_LINE_FORMAT = (
    "You are auditing road markings for a city cycling infrastructure review in New York City. "
    "Focus on markings visible from the cyclist's perspective. Only report markings that are ACTUALLY VISIBLE on the pavement. "
    "Do NOT hallucinate or guess missing markings. Check for: crosswalk, bike_lane, stop_line, yield_line, "
    "directional_arrow, word_text, bus_box, hov_diamond. "
    "For each visible marking, output: (1) type; (2) condition (intact, faded, partial, missing_remnant); "
    "(3) confidence 0.0-1.0; (4) street name if visible on a sign or landmark, else 'unknown'; "
    "(5) a concise note. If no markings are visible, reply 'NONE'. "
    "List at most 4 markings. Format exactly one line per marking: `TYPE | CONDITION | CONFIDENCE | STREET | NOTE`."
)

PROMPT_JSON_FORMAT = (
    "You are auditing road markings for a city infrastructure review. For each clip, "
    "find all painted road markings. Check specifically for: pedestrian crosswalks "
    "(zebra/continental stripes), school crossings, stop lines, yield lines, painted "
    "words (SCHOOL, BUS, STOP, SLOW), bus stop boxes, HOV lane diamonds, and "
    "directional/turn arrows.\n"
    "List each distinct physical marking at most once. Output a JSON array of objects with keys:\n"
    "\"type\": one of [crosswalk, school_crossing, stop_line, yield_line, word_text, bus_box, hov_diamond, directional_arrow],\n"
    "\"condition\": one of [intact, faded, partial, missing_remnant],\n"
    "\"confidence\": float between 0.0 and 1.0,\n"
    "\"street\": string (visible street name sign/intersection if visible, else \"unknown\"),\n"
    "\"note\": string (short description of the marking).\n"
    "If no markings are visible, output []. Output ONLY valid JSON."
)

PROMPT_VARIANTS = {
    "line_format": PROMPT_LINE_FORMAT,
    "json_format": PROMPT_JSON_FORMAT,
}


# ---------------------------------------------------------------------------
# Config & Environment loading
# ---------------------------------------------------------------------------

def load_environment() -> Dict[str, str]:
    """Load config from env vars or /config/*.config."""
    cfg: Dict[str, str] = {}

    for key in [
        "INGRESS_URL", "USERNAME", "PASSWORD", "GPU_BEARER_TOKEN",
        "COSMOS3_REASON_URL", "GPU_HOST", "S3_CHUNKS_BUCKET", "S3_SEGMENTS_BUCKET"
    ]:
        val = os.environ.get(key)
        if val:
            cfg[key] = val

    config_files = sorted(glob.glob("/config/*.config"))
    if config_files and ("INGRESS_URL" not in cfg or "PASSWORD" not in cfg):
        config_path = config_files[0]
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("\"'")
                        if k not in cfg:
                            cfg[k] = v
        except Exception as e:
            print(f"Note: Could not read {config_path}: {e}")

    gpu_host = cfg.get("GPU_HOST", "166.19.38.112")
    if "COSMOS3_REASON_URL" not in cfg:
        cfg["COSMOS3_REASON_URL"] = f"http://{gpu_host}:8001"

    required = ["INGRESS_URL", "USERNAME", "PASSWORD", "GPU_BEARER_TOKEN"]
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        raise RuntimeError(
            f"Missing required configuration keys: {missing}. "
            "Ensure environment variables or /config/*.config are present."
        )

    return cfg


# ---------------------------------------------------------------------------
# HTTP Helpers
# ---------------------------------------------------------------------------

def api_request(
    url: str,
    method: str = "GET",
    data: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 60,
) -> Tuple[int, bytes]:
    """Perform HTTP request returning (status_code, body_bytes)."""
    headers = dict(headers or {})
    req_body = None
    if data is not None:
        req_body = json.dumps(data).encode("utf-8")
        if "Content-Type" not in headers:
            headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=req_body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        raise RuntimeError(f"HTTP request to {url} failed: {e}")


def login(backend_url: str, username: str, password: str) -> str:
    """Authenticate with VSS backend and return access JWT."""
    url = f"{backend_url.rstrip('/')}/api/v1/auth/login"
    status, body = api_request(
        url,
        method="POST",
        data={"username": username, "password": password},
        timeout=15,
    )
    if status != 200:
        raise RuntimeError(f"Login failed (HTTP {status}): {body.decode('utf-8', errors='replace')}")
    data = json.loads(body.decode("utf-8"))
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"No access_token returned by {url}")
    return token


def fetch_candidate_segments(
    backend_url: str,
    token: str,
    count: int = 10,
) -> List[Dict[str, Any]]:
    """Fetch candidate segment metadata specifically for NYC cycling footage (nyc_bike_gopro-1)."""
    headers = {"Authorization": f"Bearer {token}"}
    backend = backend_url.rstrip("/")
    candidates: List[Dict[str, Any]] = []
    seen_sources = set()

    # Search queries targeted specifically at NYC cycling footage
    search_queries = [
        {"query": "bike lane green paint bicycle path road marking", "metadata_filters": {"location": "new_york", "camera_id": "nyc_bike_gopro-1"}},
        {"query": "pedestrian crosswalk stop line cyclist riding", "metadata_filters": {"location": "new_york", "camera_id": "nyc_bike_gopro-1"}},
        {"query": "bicycle symbol sharrow painted text street", "metadata_filters": {"location": "new_york", "camera_id": "nyc_bike_gopro-1"}},
        {"query": "cyclist riding on new york street road markings", "metadata_filters": {"location": "new_york"}},
    ]

    for sq in search_queries:
        if len(candidates) >= count:
            break
        url = f"{backend}/api/v1/search"
        payload = {
            "query": sq["query"],
            "top_k": 15,
            "min_similarity": 0.20,
            "include_public": True,
            "metadata_filters": sq["metadata_filters"],
        }

        status, body = api_request(
            url,
            method="POST",
            data=payload,
            headers=headers,
            timeout=30,
        )
        if status == 200:
            data = json.loads(body.decode("utf-8"))
            for res in data.get("results", []):
                src = res.get("source")
                if src and src not in seen_sources:
                    seen_sources.add(src)
                    candidates.append({
                        "source": src,
                        "original_video": res.get("original_video"),
                        "camera_id": res.get("camera_id") or "nyc_bike_gopro-1",
                        "location": res.get("location") or "new_york",
                        "reasoning_content": res.get("reasoning_content", ""),
                    })
                if len(candidates) >= count:
                    break

    # Fallback to explore specifically for new_york / nyc_bike_gopro-1
    if len(candidates) < count:
        exp_url = f"{backend}/api/v1/videos/explore?scope=all&limit=50&offset=0&location=new_york"
        status, body = api_request(exp_url, method="GET", headers=headers, timeout=20)
        if status == 200:
            exp_data = json.loads(body.decode("utf-8"))
            for chunk in exp_data.get("chunks", []):
                if chunk.get("camera_id") == "nyc_bike_gopro-1":
                    for seg in chunk.get("timeline", []):
                        src = seg.get("source")
                        if src and src not in seen_sources:
                            seen_sources.add(src)
                            candidates.append({
                                "source": src,
                                "original_video": chunk.get("original_video"),
                                "camera_id": chunk.get("camera_id") or "nyc_bike_gopro-1",
                                "location": "new_york",
                                "reasoning_content": seg.get("preview_caption", ""),
                            })
                        if len(candidates) >= count:
                            break
                if len(candidates) >= count:
                    break

    return candidates[:count]


def download_segment_bytes(backend_url: str, source: str, token: str) -> bytes:
    """Download video clip mp4 bytes via /api/v1/videos/stream."""
    quoted_source = urllib.parse.quote(source, safe="")
    url = f"{backend_url.rstrip('/')}/api/v1/videos/stream?source={quoted_source}&token={token}"
    status, body = api_request(url, method="GET", timeout=45)
    if status != 200:
        raise RuntimeError(f"Failed to stream clip {source} (HTTP {status})")
    return body


def get_cosmos_model_id(cosmos_url: str, bearer_token: str) -> str:
    """Query /v1/models to obtain the active model ID."""
    url = f"{cosmos_url.rstrip('/')}/v1/models"
    headers = {"Authorization": f"Bearer {bearer_token}"}
    status, body = api_request(url, method="GET", headers=headers, timeout=10)
    if status != 200:
        raise RuntimeError(f"Failed to query {url} (HTTP {status}): {body.decode('utf-8', errors='replace')}")
    data = json.loads(body.decode("utf-8"))
    model_list = data.get("data", [])
    if not model_list:
        return "nvidia/cosmos3-nano-reasoner"
    return model_list[0].get("id", "nvidia/cosmos3-nano-reasoner")


def run_cosmos_inference(
    cosmos_url: str,
    bearer_token: str,
    model_id: str,
    prompt_text: str,
    video_base64: str,
    temperature: float = 0.0,
    max_tokens: int = 512,
) -> str:
    """Run chat completion with Cosmos3-Reason passing text + video base64."""
    url = f"{cosmos_url.rstrip('/')}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model_id,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_text},
                    {
                        "type": "video_url",
                        "video_url": {
                            "url": f"data:video/mp4;base64,{video_base64}"
                        },
                    },
                ],
            }
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    status, body = api_request(url, method="POST", data=payload, headers=headers, timeout=90)
    if status != 200:
        raise RuntimeError(
            f"Cosmos inference failed (HTTP {status}): {body.decode('utf-8', errors='replace')}"
        )
    resp_json = json.loads(body.decode("utf-8"))
    choices = resp_json.get("choices", [])
    if not choices:
        return ""
    return choices[0].get("message", {}).get("content", "")


# ---------------------------------------------------------------------------
# Stability & Comparison Logic
# ---------------------------------------------------------------------------

def evaluate_stability(s1: List[Dict[str, Any]], s2: List[Dict[str, Any]]) -> Dict[str, bool]:
    """Check stability across run 1 and run 2."""
    types1 = set(s.get("type") for s in s1 if s.get("type"))
    types2 = set(s.get("type") for s in s2 if s.get("type"))
    type_match = (types1 == types2)

    type_cond1 = set((s.get("type"), s.get("condition")) for s in s1)
    type_cond2 = set((s.get("type"), s.get("condition")) for s in s2)
    condition_match = (type_cond1 == type_cond2)

    strict_match = condition_match and (
        [s.get("street") for s in s1] == [s.get("street") for s in s2]
    )

    return {
        "type_match": type_match,
        "condition_match": condition_match,
        "strict_match": strict_match,
    }


# ---------------------------------------------------------------------------
# Main CLI Workflow
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Probe Cosmos3-Reason with road marking audit prompts")
    parser.add_argument("--clips", type=int, default=10, help="Number of clips to test (default 10)")
    parser.add_argument("--dry-run", action="store_true", help="Fetch clips without invoking GPU inference")
    parser.add_argument("--output", type=str, default="probe_results.json", help="Path to save probe results")
    args = parser.parse_args()

    print("=" * 70)
    print("ROAD MARKING AUDIT — COSMOS3-REASON VM PROMPT VALIDATION")
    print("=" * 70)

    # 1. Environment check
    print("\n[1/5] Loading configuration...")
    try:
        cfg = load_environment()
        print("  ✓ Environment variables and /config verified")
        print(f"  ✓ Target Cosmos endpoint: {cfg['COSMOS3_REASON_URL']}")
    except Exception as e:
        print(f"  ✗ Configuration error: {e}")
        sys.exit(1)

    # 2. Authenticate
    print("\n[2/5] Authenticating to VSS backend...")
    try:
        token = login(cfg["INGRESS_URL"], cfg["USERNAME"], cfg["PASSWORD"])
        print("  ✓ Login successful (JWT acquired)")
    except Exception as e:
        print(f"  ✗ Authentication failed: {e}")
        sys.exit(1)

    # 3. Pull candidate segments
    print(f"\n[3/5] Selecting {args.clips} candidate marking clips from archive...")
    candidates = fetch_candidate_segments(cfg["INGRESS_URL"], token, count=args.clips)
    print(f"  ✓ Found {len(candidates)} candidate clips:")
    for i, c in enumerate(candidates, 1):
        src_name = c["source"].split("/")[-1]
        print(f"    [{i:02d}] {src_name} ({c['camera_id']} / {c['location']})")

    if not candidates:
        print("  ✗ No candidate clips found in index!")
        sys.exit(1)

    if args.dry_run:
        print("\n[DRY RUN] Skipping download and GPU inference.")
        return

    # 4. Resolve Model ID
    print("\n[4/5] Checking Cosmos3-Reason model...")
    try:
        model_id = get_cosmos_model_id(cfg["COSMOS3_REASON_URL"], cfg["GPU_BEARER_TOKEN"])
        print(f"  ✓ Using model: {model_id}")
    except Exception as e:
        print(f"  ✗ Could not fetch Cosmos model: {e}")
        sys.exit(1)

    # 5. Run inference across prompt variants
    print(f"\n[5/5] Running inference across {len(PROMPT_VARIANTS)} prompt variants (2 runs each)...")

    results_data: Dict[str, Any] = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model_id": model_id,
        "clip_count": len(candidates),
        "variants": {},
    }

    for variant_name, prompt_text in PROMPT_VARIANTS.items():
        print(f"\n--- Testing Variant: {variant_name} ---")
        variant_records = []
        cond_stable_count = 0
        type_stable_count = 0
        clips_with_findings = 0
        total_sightings_count = 0

        for idx, candidate in enumerate(candidates, 1):
            source = candidate["source"]
            filename = source.split("/")[-1]
            print(f"  Clip [{idx}/{len(candidates)}] {filename} ({candidate['location']})...", end="", flush=True)

            try:
                # Download bytes
                clip_bytes = download_segment_bytes(cfg["INGRESS_URL"], source, token)
                b64_video = base64.b64encode(clip_bytes).decode("utf-8")

                # Run 1
                out1 = run_cosmos_inference(
                    cfg["COSMOS3_REASON_URL"],
                    cfg["GPU_BEARER_TOKEN"],
                    model_id,
                    prompt_text,
                    b64_video,
                    temperature=0.0,
                )
                sightings1 = parse_sightings(out1)

                # Run 2 (stability check)
                out2 = run_cosmos_inference(
                    cfg["COSMOS3_REASON_URL"],
                    cfg["GPU_BEARER_TOKEN"],
                    model_id,
                    prompt_text,
                    b64_video,
                    temperature=0.0,
                )
                sightings2 = parse_sightings(out2)

                stability = evaluate_stability(sightings1, sightings2)
                if stability["condition_match"]:
                    cond_stable_count += 1
                if stability["type_match"]:
                    type_stable_count += 1
                if sightings1 or sightings2:
                    clips_with_findings += 1
                total_sightings_count += len(sightings1)

                print(f" findings: {len(sightings1)} (r1), {len(sightings2)} (r2) | cond_stable: {stability['condition_match']}")

                variant_records.append({
                    "source": source,
                    "filename": filename,
                    "camera_id": candidate["camera_id"],
                    "location": candidate["location"],
                    "run1": {"raw": out1, "sightings": sightings1},
                    "run2": {"raw": out2, "sightings": sightings2},
                    "stability": stability,
                })
            except Exception as e:
                print(f" ERROR: {e}")
                variant_records.append({
                    "source": source,
                    "filename": filename,
                    "error": str(e),
                    "stability": {"type_match": False, "condition_match": False, "strict_match": False},
                })

        results_data["variants"][variant_name] = {
            "records": variant_records,
            "clips_tested": len(candidates),
            "clips_with_findings": clips_with_findings,
            "total_sightings": total_sightings_count,
            "type_stable_runs": type_stable_count,
            "condition_stable_runs": cond_stable_count,
            "condition_stability_pct": round((cond_stable_count / len(candidates)) * 100, 1) if candidates else 0.0,
            "type_stability_pct": round((type_stable_count / len(candidates)) * 100, 1) if candidates else 0.0,
        }

    # 6. Print Summary Table
    print("\n" + "=" * 75)
    print("PROBE RESULTS SUMMARY & COMPARISON")
    print("=" * 75)
    print(f"{'Variant':<15} | {'Tested':<7} | {'Findings':<9} | {'Sightings':<10} | {'Type Stab %':<12} | {'Cond Stab %':<12}")
    print("-" * 75)

    for v_name, v_summary in results_data["variants"].items():
        print(
            f"{v_name:<15} | "
            f"{v_summary['clips_tested']:<7} | "
            f"{v_summary['clips_with_findings']:<9} | "
            f"{v_summary['total_sightings']:<10} | "
            f"{v_summary['type_stability_pct']:<11}% | "
            f"{v_summary['condition_stability_pct']:<11}%"
        )
    print("-" * 75)

    # 7. Evaluate Gates
    line_summary = results_data["variants"].get("line_format", {})
    json_summary = results_data["variants"].get("json_format", {})

    print("\nGATE EVALUATION (HANDOFF §6 & §8.1):")
    for v_name, s in results_data["variants"].items():
        passed_finding_gate = s["clips_with_findings"] >= int(0.7 * len(candidates))
        passed_stability_gate = s["condition_stability_pct"] >= 80.0
        print(f"  • {v_name}:")
        print(f"      Marking findings: {s['clips_with_findings']}/{len(candidates)} clips "
              f"({'PASS' if passed_finding_gate else 'WARN/FAIL'})")
        print(f"      Condition stability: {s['condition_stability_pct']}% "
              f"({'PASS' if passed_stability_gate else 'WARN/FAIL'})")

    best_variant = "line_format" if (
        line_summary.get("clips_with_findings", 0) >= json_summary.get("clips_with_findings", 0)
    ) else "json_format"
    print(f"\n  ✓ Recommended prompt variant for re-ingest: '{best_variant}'")

    # 8. Save output
    output_path = os.path.join(SCRIPT_DIR, args.output)
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(results_data, f, indent=2)
        print(f"\n✓ Saved detailed probe results to: {output_path}")
    except Exception as e:
        print(f"✗ Failed to write {output_path}: {e}")


if __name__ == "__main__":
    main()
