#!/usr/bin/env python3
"""Summarize vosk_grammar_probe JSONL logs."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


DEFAULT_LOG_DIR = Path(".cache/live_verse_vosk/vosk_probe")


def event_paths(log_dir: Path) -> list[Path]:
    if log_dir.is_file():
        return [log_dir]
    return sorted(log_dir.glob("*/events.jsonl"))


def iter_events(paths: list[Path]):
    for path in paths:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                yield path, line_number, {"event": "invalid_json", "raw": line}
                continue
            yield path, line_number, event


def summarize(log_dir: Path) -> dict:
    final_texts: Counter[str] = Counter()
    unmatched_texts: Counter[str] = Counter()
    refs: Counter[str] = Counter()
    books: Counter[str] = Counter()
    range_refs: Counter[str] = Counter()
    attempts: Counter[str] = Counter()
    event_count = 0

    for _path, _line_number, event in iter_events(event_paths(log_dir)):
        event_count += 1
        if event.get("event") == "final_raw":
            text = str(event.get("text") or "").strip()
            if text:
                final_texts[text] += 1
        if event.get("event") not in {"parsed", "text_probe"}:
            continue
        payload = event.get("payload") or {}
        text = str(payload.get("text") or "").strip()
        ref = str(payload.get("ref") or "").strip()
        book = str(payload.get("book") or "").strip()
        if ref:
            refs[ref] += 1
            if "-" in ref:
                range_refs[ref] += 1
        elif text:
            unmatched_texts[text] += 1
        if book:
            books[book] += 1
        for attempt in payload.get("attempts") or []:
            attempt_text = str(attempt.get("text") or "").strip()
            if attempt_text and not attempt.get("matched"):
                attempts[attempt_text] += 1

    return {
        "log_dir": str(log_dir),
        "logs": len(event_paths(log_dir)),
        "events": event_count,
        "top_final_texts": final_texts.most_common(30),
        "top_unmatched_texts": unmatched_texts.most_common(30),
        "top_unmatched_attempts": attempts.most_common(30),
        "top_refs": refs.most_common(30),
        "top_books": books.most_common(30),
        "range_refs": range_refs.most_common(30),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize vosk_grammar_probe JSONL logs.")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args()

    report = summarize(args.log_dir)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(f"logs={report['logs']} events={report['events']}")
    for title, key in (
        ("Top final Vosk texts", "top_final_texts"),
        ("Top unmatched parsed texts", "top_unmatched_texts"),
        ("Top unmatched buffer attempts", "top_unmatched_attempts"),
        ("Top refs", "top_refs"),
        ("Top books", "top_books"),
        ("Range refs", "range_refs"),
    ):
        print(f"\n{title}:")
        for value, count in report[key]:
            print(f"  {count:>3}  {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
