from __future__ import annotations

import subprocess
from pathlib import Path


class LlamaHelp:
    def __init__(self, binary: Path) -> None:
        self.binary = binary
        self.text = self._read_help(binary)

    @staticmethod
    def _read_help(binary: Path) -> str:
        if not binary.exists():
            return ""
        for flag in ("--help", "-h"):
            try:
                result = subprocess.run([str(binary), flag], check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", timeout=20)
                if result.stdout:
                    return result.stdout
            except (OSError, subprocess.SubprocessError):
                continue
        return ""

    def supports(self, *names: str) -> bool:
        if not self.text:
            return True
        return any(name in self.text for name in names)


def append_option(args: list[str], help_text: LlamaHelp, names: tuple[str, ...], value: str | int | float | None = None) -> None:
    chosen = next((name for name in names if help_text.supports(name)), None)
    if chosen is None:
        return
    args.append(chosen)
    if value is not None:
        args.append(str(value))
