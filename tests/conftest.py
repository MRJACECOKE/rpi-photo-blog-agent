from __future__ import annotations

import os
from pathlib import Path

import pytest
from PIL import Image

from app.config import AppConfig


@pytest.fixture
def sample_image(tmp_path: Path) -> Path:
    path = tmp_path / "sample.jpg"
    Image.new("RGB", (1200, 800), (20, 120, 200)).save(path, "JPEG")
    return path


@pytest.fixture
def fake_config(tmp_path: Path) -> AppConfig:
    root = tmp_path
    for name in ("models/vlm", "models/llm", "runs", "outputs", "logs", "config"):
        (root / name).mkdir(parents=True, exist_ok=True)
    prompts = Path(__file__).resolve().parents[1] / "config" / "prompts.yaml"
    (root / "config" / "prompts.yaml").write_text(prompts.read_text(encoding="utf-8"), encoding="utf-8")
    vlm_bin = root / "llama-mtmd-cli"
    llm_bin = root / "llama-cli"
    vlm_bin.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if '--help' in sys.argv:\n"
        " print('-m --model --image -f -t -c -n --temp -ngl --mmproj --no-mmproj-offload --image-min-tokens --image-max-tokens'); raise SystemExit(0)\n"
        "print('{\"summary\":\"파란 이미지\",\"scene\":{\"location_type\":\"알 수 없음\",\"time_or_lighting\":\"밝음\",\"weather\":\"\"},\"subjects\":[{\"name\":\"파란 면\",\"description\":\"단색 표면\",\"position\":\"중앙\",\"confidence\":0.9}],\"visible_text\":[],\"colors_and_composition\":{\"dominant_colors\":[\"파랑\"],\"composition\":\"단순함\",\"mood_from_visuals\":\"차분함\"},\"blog_worthy_details\":[\"색감\"],\"uncertainties\":[],\"privacy_notes\":[],\"raw_caption\":\"파란 이미지\"}')\n",
        encoding="utf-8",
    )
    llm_bin.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if '--help' in sys.argv:\n"
        " print('-m --model -f -t --threads-batch -c -n --temp --top-p --top-k --repeat-penalty -b -ub -ngl --parallel --cache-type-k --cache-type-v --flash-attn'); raise SystemExit(0)\n"
        "body = '사진에 근거한 충분한 길이의 테스트 본문입니다. ' * 30\n"
        "print('# 제목\\n\\n짧은 도입 문단\\n\\n## 사진에서 가장 먼저 눈에 들어오는 것\\n\\n' + body + '\\n\\n## 장면을 자세히 살펴보면\\n\\n' + body + '\\n\\n## 사진이 전하는 분위기\\n\\n' + body + '\\n\\n## 마무리\\n\\n' + body + '\\n\\n---\\n\\n**이미지 대체 텍스트:** 파란 이미지\\n\\n**메타 설명:** 파란 이미지를 소개합니다.\\n\\n**추천 태그:** 사진, 색감, 기록')\n",
        encoding="utf-8",
    )
    os.chmod(vlm_bin, 0o755)
    os.chmod(llm_bin, 0o755)
    vlm_model = root / "models/vlm/qwen-vl-Q4_K_M.gguf"
    mmproj = root / "models/vlm/mmproj.gguf"
    llm_model = root / "models/llm/qwen-IQ2_M.gguf"
    for path in (vlm_model, mmproj, llm_model):
        path.write_bytes(b"gguf")
    return AppConfig(
        root=root,
        llama_cli=llm_bin,
        llama_mtmd_cli=vlm_bin,
        vlm_hf_repo="vlm/repo",
        vlm_quant="Q4_K_M",
        vlm_model_path=vlm_model,
        vlm_mmproj_path=mmproj,
        llm_hf_repo="llm/repo",
        llm_quant_preferences=("IQ2_M", "Q2_K"),
        llm_model_path=llm_model,
        max_llm_gguf_gib=12.5,
        allow_oversized_model=False,
        model_dir=root / "models",
        run_dir=root / "runs",
        output_dir=root / "outputs",
        threads=4,
        vlm_ctx_size=2048,
        llm_ctx_size=4096,
        vlm_max_tokens=512,
        llm_max_tokens=1600,
        min_available_before_vlm_mb=1,
        min_available_before_llm_mb=1,
        min_available_during_run_mb=1,
        memory_check_interval_sec=0.01,
        memory_recovery_timeout_sec=1,
        vlm_timeout_sec=5,
        llm_timeout_sec=5,
        process_terminate_grace_sec=1,
        max_image_edge=896,
        jpeg_quality=88,
        enable_flash_attn=False,
        enable_swap_warning=True,
        allow_cache_drop=False,
        blog_language="ko",
    )
