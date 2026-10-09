#!/usr/bin/env python3
"""Unit tests for road-marking-audit core client and parser contracts."""

import os
import sys
import unittest

# Ensure tools/road-marking-audit is in sys.path
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIT_DIR = os.path.join(REPO_ROOT, "tools", "road-marking-audit")
if AUDIT_DIR not in sys.path:
    sys.path.insert(0, AUDIT_DIR)

from vss_client import (
    MARKING_TYPES,
    CONDITIONS,
    OBJECT_CLASSES,
    MockVSSClient,
    LiveVSSClient,
    parse_sightings,
)


class TestParseSightings(unittest.TestCase):
    """Test parse_sightings for line format and JSON format handling."""

    def test_line_format_canonical_and_aliases(self):
        text = """
        crosswalk | intact | 0.95 | Broadway | clear zebra stripes
        pedestrian crosswalk | worn | 0.8 | 5th Ave | slightly faded
        stop bar | low contrast | 0.7 | Elm St | stop line visible
        shark teeth | absent | 0.6 | Main St | yield line missing
        """
        sightings = parse_sightings(text)
        self.assertEqual(len(sightings), 4)

        s0 = sightings[0]
        self.assertEqual(s0["type"], "crosswalk")
        self.assertEqual(s0["condition"], "intact")
        self.assertEqual(s0["confidence"], 0.95)
        self.assertEqual(s0["street"], "Broadway")

        s1 = sightings[1]
        self.assertEqual(s1["type"], "crosswalk")
        self.assertEqual(s1["condition"], "faded")
        self.assertEqual(s1["street"], "5th Ave")

        s2 = sightings[2]
        self.assertEqual(s2["type"], "stop_line")
        self.assertEqual(s2["condition"], "faded")

        s3 = sightings[3]
        self.assertEqual(s3["type"], "yield_line")
        self.assertEqual(s3["condition"], "missing_remnant")

    def test_line_format_drops_invalid(self):
        text = """
        invalid_type | intact | 0.8 | Main St | bogus type
        crosswalk | invalid_condition | 0.8 | Main St | bogus condition
        short | line
        """
        sightings = parse_sightings(text)
        self.assertEqual(sightings, [])

    def test_json_format_array_and_object(self):
        json_array = """
        [
          {"type": "zebra crossing", "condition": "partial", "confidence": 0.82, "street": "Broadway", "note": "half worn"},
          {"type": "bus stop", "condition": "faded", "confidence": "0.75", "street": "8th Ave", "note": "bus box"}
        ]
        """
        sightings = parse_sightings(json_array)
        self.assertEqual(len(sightings), 2)
        self.assertEqual(sightings[0]["type"], "crosswalk")
        self.assertEqual(sightings[0]["condition"], "partial")
        self.assertEqual(sightings[1]["type"], "bus_box")
        self.assertEqual(sightings[1]["condition"], "faded")

    def test_json_markdown_block(self):
        json_md = """```json
        {
          "findings": [
            {"type": "turn arrow", "condition": "good", "confidence": 0.9, "street": "unknown"}
          ]
        }
        ```"""
        sightings = parse_sightings(json_md)
        self.assertEqual(len(sightings), 1)
        self.assertEqual(sightings[0]["type"], "directional_arrow")
        self.assertEqual(sightings[0]["condition"], "intact")


