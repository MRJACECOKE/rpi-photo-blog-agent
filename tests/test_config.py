from __future__ import annotations

from pathlib import Path

import pytest

from app.config import AppConfig, load_dotenv, parse_bool
from app.schemas import ConfigError


CONFIG_ENV_KEYS = (
    "LLAMA_CLI",
    "THREADS",
    "MAX_LLM_GGUF_GIB",
    "ALLOW_OVERSIZED_MODEL",
    "LLM_QUANT_PREFERENCES",
    "MODEL_DIR",
)


@pytest.fixture(autouse=True)
def clear_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in CONFIG_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_load_dotenv_skips_comments_and_strips_quotes(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n"
        "# comment\n"
        "THREADS='8'\n"
        "BLOG_LANGUAGE=\"ko\"\n"
        "IGNORED_LINE\n",
        encoding="utf-8",
    )

    assert load_dotenv(env_file) == {"THREADS": "8", "BLOG_LANGUAGE": "ko"}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, True),
        (True, True),
        ("yes", True),
        ("ON", True),
        ("0", False),
        ("false", False),
    ],
)
def test_parse_bool(value: str | bool | None, expected: bool) -> None:
    assert parse_bool(value, default=True) is expected


def test_app_config_load_uses_dotenv_and_resolves_relative_paths(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "THREADS=6\n"
        "MAX_LLM_GGUF_GIB=9.5\n"
        "ALLOW_OVERSIZED_MODEL=true\n"
        "LLM_QUANT_PREFERENCES=Q2_K, IQ2_S,,\n"
        "MODEL_DIR=custom-models\n",
        encoding="utf-8",
    )

    config = AppConfig.load(tmp_path)

    assert config.threads == 6
    assert config.max_llm_gguf_gib == 9.5
    assert config.allow_oversized_model is True
    assert config.llm_quant_preferences == ("Q2_K", "IQ2_S")
    assert config.model_dir == (tmp_path / "custom-models").resolve()


def test_environment_overrides_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("THREADS=6\n", encoding="utf-8")
    monkeypatch.setenv("THREADS", "2")

    assert AppConfig.load(tmp_path).threads == 2


def test_app_config_load_rejects_invalid_numbers(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("THREADS=fast\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="THREADS must be an integer"):
        AppConfig.load(tmp_path)


def test_safe_rpi_defaults_use_sequential_completion_profile(tmp_path: Path) -> None:
    config = AppConfig.load(tmp_path)

    assert config.llama_cli.name == "llama-completion"
    assert config.llm_ctx_size == 3072
    assert config.vlm_max_tokens == 640
    assert config.llm_max_tokens == 1200
    assert config.min_available_before_llm_mb == 12288
    assert config.min_available_during_run_mb == 1536
