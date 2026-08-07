#!/usr/bin/env python3
"""Replay timestamped recognized speech through the text citation detector."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_SRC = PROJECT_ROOT / "packages" / "bible_parser_core" / "src"
for import_path in (PROJECT_ROOT, CORE_SRC):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from bible_parser_core.bible_text_search import BibleTextSearcher
from bible_parser_core.text_citation_detector import ScriptureTextDetector, TextDetectionConfig


def load_transcript(path: Path) -> list[dict]:
    fragments: list[dict] = []
    with path.open("r", encoding="utf-8-sig") as file:
        for line_number, raw_line in enumerate(file, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                item = json.loads(line)
                timestamp = float(item["time"])
                text = str(item["text"]).strip()
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"Некорректная строка {line_number}: {exc}") from exc
            if text:
                fragments.append({"time": timestamp, "text": text})
    return fragments


def replay_transcript(path: Path, detector: ScriptureTextDetector) -> list[dict]:
    rows: list[dict] = []
    for fragment in load_transcript(path):
        decision = detector.process_fragment(fragment["text"], fragment["time"])
        row = {
            **fragment,
            "accepted": decision.accepted,
            "reference": decision.reference,
            "score": round(decision.score, 3),
            "margin": round(decision.margin, 3),
            "matched_words": decision.matched_words,
            "window": decision.window_text,
            "reason": decision.reason,
            "confirmations": decision.confirmations,
        }
        rows.append(row)
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Прогнать JSONL-транскрипцию через текстовый детектор LiVerse без микрофона."
    )
    parser.add_argument("transcript", type=Path, help="JSONL со строками time и text.")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("bible_index/bible_index.db"),
        help="Путь к bible_index.db.",
    )
    parser.add_argument("--output", type=Path, help="Записать JSONL-результат в файл.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        with BibleTextSearcher(args.db) as searcher:
            detector = ScriptureTextDetector(searcher, TextDetectionConfig())
            rows = replay_transcript(args.transcript, detector)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Ошибка replay: {exc}", file=sys.stderr)
        return 1

    rendered = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(f"Результат: {args.output.resolve()}")
    else:
        print(rendered, end="")
    accepted = sum(bool(row["accepted"]) for row in rows)
    print(f"Фрагментов: {len(rows)}; принятых цитат: {accepted}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