class TestLiveVSSClient(unittest.TestCase):
    """Test LiveVSSClient with mock HTTP transport."""

    def setUp(self):
        def fake_transport(method, url, headers=None, json_data=None):
            if "/api/v1/auth/login" in url:
                return 200, {"access_token": "test-jwt-token"}
            elif "/api/v1/search" in url:
                return 200, {
                    "results": [
                        {
                            "source": "s3://bucket/test_chunk_segment_001_of_002.mp4",
                            "original_video": "s3://bucket/test_chunk.mp4",
                            "camera_id": "nyc_bike_gopro-1",
                            "location": "new_york",
                            "best_match_start_sec": 10.0,
                            "reasoning_content": "crosswalk | faded | 0.88 | Broadway | zebra crosswalk",
                        },
                        {
                            "source": "s3://bucket/test_chunk_segment_002_of_002.mp4",
                            "original_video": "s3://bucket/test_chunk.mp4",
                            "camera_id": "nyc_bike_gopro-1",
                            "location": "new_york",
                            "best_match_start_sec": 15.0,
                            "reasoning_content": "stop_line | intact | 0.92 | Broadway | stop bar",
                        },
                    ],
                    "llm_synthesis": {"response": "Mock synthesis response."},
                }
            elif "/api/v1/videos/detections" in url:
                return 200, {"objects": {"person": 3, "bicycle": 2, "car": 5}}
            return 404, {"detail": "not found"}

        self.client = LiveVSSClient(
            base_url="http://mock-vss",
            username="testuser",
            password="testpass",
            transport=fake_transport,
        )

    def test_live_search_contract_and_triptych(self):
        res = self.client.search({"marking_types": ["crosswalk"]})
        self.assertIn("results", res)
        self.assertIn("chunk_results", res)
        self.assertIn("llm_synthesis", res)

        results = res["results"]
        self.assertEqual(len(results), 1)
        r0 = results[0]
        self.assertEqual(r0["camera_id"], "nyc_bike_gopro-1")
        self.assertEqual(r0["location"], "new_york")
        self.assertIn("stream_url", r0)
        self.assertTrue(r0["stream_url"].startswith("/api/v1/videos/stream"))
        self.assertEqual(r0["object_counts"]["person"], 3)
        self.assertEqual(r0["object_counts"]["bicycle"], 2)

        # Check triptych after-clip
        self.assertEqual(r0["after_source"], "s3://bucket/test_chunk_segment_002_of_002.mp4")
        self.assertIsNotNone(r0["after_stream_url"])
        self.assertTrue(r0["after_stream_url"].startswith("/api/v1/videos/stream"))

    def test_parity_filter_semantics_with_mock_client(self):
        mock_client = MockVSSClient()
        mock_res = mock_client.search({"marking_types": ["crosswalk"]})
        self.assertIn("results", mock_res)
        self.assertIn("chunk_results", mock_res)
        for r in mock_res["results"]:
            for s in r["marking_sightings"]:
                self.assertEqual(s["type"], "crosswalk")


class TestAgentAnswerAndEvidence(unittest.TestCase):
    def setUp(self):
        from main import _evidence, agent_answer, flatten_findings
        self._evidence = _evidence
        self.agent_answer = agent_answer
        self.flatten_findings = flatten_findings

    def test_evidence_structure_and_stream_url(self):
        sample_findings = [{
            "type": "crosswalk",
            "condition": "worn",
            "street": "Broadway",
            "camera_id": "cam-1",
            "captured_at": "2026-10-09 12:00:00",
            "start_sec": 10.0,
            "note": "faded paint",
            "confidence": 0.85,
            "person_count": 4,
            "source": "s3://b/seg.mp4",
            "stream_url": "/api/v1/videos/stream?source=s3%3A%2F%2Fb%2Fseg.mp4&token=xyz",
        }]
        ev = self._evidence(sample_findings)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["stream_url"], "/api/v1/videos/stream?source=s3%3A%2F%2Fb%2Fseg.mp4&token=xyz")
        self.assertEqual(ev[0]["confidence"], 0.85)
        self.assertEqual(ev[0]["person_count"], 4)

    def test_agent_answer_grounded_response(self):
        sample_findings = [{
            "id": "seg-1-0",
            "type": "crosswalk",
            "condition": "worn",
            "street": "Central Park West",
            "camera_id": "cam-1",
            "captured_at": "2026-10-09 12:00:00",
            "start_sec": 10.0,
            "note": "faded paint",
            "confidence": 0.9,
            "person_count": 8,
            "stream_url": "/api/v1/videos/stream?source=s3%3A%2F%2Fb%2Fseg.mp4&token=xyz",
        }]
        answer, evidence = self.agent_answer("Which street should be repainted first?", sample_findings, 2)
        self.assertTrue(len(answer) > 0)
        self.assertTrue(len(evidence) > 0)
        self.assertEqual(evidence[0]["street"], "Central Park West")
        self.assertEqual(evidence[0]["stream_url"], "/api/v1/videos/stream?source=s3%3A%2F%2Fb%2Fseg.mp4&token=xyz")


if __name__ == "__main__":
    unittest.main()
