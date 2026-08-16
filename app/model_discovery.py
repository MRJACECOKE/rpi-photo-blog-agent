from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .schemas import ModelSelection, ModelSelectionError

GGUF_RE = re.compile(r"\.gguf(?:\.\d+)?$", re.IGNORECASE)
SHARD_RE = re.compile(r"^(?P<prefix>.+)-(?P<idx>\d{5})-of-(?P<total>\d{5})\.gguf$", re.IGNORECASE)


@dataclass(frozen=True)
class RemoteFile:
    path: str
    size: int


def quant_in_name(name: str, quant: str) -> bool:
    return quant.lower() in name.lower()


def shard_group_key(name: str) -> str:
    match = SHARD_RE.match(Path(name).name)
    if match:
        return match.group("prefix")
    return Path(name).stem


def group_gguf_files(files: Iterable[RemoteFile]) -> dict[str, list[RemoteFile]]:
    groups: dict[str, list[RemoteFile]] = {}
    for item in files:
        if not GGUF_RE.search(item.path):
            continue
        key = shard_group_key(item.path)
        groups.setdefault(key, []).append(item)
    for values in groups.values():
        values.sort(key=lambda item: item.path)
    return groups


def select_remote_gguf(files: list[RemoteFile], quant_preferences: tuple[str, ...], repo_id: str, target_dir: Path) -> ModelSelection:
    groups = group_gguf_files(files)
    for quant in quant_preferences:
        candidates = [(name, shards) for name, shards in groups.items() if any(quant_in_name(shard.path, quant) for shard in shards)]
        if not candidates:
            continue
        candidates.sort(key=lambda pair: sum(shard.size for shard in pair[1]))
        _name, shards = candidates[0]
        paths = tuple(target_dir / shard.path for shard in shards)
        return ModelSelection(
            repo_id=repo_id,
            quantization=quant,
            files=paths,
            primary_path=paths[0],
            total_bytes=sum(shard.size for shard in shards),
        )
    raise ModelSelectionError(f"No GGUF files matched quantization preferences: {', '.join(quant_preferences)}")


def local_model_selection(repo_id: str, quantization: str, primary: Path, max_gib: float | None = None, allow_oversized: bool = False) -> ModelSelection:
    primary = primary.expanduser().resolve()
    if not primary.exists():
        raise ModelSelectionError(f"model path does not exist: {primary}")
    files = discover_local_shards(primary)
    total = sum(path.stat().st_size for path in files)
    if max_gib is not None and total > int(max_gib * 1024**3) and not allow_oversized:
        raise ModelSelectionError(f"model files total {total / 1024**3:.2f} GiB exceeds {max_gib:.2f} GiB")
    return ModelSelection(repo_id=repo_id, quantization=quantization, files=tuple(files), primary_path=primary, total_bytes=total, oversized=max_gib is not None and total > int(max_gib * 1024**3))


def discover_local_shards(primary: Path) -> list[Path]:
    name = primary.name
    match = SHARD_RE.match(name)
    if not match:
        return [primary]
    prefix = match.group("prefix")
    total = int(match.group("total"))
    files = [primary.parent / f"{prefix}-{idx:05d}-of-{total:05d}.gguf" for idx in range(1, total + 1)]
    missing = [str(path) for path in files if not path.exists()]
    if missing:
        raise ModelSelectionError(f"missing GGUF shard(s): {', '.join(missing)}")
    return files


def list_hf_files(repo_id: str) -> list[RemoteFile]:
    try:
        from huggingface_hub import HfApi
    except ImportError as exc:
        raise ModelSelectionError("huggingface_hub is required for model discovery/download") from exc
    api = HfApi()
    siblings = api.model_info(repo_id, files_metadata=True).siblings
    result: list[RemoteFile] = []
    for item in siblings:
        size = int(getattr(item, "size", 0) or 0)
        result.append(RemoteFile(path=item.rfilename, size=size))
    return result


def find_local_downloaded_model(repo_id: str, model_dir: Path, quant_preferences: tuple[str, ...]) -> ModelSelection | None:
    target = model_dir / "llm"
    files = [RemoteFile(path=path.name, size=path.stat().st_size) for path in target.glob("*.gguf*") if path.is_file()]
    if not files:
        return None
    try:
        selection = select_remote_gguf(files, quant_preferences, repo_id, target)
        files_existing = tuple(path for path in selection.files if path.exists())
        if len(files_existing) != len(selection.files):
            return None
        return selection
    except ModelSelectionError:
        return None


def find_local_vlm(repo_id: str, model_dir: Path, quant: str) -> ModelSelection | None:
    target = model_dir / "vlm"
    ggufs = [path for path in target.glob("*.gguf") if quant_in_name(path.name, quant) and "mmproj" not in path.name.lower()]
    if not ggufs:
        return None
    primary = sorted(ggufs, key=lambda p: p.stat().st_size)[0]
    mmproj_candidates = sorted([p for p in target.glob("*.gguf") if "mmproj" in p.name.lower()])
    return ModelSelection(
        repo_id=repo_id,
        quantization=quant,
        files=(primary,),
        primary_path=primary,
        total_bytes=primary.stat().st_size,
        mmproj_path=mmproj_candidates[0] if mmproj_candidates else None,
    )
