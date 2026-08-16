from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "smoke" / "kitchen-cabinets-sink.jpg"
ATTRIBUTION = ROOT / "fixtures" / "smoke" / "attribution.json"
EXPECTED_SHA256 = "1c007ab919b41d30f49729346ab5eef1734748f787249e419c60794c04c69649"


def test_public_smoke_fixture_matches_successful_run_input() -> None:
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == EXPECTED_SHA256


def test_public_smoke_fixture_has_publishable_attribution() -> None:
    metadata = json.loads(ATTRIBUTION.read_text(encoding="utf-8"))["items"][0]

    assert metadata["sanitized_sha256"] == EXPECTED_SHA256
    assert metadata["rights_status"] == "publish_allowed"
    assert metadata["license"]["id"] == "CC-BY-2.0"
    assert metadata["creator"] == "amslerPIX"
