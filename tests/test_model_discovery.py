from __future__ import annotations

from pathlib import Path

import pytest

from app.model_discovery import RemoteFile, discover_local_shards, local_model_selection, select_remote_gguf
from app.schemas import ModelSelectionError


def test_oversized_model_rejected(tmp_path: Path) -> None:
    model = tmp_path / "huge-IQ2_M.gguf"
    with model.open("wb") as handle:
        handle.truncate(13 * 1024**3)
    with pytest.raises(ModelSelectionError):
        local_model_selection("repo", "IQ2_M", model, max_gib=12.5, allow_oversized=False)


def test_split_gguf_size_is_summed() -> None:
    files = [
        RemoteFile("qwen-IQ2_M-00001-of-00002.gguf", 10),
        RemoteFile("qwen-IQ2_M-00002-of-00002.gguf", 20),
        RemoteFile("qwen-Q4_K_M.gguf", 100),
    ]
    selection = select_remote_gguf(files, ("IQ2_M",), "repo", Path("models/llm"))
    assert selection.total_bytes == 30
    assert selection.primary_path.name == "qwen-IQ2_M-00001-of-00002.gguf"


def test_missing_local_shard_rejected(tmp_path: Path) -> None:
    first = tmp_path / "qwen-IQ2_M-00001-of-00002.gguf"
    first.write_bytes(b"x")
    with pytest.raises(ModelSelectionError):
        discover_local_shards(first)
