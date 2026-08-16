#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import AppConfig
from app.model_discovery import RemoteFile, list_hf_files, select_remote_gguf


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download GGUF models for the Raspberry Pi photo blog agent")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--vlm-only", action="store_true")
    parser.add_argument("--llm-only", action="store_true")
    parser.add_argument("--quant", help="override LLM quantization preference")
    return parser.parse_args()


def disk_free(path: Path) -> int:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free


def ensure_space(path: Path, needed: int) -> None:
    free = disk_free(path)
    if free < needed:
        raise SystemExit(f"FAIL: 디스크 공간 부족: 필요 {needed / 1024**3:.2f} GiB, 여유 {free / 1024**3:.2f} GiB")


def download_files(repo_id: str, files: list[str], target_dir: Path, dry_run: bool) -> None:
    print(f"선택 저장소: {repo_id}")
    for filename in files:
        print(f"  - {filename}")
    if dry_run:
        return
    from huggingface_hub import hf_hub_download

    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
    for filename in files:
        hf_hub_download(repo_id=repo_id, filename=filename, local_dir=target_dir, local_dir_use_symlinks=False, token=token)
        path = target_dir / filename
        if not path.exists() or path.stat().st_size == 0:
            raise SystemExit(f"FAIL: 다운로드 결과가 올바르지 않습니다: {path}")


def select_vlm(files: list[RemoteFile], quant: str, repo_id: str, target_dir: Path) -> tuple[list[str], int]:
    bodies = [item for item in files if item.path.lower().endswith(".gguf") and quant.lower() in item.path.lower() and "mmproj" not in item.path.lower()]
    mmprojs = [item for item in files if item.path.lower().endswith(".gguf") and "mmproj" in item.path.lower()]
    if not bodies:
        raise SystemExit(f"FAIL: VLM {quant} GGUF 파일을 찾지 못했습니다.")
    if not mmprojs:
        raise SystemExit("FAIL: VLM mmproj GGUF 파일을 찾지 못했습니다.")
    body = sorted(bodies, key=lambda item: item.size)[0]
    mmproj = sorted(mmprojs, key=lambda item: item.size)[0]
    return [body.path, mmproj.path], body.size + mmproj.size


def main() -> int:
    args = parse_args()
    config = AppConfig.load(ROOT)
    do_vlm = not args.llm_only
    do_llm = not args.vlm_only

    if do_vlm:
        print("[VLM] Hugging Face 파일 목록 조회")
        vlm_files = list_hf_files(config.vlm_hf_repo)
        selected, total = select_vlm(vlm_files, config.vlm_quant, config.vlm_hf_repo, config.model_dir / "vlm")
        print(f"[VLM] 선택 파일 합계: {total / 1024**3:.2f} GiB")
        ensure_space(config.model_dir / "vlm", total)
        download_files(config.vlm_hf_repo, selected, config.model_dir / "vlm", args.dry_run)

    if do_llm:
        print("[LLM] Hugging Face 파일 목록 조회")
        prefs = (args.quant,) if args.quant else config.llm_quant_preferences
        llm_files = list_hf_files(config.llm_hf_repo)
        selection = select_remote_gguf(llm_files, prefs, config.llm_hf_repo, config.model_dir / "llm")
        limit = int(config.max_llm_gguf_gib * 1024**3)
        print(f"[LLM] 선택 양자화: {selection.quantization}")
        print(f"[LLM] 선택 파일 합계: {selection.total_bytes / 1024**3:.2f} GiB")
        if selection.total_bytes > limit and not config.allow_oversized_model:
            raise SystemExit(f"FAIL: {selection.total_bytes / 1024**3:.2f} GiB가 제한 {config.max_llm_gguf_gib:.2f} GiB를 초과합니다.")
        if selection.total_bytes > limit:
            print("WARN: oversized LLM 모델을 허용했습니다. Raspberry Pi가 메모리 압박을 받을 수 있습니다.")
        ensure_space(config.model_dir / "llm", selection.total_bytes)
        download_files(config.llm_hf_repo, [str(path.relative_to(config.model_dir / "llm")) for path in selection.files], config.model_dir / "llm", args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
