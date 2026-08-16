from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .schemas import ConfigError


def parse_bool(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


@dataclass(frozen=True)
class AppConfig:
    root: Path
    llama_cli: Path
    llama_mtmd_cli: Path
    vlm_hf_repo: str
    vlm_quant: str
    vlm_model_path: Path | None
    vlm_mmproj_path: Path | None
    llm_hf_repo: str
    llm_quant_preferences: tuple[str, ...]
    llm_model_path: Path | None
    max_llm_gguf_gib: float
    allow_oversized_model: bool
    model_dir: Path
    run_dir: Path
    output_dir: Path
    threads: int
    vlm_ctx_size: int
    llm_ctx_size: int
    vlm_max_tokens: int
    llm_max_tokens: int
    min_available_before_vlm_mb: int
    min_available_before_llm_mb: int
    min_available_during_run_mb: int
    memory_check_interval_sec: float
    memory_recovery_timeout_sec: float
    vlm_timeout_sec: int
    llm_timeout_sec: int
    process_terminate_grace_sec: int
    max_image_edge: int
    jpeg_quality: int
    enable_flash_attn: bool
    enable_swap_warning: bool
    allow_cache_drop: bool
    blog_language: str
    strict_dry_run: bool = False

    @classmethod
    def load(cls, root: Path | None = None) -> "AppConfig":
        project_root = (root or Path.cwd()).resolve()
        env_file = load_dotenv(project_root / ".env")

        def val(key: str, default: str = "") -> str:
            return os.environ.get(key, env_file.get(key, default))

        def path_val(key: str, default: str = "") -> Path | None:
            raw = val(key, default).strip()
            if not raw:
                return None
            path = Path(raw)
            return path if path.is_absolute() else (project_root / path).resolve()

        def required_int(key: str, default: str) -> int:
            try:
                return int(val(key, default))
            except ValueError as exc:
                raise ConfigError(f"{key} must be an integer") from exc

        def required_float(key: str, default: str) -> float:
            try:
                return float(val(key, default))
            except ValueError as exc:
                raise ConfigError(f"{key} must be a number") from exc

        model_dir = path_val("MODEL_DIR", "models") or (project_root / "models")
        run_dir = path_val("RUN_DIR", "runs") or (project_root / "runs")
        output_dir = path_val("OUTPUT_DIR", "outputs") or (project_root / "outputs")
        prefs = tuple(p.strip() for p in val("LLM_QUANT_PREFERENCES", "IQ2_M,Q2_K_L,Q2_K,IQ2_S").split(",") if p.strip())
        return cls(
            root=project_root,
            llama_cli=path_val("LLAMA_CLI", "third_party/llama.cpp/build/bin/llama-completion") or project_root / "third_party/llama.cpp/build/bin/llama-completion",
            llama_mtmd_cli=path_val("LLAMA_MTMD_CLI", "third_party/llama.cpp/build/bin/llama-mtmd-cli") or project_root / "third_party/llama.cpp/build/bin/llama-mtmd-cli",
            vlm_hf_repo=val("VLM_HF_REPO", "ggml-org/Qwen2.5-VL-3B-Instruct-GGUF"),
            vlm_quant=val("VLM_QUANT", "Q4_K_M"),
            vlm_model_path=path_val("VLM_MODEL_PATH"),
            vlm_mmproj_path=path_val("VLM_MMPROJ_PATH"),
            llm_hf_repo=val("LLM_HF_REPO", "unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF"),
            llm_quant_preferences=prefs,
            llm_model_path=path_val("LLM_MODEL_PATH"),
            max_llm_gguf_gib=required_float("MAX_LLM_GGUF_GIB", "12.5"),
            allow_oversized_model=parse_bool(val("ALLOW_OVERSIZED_MODEL", "false")),
            model_dir=model_dir,
            run_dir=run_dir,
            output_dir=output_dir,
            threads=required_int("THREADS", "4"),
            vlm_ctx_size=required_int("VLM_CTX_SIZE", "2048"),
            llm_ctx_size=required_int("LLM_CTX_SIZE", "3072"),
            vlm_max_tokens=required_int("VLM_MAX_TOKENS", "640"),
            llm_max_tokens=required_int("LLM_MAX_TOKENS", "1200"),
            min_available_before_vlm_mb=required_int("MIN_AVAILABLE_BEFORE_VLM_MB", "4096"),
            min_available_before_llm_mb=required_int("MIN_AVAILABLE_BEFORE_LLM_MB", "12288"),
            min_available_during_run_mb=required_int("MIN_AVAILABLE_DURING_RUN_MB", "1536"),
            memory_check_interval_sec=required_float("MEMORY_CHECK_INTERVAL_SEC", "1"),
            memory_recovery_timeout_sec=required_float("MEMORY_RECOVERY_TIMEOUT_SEC", "120"),
            vlm_timeout_sec=required_int("VLM_TIMEOUT_SEC", "1800"),
            llm_timeout_sec=required_int("LLM_TIMEOUT_SEC", "7200"),
            process_terminate_grace_sec=required_int("PROCESS_TERMINATE_GRACE_SEC", "15"),
            max_image_edge=required_int("MAX_IMAGE_EDGE", "896"),
            jpeg_quality=required_int("JPEG_QUALITY", "88"),
            enable_flash_attn=parse_bool(val("ENABLE_FLASH_ATTN", "false")),
            enable_swap_warning=parse_bool(val("ENABLE_SWAP_WARNING", "true"), True),
            allow_cache_drop=parse_bool(val("ALLOW_CACHE_DROP", "false")),
            blog_language=val("BLOG_LANGUAGE", "ko"),
            strict_dry_run=parse_bool(val("STRICT_DRY_RUN", "false")),
        )
