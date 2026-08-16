from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.cli import main
from app.image_preprocessor import preprocess_image
from app.orchestrator import AgentLock, PhotoBlogOrchestrator
from app.output_parser import parse_vision_json
from app.schemas import LockError, RunRequest, VisionOutputError


def valid_json() -> str:
    return json.dumps(
        {
            "summary": "요약",
            "scene": {"location_type": "실외", "time_or_lighting": "밝음", "weather": ""},
            "subjects": [{"name": "나무", "description": "초록 잎이 보임", "position": "중앙", "confidence": 0.9}],
            "visible_text": [],
            "colors_and_composition": {"dominant_colors": [], "composition": "", "mood_from_visuals": ""},
            "blog_worthy_details": [],
            "uncertainties": [],
            "privacy_notes": [],
            "raw_caption": "",
        },
        ensure_ascii=False,
    )


def test_json_code_fence_recovery() -> None:
    parsed = parse_vision_json("```json\n" + valid_json() + "\n```")
    assert parsed["summary"] == "요약"


def test_invalid_vlm_json_rejected() -> None:
    with pytest.raises(VisionOutputError):
        parse_vision_json('{"summary": "only"}')


def test_image_preprocess_limits_size(sample_image: Path, tmp_path: Path) -> None:
    prepared = preprocess_image(sample_image, tmp_path / "prepared.jpg", 896, 88)
    assert max(prepared.prepared_size) <= 896
    assert prepared.source_path == sample_image.resolve()


def test_lock_blocks_concurrent_runs(tmp_path: Path, sample_image: Path) -> None:
    lock_path = tmp_path / "runs" / "agent.lock"
    request = RunRequest(image=sample_image)
    with AgentLock(lock_path, request):
        with pytest.raises(LockError):
            with AgentLock(lock_path, request):
                pass


def test_dry_run_command_generation(fake_config, sample_image: Path) -> None:
    result = PhotoBlogOrchestrator(fake_config).run(RunRequest(image=sample_image, topic="테스트", dry_run=True))
    assert result["vlm_command"][0].endswith("llama-mtmd-cli")
    assert result["llm_command"][0].endswith("llama-cli")
    assert not (Path(result["run_dir"]) / "llm_stdout.txt").exists()


def test_fake_vlm_to_fake_llm_integration(fake_config, sample_image: Path) -> None:
    output = fake_config.output_dir / "blog.md"
    result = PhotoBlogOrchestrator(fake_config).run(RunRequest(image=sample_image, topic="테스트", output=output))
    assert output.exists()
    assert "# 제목" in output.read_text(encoding="utf-8")
    run_dir = Path(result["run_dir"])
    assert (run_dir / "vision.json").exists()
    assert (run_dir / "metrics.json").exists()


def test_cli_dry_run(fake_config, sample_image: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(fake_config.root)
    assert main(["--image", str(sample_image), "--topic", "사진", "--dry-run"]) == 0
