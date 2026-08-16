from __future__ import annotations

from pathlib import Path

from .config import AppConfig
from .llama_options import LlamaHelp, append_option
from .schemas import ModelSelection


def build_llm_command(config: AppConfig, model: ModelSelection, prompt_path: Path) -> list[str]:
    help_text = LlamaHelp(config.llama_cli)
    args = [str(config.llama_cli)]
    append_option(args, help_text, ("-m", "--model"), model.primary_path)
    append_option(args, help_text, ("-f", "--file"), prompt_path)
    append_option(args, help_text, ("-t", "--threads"), config.threads)
    append_option(args, help_text, ("--threads-batch", "--threads-batch"), config.threads)
    append_option(args, help_text, ("-c", "--ctx-size", "--ctx"), config.llm_ctx_size)
    append_option(args, help_text, ("-n", "--n-predict"), config.llm_max_tokens)
    append_option(args, help_text, ("--temp", "--temperature"), 0.65)
    append_option(args, help_text, ("--top-p",), 0.9)
    append_option(args, help_text, ("--top-k",), 40)
    append_option(args, help_text, ("--repeat-penalty", "--repeat_penalty"), 1.08)
    append_option(args, help_text, ("-b", "--batch-size", "--batch_size"), 128)
    append_option(args, help_text, ("-ub", "--ubatch-size", "--ubatch-size"), 64)
    append_option(args, help_text, ("-ngl", "--gpu-layers", "--n-gpu-layers"), 0)
    append_option(args, help_text, ("--parallel",), 1)
    if help_text.supports("-no-cnv"):
        args.append("-no-cnv")
    elif help_text.supports("--no-conversation"):
        args.append("--no-conversation")
    if help_text.supports("--simple-io"):
        args.append("--simple-io")
    if help_text.supports("--no-display-prompt"):
        args.append("--no-display-prompt")
    if help_text.supports("--no-mmap"):
        pass
    if help_text.supports("--mlock"):
        pass
    if help_text.supports("--cache-type-k"):
        append_option(args, help_text, ("--cache-type-k",), "q8_0")
    if help_text.supports("--cache-type-v"):
        append_option(args, help_text, ("--cache-type-v",), "q8_0")
    if config.enable_flash_attn:
        if help_text.supports("--flash-attn"):
            args.append("--flash-attn")
        elif help_text.supports("-fa"):
            args.append("-fa")
    return args
