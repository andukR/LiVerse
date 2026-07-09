#!/usr/bin/env python3
"""Summarize vosk_grammar_probe JSONL logs."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from tools.review_trigger_cases import CATEGORY_LABELS, case_signature, is_unreviewed, load_jsonl


DEFAULT_LOG_DIR = Path(".cache/liverse/vosk_probe")
ERROR_CATEGORIES = {
    "vosk_distortion",
    "false_paronym",
    "false_homonym",
    "false_plain_speech",
    "false_noise",
    "unclear",
    "wrong_reference",
}


def event_paths(log_dir: Path) -> list[Path]:
    if log_dir.is_file():
        return [log_dir]
    if (log_dir / "events.jsonl").is_file():
        return [log_dir / "events.jsonl"]
    paths = sorted(log_dir.glob("*/events.jsonl"))
    if not paths and log_dir == DEFAULT_LOG_DIR:
        paths = sorted(Path(".cache/live_verse_vosk/vosk_probe").glob("*/events.jsonl"))
    return paths


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


def trigger_case_paths(log_dir: Path) -> list[Path]:
    if log_dir.is_file():
        return [log_dir] if log_dir.name == "trigger_cases.jsonl" else []
    if (log_dir / "trigger_cases.jsonl").is_file():
        return [log_dir / "trigger_cases.jsonl"]
    return sorted(log_dir.glob("*/trigger_cases.jsonl"), key=lambda path: path.parent.name)


def reviewed_trigger_cases(log_dir: Path) -> list[tuple[Path, dict]]:
    cases_by_signature: dict[tuple[str, str, str, str, str], tuple[Path, dict]] = {}
    for cases_path in trigger_case_paths(log_dir):
        for case in load_jsonl(cases_path):
            if is_unreviewed(case):
                continue
            cases_by_signature[case_signature(case, cases_path)] = (cases_path, case)
    return list(cases_by_signature.values())


def is_error_category(category: str) -> bool:
    return category in ERROR_CATEGORIES


def risk_score(case: dict) -> float | None:
    payload = case.get("payload") if isinstance(case.get("payload"), dict) else {}
    value = payload.get("risk_score")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def risk_level(case: dict) -> str:
    payload = case.get("payload") if isinstance(case.get("payload"), dict) else {}
    return str(payload.get("risk_level") or "")


def risk_reasons(case: dict) -> list[str]:
    payload = case.get("payload") if isinstance(case.get("payload"), dict) else {}
    reasons = payload.get("risk_reasons") or []
    if not isinstance(reasons, list):
        return []
    return [str(reason) for reason in reasons]


def threshold_report(cases: list[dict], threshold: float) -> dict:
    true_auto = true_confirm = error_caught = error_missed = 0
    for case in cases:
        score = risk_score(case)
        if score is None:
            continue
        category = str(case.get("review_category") or "")
        needs_confirmation = score >= threshold
        if is_error_category(category):
            if needs_confirmation:
                error_caught += 1
            else:
                error_missed += 1
        elif category == "true_reference":
            if needs_confirmation:
                true_confirm += 1
            else:
                true_auto += 1
    return {
        "threshold": threshold,
        "error_caught": error_caught,
        "error_missed": error_missed,
        "true_confirm": true_confirm,
        "true_auto": true_auto,
    }


def summarize_risk_reviews(log_dir: Path) -> dict:
    entries = reviewed_trigger_cases(log_dir)
    cases = [case for _path, case in entries]
    scored_cases = [case for case in cases if risk_score(case) is not None]
    categories = Counter(str(case.get("review_category") or "") for case in cases)
    levels = Counter(risk_level(case) or "unknown" for case in scored_cases)
    reasons = Counter(reason for case in scored_cases for reason in risk_reasons(case))

    return {
        "reviewed_total": len(cases),
        "reviewed_with_risk_score": len(scored_cases),
        "categories": categories.most_common(),
        "risk_levels": levels.most_common(),
        "risk_reasons": reasons.most_common(20),
        "thresholds": [
            threshold_report(scored_cases, 0.2),
            threshold_report(scored_cases, 0.3),
            threshold_report(scored_cases, 0.6),
        ],
    }


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
        "risk_reviews": summarize_risk_reviews(log_dir),
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
    risk_reviews = report["risk_reviews"]
    print("\nRisk score по размеченным случаям:")
    print(
        f"  Размечено всего: {risk_reviews['reviewed_total']}, "
        f"с risk_score: {risk_reviews['reviewed_with_risk_score']}"
    )
    print("  Категории:")
    for category, count in risk_reviews["categories"]:
        label = CATEGORY_LABELS.get(category, category or "без категории")
        print(f"    {count:>3}  {category} ({label})")
    print("  Risk levels:")
    for level, count in risk_reviews["risk_levels"]:
        print(f"    {count:>3}  {level}")
    print("  Пороги полуавтоматического режима:")
    for item in risk_reviews["thresholds"]:
        print(
            f"    score >= {item['threshold']}: "
            f"ошибок оператору {item['error_caught']}, "
            f"ошибок пропущено {item['error_missed']}, "
            f"верных оператору {item['true_confirm']}, "
            f"верных автоматически {item['true_auto']}"
        )
    print("  Частые причины риска:")
    for reason, count in risk_reviews["risk_reasons"]:
        print(f"    {count:>3}  {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
