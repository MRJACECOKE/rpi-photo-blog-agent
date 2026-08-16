from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml


def load_prompts(path: Path) -> dict[str, str]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return {str(key): str(value) for key, value in data.items()}


def build_vision_prompt(prompts: dict[str, str]) -> str:
    return prompts["vision_system"].strip() + "\n\n" + prompts["vision_schema"].strip() + "\n"


def build_blog_prompt(
    prompts: dict[str, str],
    vision_data: dict[str, Any],
    topic: str,
    audience: str,
    tone: str,
    keywords: str,
    language: str = "ko",
) -> str:
    data = json.dumps(vision_data, ensure_ascii=False, indent=2)
    user_data = json.dumps(
        {
            "topic": topic,
            "audience": audience,
            "tone": tone,
            "keywords": keywords,
            "language": language,
        },
        ensure_ascii=False,
        indent=2,
    )
    return (
        prompts["blog_system"].strip()
        + "\n\n<vision_data>\n"
        + data
        + "\n</vision_data>\n\n<user_blog_preferences>\n"
        + user_data
        + "\n</user_blog_preferences>\n"
    )
