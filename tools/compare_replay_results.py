#!/usr/bin/env python3
"""Compare two saved LiVerse replay results without rerunning audio."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def log_directories(root: Path) -> list[Path]:
    if (root / "trigger_cases.jsonl").exists() or (root / "session.json").exists():
        return [root]
    if root.name == "logs":
        return sorted(path for path in root.iterdir() if path.is_dir())
    logs = root / "logs"
    if logs.is_dir():
        return sorted(path for path in logs.iterdir() if path.is_dir())
    return sorted(path for path in root.glob("*/logs/*") if path.is_dir())


def source_audio(log_dir: Path) -> str:
    session = load_json(log_dir / "session.json")
    value = str(session.get("source_audio") or session.get("audio") or "")
    # Keep the comparison portable when old and new logs were copied to
    # different checkout directories; the downloaded filename contains the
    # YouTube id and is the stable identity needed here.
    return value.replace("\\", "/").rsplit("/", 1)[-1]


def number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def refs(payload: dict[str, Any]) -> tuple[str, ...]:
    values = payload.get("reference_list") or []
    result = []
    for item in values:
        if isinstance(item, dict) and item.get("ref"):
            result.append(str(item["ref"]))
    return tuple(result)


def case_key(case: dict[str, Any], source: str, occurrence: int = 0) -> tuple[Any, ...]:
    return (
        source,
        round(number(case.get("window_start_seconds")), 2),
        round(number(case.get("window_end_seconds")), 2),
        round(number(case.get("timecode_seconds")), 2),
        occurrence,
    )


def case_snapshot(case: dict[str, Any]) -> dict[str, Any]:
    payload = case.get("payload") if isinstance(case.get("payload"), dict) else {}
    output = case.get("output") if isinstance(case.get("output"), dict) else {}
    replay_output = output.get("replay") if isinstance(output.get("replay"), dict) else {}
    return {
        "ref": str(case.get("ref") or payload.get("ref") or ""),
        "source": str(payload.get("source") or ""),
        "reference_list": refs(payload),
        "risk_level": str(payload.get("risk_level") or payload.get("risk", {}).get("level") or ""),
        "has_slide": bool(payload.get("has_slide")),
        "output_sent": bool(replay_output.get("sent")),
        "output_action": str(replay_output.get("action") or ""),
        "review_category": str(case.get("review_category") or ""),
    }


def collect_cases(root: Path) -> dict[tuple[Any, ...], dict[str, Any]]:
    result: dict[tuple[Any, ...], dict[str, Any]] = {}
    occurrences: Counter[tuple[Any, ...]] = Counter()
    for log_dir in log_directories(root):
        source = source_audio(log_dir)
        for case_path in sorted(log_dir.glob("trigger_cases.jsonl")):
            for case in load_jsonl(case_path):
                base = case_key(case, source)
                occurrence = occurrences[base]
                occurrences[base] += 1
                key = case_key(case, source, occurrence)
                result[key] = {
                    "key": key,
                    "snapshot": case_snapshot(case),
                    "case_id": case.get("case_id"),
                    "log": str(log_dir),
                }
    return result


def collect_incident_counts(root: Path) -> Counter[str]:
    counts: Counter[str] = Counter()
    for log_dir in log_directories(root):
        path = log_dir / "smart_slide_incidents.jsonl"
        if path.exists():
            counts[source_audio(log_dir)] += len(load_jsonl(path))
    return counts


def compare(baseline: Path, candidate: Path) -> dict[str, Any]:
    old = collect_cases(baseline)
    new = collect_cases(candidate)
    old_keys, new_keys = set(old), set(new)
    added = sorted(new_keys - old_keys, key=str)
    removed = sorted(old_keys - new_keys, key=str)
    changed: list[dict[str, Any]] = []
    field_counts: Counter[str] = Counter()
    for key in sorted(old_keys & new_keys, key=str):
        before = old[key]["snapshot"]
        after = new[key]["snapshot"]
        fields = {
            field: {"before": before[field], "after": after[field]}
            for field in before
            if before[field] != after[field]
        }
        if fields:
            # ``fields`` maps names to before/after dictionaries.  Counter
            # must count the field names, not attempt to add dictionaries.
            field_counts.update(fields.keys())
            changed.append({"key": key, "fields": fields, "before_log": old[key]["log"], "after_log": new[key]["log"]})
    return {
        "baseline": str(baseline),
        "candidate": str(candidate),
        "metrics": {
            "baseline_cases": len(old),
            "candidate_cases": len(new),
            "unchanged_cases": len(old_keys & new_keys) - len(changed),
            "changed_cases": len(changed),
            "added_cases": len(added),
            "removed_cases": len(removed),
            "changed_fields": dict(field_counts),
            "baseline_incidents": sum(collect_incident_counts(baseline).values()),
            "candidate_incidents": sum(collect_incident_counts(candidate).values()),
        },
        "added": [str(key) for key in added],
        "removed": [str(key) for key in removed],
        "changed": changed,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path, help="Старая папка batch или logs")
    parser.add_argument("candidate", type=Path, help="Новая папка batch или logs")
    parser.add_argument("--json", action="store_true", help="Вывести полный JSON-отчёт")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = compare(args.baseline, args.candidate)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    metrics = report["metrics"]
    print(f"Базовых случаев: {metrics['baseline_cases']}")
    print(f"Новых случаев: {metrics['candidate_cases']}")
    print(f"Без изменений: {metrics['unchanged_cases']}")
    print(f"Изменились: {metrics['changed_cases']}")
    print(f"Добавились: {metrics['added_cases']}; исчезли: {metrics['removed_cases']}")
    print(f"Инциденты УПС: {metrics['baseline_incidents']} -> {metrics['candidate_incidents']}")
    for item in report["changed"]:
        print("\nИзменение:", item["key"])
        for field, values in item["fields"].items():
            print(f"  {field}: {values['before']!r} -> {values['after']!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
