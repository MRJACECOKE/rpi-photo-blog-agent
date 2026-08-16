from __future__ import annotations

from pathlib import Path

from .config import AppConfig
from .llama_options import LlamaHelp, append_option
from .schemas import ModelSelection


def build_vlm_command(config: AppConfig, model: ModelSelection, image_path: Path, prompt_path: Path) -> list[str]:
    help_text = LlamaHelp(config.llama_mtmd_cli)
    args = [str(config.llama_mtmd_cli)]
    append_option(args, help_text, ("-m", "--model"), model.primary_path)
    if model.mmproj_path:
        append_option(args, help_text, ("--mmproj", "--mmproj-file"), model.mmproj_path)
    append_option(args, help_text, ("--image", "--image-file", "--img"), image_path)
    append_option(args, help_text, ("-f", "--file"), prompt_path)
    append_option(args, help_text, ("-t", "--threads"), config.threads)
    append_option(args, help_text, ("-c", "--ctx-size", "--ctx"), config.vlm_ctx_size)
    append_option(args, help_text, ("-n", "--n-predict"), config.vlm_max_tokens)
    append_option(args, help_text, ("--temp", "--temperature"), 0.2)
    append_option(args, help_text, ("-ngl", "--gpu-layers", "--n-gpu-layers"), 0)
    if help_text.supports("--no-mmproj-offload"):
        args.append("--no-mmproj-offload")
    return args
