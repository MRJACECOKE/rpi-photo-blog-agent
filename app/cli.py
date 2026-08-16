from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import AppConfig
from .orchestrator import PhotoBlogOrchestrator
from .schemas import AgentError, RunRequest


def configure_logging(config: AppConfig, verbose: bool = False) -> None:
    (config.root / "logs").mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    log_path = config.root / "logs" / "agent.log"
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Raspberry Pi local photo blog agent")
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--topic", default="")
    parser.add_argument("--audience", default="")
    parser.add_argument("--tone", default="")
    parser.add_argument("--keywords", default="")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--language", default="ko")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-run-files", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--vlm-model", type=Path)
    parser.add_argument("--vlm-mmproj", type=Path)
    parser.add_argument("--llm-model", type=Path)
    parser.add_argument("--force-oversized-model", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = AppConfig.load(Path.cwd())
    configure_logging(config, args.verbose)
    request = RunRequest(
        image=args.image,
        topic=args.topic,
        audience=args.audience,
        tone=args.tone,
        keywords=args.keywords,
        output=args.output,
        language=args.language,
        dry_run=args.dry_run,
        keep_run_files=args.keep_run_files,
        verbose=args.verbose,
        vlm_model=args.vlm_model,
        vlm_mmproj=args.vlm_mmproj,
        llm_model=args.llm_model,
        force_oversized_model=args.force_oversized_model,
    )
    try:
        result = PhotoBlogOrchestrator(config).run(request)
    except AgentError as exc:
        logging.getLogger(__name__).error("%s", exc)
        return 2
    except KeyboardInterrupt as exc:
        logging.getLogger(__name__).error("interrupted: %s", exc)
        return 130
    if args.dry_run:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"run_id": result["run_id"], "output": result["output"], "run_dir": result["run_dir"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
