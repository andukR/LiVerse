#!/usr/bin/env python3
"""Run a small or core replay regression set and compare it with its baseline."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from compare_replay_results import compare, load_jsonl


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BATCH_ROOT = PROJECT_ROOT / ".cache/liverse/rodnik_replay_batches"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / ".cache/liverse/replay_regressions"


def batch_dirs(root: Path) -> list[Path]:
    return sorted(
        (path for path in root.iterdir() if path.is_dir() and (path / "plans").is_dir()),
        key=lambda path: path.name,
    )


def batch_kind(batch: Path) -> set[str]:
    kinds: set[str] = set()
    if any((batch / "logs").glob("*/smart_slide_incidents.jsonl")):
        kinds.add("ups")
    for path in (batch / "logs").glob("*/trigger_cases.jsonl"):
        for case in load_jsonl(path):
            payload = case.get("payload") if isinstance(case.get("payload"), dict) else {}
            source = str(payload.get("source") or "")
            if source == "text_citation":
                kinds.add("text")
            elif source:
                kinds.add("address")
    return kinds


def previously_run_video_ids(output_root: Path) -> set[str]:
    """Return video IDs already covered by earlier regression manifests."""
    used: set[str] = set()
    if not output_root.is_dir():
        return used
    for manifest_path in output_root.glob("*/manifest.json"):
        try:
            rows = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and row.get("video_id"):
                used.add(str(row["video_id"]))
    return used


def choose_plans(
    root: Path,
    scope: str,
    limit: int,
    *,
    excluded_video_ids: set[str] | None = None,
) -> list[tuple[Path, Path, str]]:
    batches = batch_dirs(root)
    if scope == "all":
        selected_batches = batches
    elif scope == "core":
        selected_batches = [batch for batch in batches if batch_kind(batch) & {"ups", "address", "text"}]
    else:
        selected_batches = []
        wanted = ("ups", "address", "text")
        for kind in wanted:
            for batch in reversed(batches):
                if kind in batch_kind(batch) and batch not in selected_batches:
                    selected_batches.append(batch)
                    break
        selected_batches.reverse()

    selected: list[tuple[Path, Path, str]] = []
    excluded = excluded_video_ids or set()
    batches_to_scan = list(selected_batches)
    if scope == "pilot" and len(selected_batches) < len(batches):
        # The latest representative batches may contain only videos already
        # used by earlier pilots. Continue through older prepared batches
        # instead of silently returning a partial pilot set.
        batches_to_scan.extend(
            batch for batch in reversed(batches) if batch not in selected_batches
        )
    selected_video_ids: set[str] = set()
    for batch in batches_to_scan:
        plans = sorted((path for path in (batch / "plans").glob("*.json")), key=lambda p: p.name)
        for plan in plans:
            video_id = str(json.loads(plan.read_text(encoding="utf-8")).get("video_id") or plan.stem)
            if scope == "pilot" and (video_id in excluded or video_id in selected_video_ids):
                continue
            selected.append((batch, plan, video_id))
            selected_video_ids.add(video_id)
            if scope == "pilot" and len(selected) >= limit:
                return selected
    return selected


def run_one(batch: Path, plan: Path, video_id: str, output_root: Path, execute: bool) -> dict[str, Any]:
    output_logs = output_root / video_id / "logs"
    output_logs.mkdir(parents=True, exist_ok=True)
    audio_dir = batch / "audio"
    command = [
        sys.executable,
        str(PROJECT_ROOT / "tools/replay_audio_files.py"),
        "--replay-window-plan",
        str(plan),
        "--window-audio-dir",
        str(audio_dir),
        "--log-dir",
        str(output_logs),
        "--asr-engine",
        "sherpa-0.54",
        "--citation-detection-mode",
        "hybrid_confirm",
        "--long-range-slide-mode",
        "one_verse",
        "--include-processed",
        "--run",
    ]
    item: dict[str, Any] = {
        "baseline": str(batch),
        "plan": str(plan),
        "video_id": video_id,
        "candidate": str(output_logs),
        "command": command,
    }
    if execute:
        subprocess.run(command, cwd=PROJECT_ROOT, check=True)
        item["comparison"] = compare(batch, output_logs)
    return item


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=("pilot", "core", "all"), default="pilot")
    parser.add_argument(
        "--limit", type=int, default=3,
        help="Число записей пилотного набора; уже использованные видео пропускаются",
    )
    parser.add_argument("--batch-root", type=Path, default=DEFAULT_BATCH_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--run", action="store_true", help="Действительно выполнить replay")
    args = parser.parse_args()

    excluded_video_ids = previously_run_video_ids(args.output_root) if args.scope == "pilot" else set()
    selected = choose_plans(
        args.batch_root,
        args.scope,
        max(1, args.limit),
        excluded_video_ids=excluded_video_ids,
    )
    if not selected:
        if excluded_video_ids:
            raise SystemExit("Не найдено новых планов replay: доступные pilot-видео уже запускались.")
        raise SystemExit("Не найдено подходящих планов replay.")
    run_root = args.output_root / datetime.now().strftime("%Y%m%d_%H%M%S")
    manifest: list[dict[str, Any]] = []
    print(f"Выбрано планов: {len(selected)}; область: {args.scope}")
    for batch, plan, video_id in selected:
        item = run_one(batch, plan, video_id, run_root, args.run)
        manifest.append(item)
        print(f"  {video_id}: baseline={batch} plan={plan}")
        if args.run:
            metrics = item["comparison"]["metrics"]
            print(
                "    сравнение: "
                f"изменились={metrics['changed_cases']} "
                f"добавились={metrics['added_cases']} "
                f"исчезли={metrics['removed_cases']}"
            )
        else:
            print("    (только план; для запуска добавьте --run)")
    run_root.mkdir(parents=True, exist_ok=True)
    manifest_path = run_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Манифест: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
