"""config/*.yaml 로더.

모델 경로, 임계값, 문체, 금칙어를 전부 외부화한다. 코드에는 스키마만 있고 값은 없다.
기존 .env 기반 AppConfig는 단일 이미지 CLI 경로에서 계속 쓰이므로 건드리지 않는다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .schemas import ConfigError


def _resolve(root: Path, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else (root / path).resolve()


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"{where}에 필수 항목 '{key}'가 없습니다")
    return mapping[key]


@dataclass(frozen=True)
class ModelTier:
    name: str
    kind: str  # "vlm" | "llm"
    backend: str  # llamacpp_mtmd | llamacpp_server | ollama
    raw: dict[str, Any]
    root: Path

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    def path(self, key: str) -> Path | None:
        return _resolve(self.root, self.raw.get(key))

    @property
    def binary(self) -> Path | None:
        return self.path("binary")

    @property
    def model_path(self) -> Path | None:
        return self.path("model_path")

    @property
    def mmproj_path(self) -> Path | None:
        return self.path("mmproj_path")

    @property
    def expected_rss_mb(self) -> int:
        return int(self.raw.get("expected_rss_mb", 0))

    @property
    def min_free_mb(self) -> int | None:
        value = self.raw.get("min_free_mb")
        return int(value) if value is not None else None

    def is_available(self) -> tuple[bool, str]:
        """이 tier를 실제로 실행할 수 있는지 파일 수준에서 확인한다."""
        if self.backend == "ollama":
            # ollama 데몬 가용성은 런타임에서 확인한다. 여기서는 모델 이름만 요구한다.
            if not self.raw.get("model"):
                return False, "ollama tier에 model 이름이 없습니다"
            return True, ""
        binary = self.binary
        if binary is None or not binary.exists():
            return False, f"실행 파일 없음: {binary}"
        model = self.model_path
        if model is None or not model.exists():
            return False, f"모델 파일 없음: {model}"
        if self.kind == "vlm" and self.backend == "llamacpp_mtmd":
            mmproj = self.mmproj_path
            if mmproj is None or not mmproj.exists():
                return False, f"mmproj 파일 없음: {mmproj}"
        return True, ""


@dataclass(frozen=True)
class BannedPattern:
    name: str
    regex: re.Pattern[str]
    reason: str


@dataclass(frozen=True)
class BlogSection:
    id: str
    title: str
    kind: str
    guidance: str
    min_items: int = 0
    max_items: int = 0


@dataclass(frozen=True)
class Settings:
    root: Path
    models: dict[str, Any]
    blog: dict[str, Any]
    runtime: dict[str, Any]
    vlm_tiers: tuple[ModelTier, ...] = field(default_factory=tuple)
    llm_tiers: tuple[ModelTier, ...] = field(default_factory=tuple)

    # ---- 경로 ----
    def dir_of(self, key: str) -> Path:
        paths = self.runtime.get("paths", {})
        raw = _require(paths, key, "runtime.yaml:paths")
        resolved = _resolve(self.root, raw)
        assert resolved is not None
        return resolved

    @property
    def inbox_dir(self) -> Path:
        return self.dir_of("inbox_dir")

    @property
    def work_dir(self) -> Path:
        return self.dir_of("work_dir")

    @property
    def output_dir(self) -> Path:
        return self.dir_of("output_dir")

    @property
    def state_dir(self) -> Path:
        return self.dir_of("state_dir")

    @property
    def logs_dir(self) -> Path:
        return self.dir_of("logs_dir")

    @property
    def lock_path(self) -> Path:
        raw = self.runtime.get("lock", {}).get("path", "state/run.lock")
        resolved = _resolve(self.root, raw)
        assert resolved is not None
        return resolved

    # ---- 그룹 접근자 ----
    @property
    def memory(self) -> dict[str, Any]:
        return self.runtime.get("memory", {})

    @property
    def thermal(self) -> dict[str, Any]:
        return self.runtime.get("thermal", {})

    @property
    def timeouts(self) -> dict[str, Any]:
        return self.runtime.get("timeouts", {})

    @property
    def images(self) -> dict[str, Any]:
        return self.runtime.get("images", {})

    @property
    def privacy(self) -> dict[str, Any]:
        return self.runtime.get("privacy", {})

    @property
    def gui(self) -> dict[str, Any]:
        return self.runtime.get("gui", {})

    @property
    def gates(self) -> dict[str, Any]:
        return self.models.get("gates", {})

    # ---- blog.yaml ----
    @property
    def sections(self) -> tuple[BlogSection, ...]:
        result = []
        for item in self.blog.get("sections", []):
            result.append(
                BlogSection(
                    id=_require(item, "id", "blog.yaml:sections"),
                    title=_require(item, "title", "blog.yaml:sections"),
                    kind=item.get("kind", "prose"),
                    guidance=item.get("guidance", ""),
                    min_items=int(item.get("min_items", 0)),
                    max_items=int(item.get("max_items", 0)),
                )
            )
        if not result:
            raise ConfigError("blog.yaml에 sections가 비어 있습니다")
        return tuple(result)

    @property
    def banned_patterns(self) -> tuple[BannedPattern, ...]:
        result = []
        for item in self.blog.get("quality", {}).get("banned_patterns", []):
            try:
                compiled = re.compile(item["pattern"])
            except re.error as exc:
                raise ConfigError(f"blog.yaml 금칙 패턴 '{item.get('name')}' 컴파일 실패: {exc}") from exc
            result.append(BannedPattern(name=item["name"], regex=compiled, reason=item.get("reason", "")))
        return tuple(result)

    @property
    def quality_limits(self) -> dict[str, Any]:
        return self.blog.get("quality", {}).get("limits", {})

    @property
    def categories(self) -> tuple[str, ...]:
        return tuple(self.blog.get("categories", []))

    # ---- tier 선택 ----
    def available_tiers(self, kind: str) -> list[tuple[ModelTier, bool, str]]:
        tiers = self.vlm_tiers if kind == "vlm" else self.llm_tiers
        report = []
        for tier in tiers:
            ok, reason = tier.is_available()
            report.append((tier, ok, reason))
        return report

    @classmethod
    def load(cls, root: Path | None = None) -> "Settings":
        project_root = (root or Path.cwd()).resolve()
        config_dir = project_root / "config"
        loaded: dict[str, dict[str, Any]] = {}
        for name in ("models", "blog", "runtime"):
            path = config_dir / f"{name}.yaml"
            if not path.exists():
                raise ConfigError(f"설정 파일이 없습니다: {path}")
            with path.open("r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle)
            if not isinstance(data, dict):
                raise ConfigError(f"{path}의 최상위는 매핑이어야 합니다")
            loaded[name] = data

        def build_tiers(kind: str) -> tuple[ModelTier, ...]:
            section = loaded["models"].get(kind, {})
            raw_tiers = section.get("tiers", [])
            if not raw_tiers:
                raise ConfigError(f"models.yaml에 {kind} tier가 없습니다")
            defaults = loaded["models"].get("defaults", {})
            built = []
            for item in raw_tiers:
                merged = {**defaults, **item}
                built.append(
                    ModelTier(
                        name=_require(merged, "name", f"models.yaml:{kind}"),
                        kind=kind,
                        backend=_require(merged, "backend", f"models.yaml:{kind}"),
                        raw=merged,
                        root=project_root,
                    )
                )
            return tuple(built)

        return cls(
            root=project_root,
            models=loaded["models"],
            blog=loaded["blog"],
            runtime=loaded["runtime"],
            vlm_tiers=build_tiers("vlm"),
            llm_tiers=build_tiers("llm"),
        )
