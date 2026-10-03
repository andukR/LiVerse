#!/usr/bin/env python3
"""Summarize vosk_grammar_probe JSONL logs."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path

from bible_parser_core.live_pipeline import score_reference_risk
from tools.review_trigger_cases import (
    CATEGORY_LABELS,
    SMART_SLIDE_CATEGORIES,
    case_signature,
    collect_smart_slide_entries,
    is_unreviewed,
    load_jsonl,
    session_metadata,
)


DEFAULT_LOG_DIR = Path(".cache/liverse/vosk_probe")
ERROR_CATEGORIES = {
    "vosk_distortion",
    "false_paronym",
    "false_homonym",
    "false_plain_speech",
    "false_noise",
    "unclear",
    "wrong_reference",
    "parser_error",
    "speaker_error",
}
TRAINING_EXCLUDED_CASES = {
    # Старые случаи, размеченные до расширения Vosk-грамматики для Колоссянам.
    # Они учат ML-модель считать ошибкой то, что теперь должно распознаваться лучше.
    ("20260710_154339_165478", "trigger_0004"): "old_colossians_grammar_confused_as_ephesians",
    ("20260710_154339_165478", "trigger_0006"): "old_colossians_grammar_confused_as_corinthians",
    ("20260710_165055_446241", "trigger_0001"): "old_colossians_grammar_confused_as_corinthians",
    ("20260710_165055_446241", "trigger_0002"): "old_colossians_grammar_confused_as_ephesians",
    ("20260710_172600_060527", "trigger_0005"): "old_colossians_grammar_confused_as_ephesians",
    ("20260710_172600_060527", "trigger_0006"): "old_colossians_grammar_confused_as_ephesians",
    ("20260710_193656_496511", "trigger_0012"): "old_colossians_grammar_confused_as_ephesians",
    ("20260825_133014_033320", "trigger_0002"): "fixed_active_range_stale_book_context",
    ("20260831_124818_969652", "trigger_0002"): "fixed_split_reference_stale_book_context",
    ("20260831_124818_969652", "trigger_0006"): "fixed_james_i_okolo_asr_alias",
    ("20260902_154834_285558", "trigger_0008"): "fixed_nehemiah_stale_ezra_context",
}
TRAINING_REASON_COLUMNS = (
    "contains_unk",
    "low_word_confidence",
    "low_average_confidence",
    "fast_speech",
    "very_fast_speech",
    "assembled_from_buffer",
    "confusable_book_form",
    "compact_reference_without_markers",
    "resolved_by_fuzzy_match",
    "missing_twenty_range_repair",
    "repeated_confusable_range_repair",
    "confusable_book_alternative",
    "confusable_number_alternative",
    "blocked_weak_context",
)
MODEL_FEATURE_COLUMNS = (
    "source_parser",
    "source_resolver",
    "source_parser_suffix",
    "source_parser_missing_twenty_range",
    "source_parser_repeated_confusable_range",
    "run_source_live",
    "run_source_replay",
    "has_slide",
    "is_range",
    "has_chapter_word",
    "has_verse_word",
    "has_range_from_word",
    "has_range_to_word",
    "has_epistle_word",
    "has_gospel_word",
    "has_prophet_word",
    "reason_contains_unk",
    "reason_low_word_confidence",
    "reason_low_average_confidence",
    "reason_fast_speech",
    "reason_very_fast_speech",
    "reason_assembled_from_buffer",
    "reason_confusable_book_form",
    "reason_compact_reference_without_markers",
    "reason_resolved_by_fuzzy_match",
    "reason_missing_twenty_range_repair",
    "reason_repeated_confusable_range_repair",
    "reason_confusable_book_alternative",
    "reason_confusable_number_alternative",
    "reason_blocked_weak_context",
)
MODEL_NUMERIC_COLUMNS = (
    "risk_score",
    "asr_word_count",
    "asr_min_confidence",
    "asr_avg_confidence",
    "asr_duration_seconds",
    "asr_words_per_second",
    "text_words",
    "number_count",
    "unknown_count",
    "vosk_buffer_parts",
    "candidate_attempts",
    "verse_count",
)
SMART_SLIDE_TRANSITION_ACTIONS = {
    "advance",
    "assisted_advance",
    "synchronize_forward",
    "assisted_synchronize_forward",
}
SMART_SLIDE_TRAINING_FIELDS = (
    "run", "run_source", "source_audio", "event_id", "passage", "slide_mode",
    "timecode_seconds", "operator_stop_seconds", "sequence_id", "sequence_position", "sequence_total",
    "review_category", "reviewed_at", "review_note", "action", "reason",
    "evidence_source", "target_confirm", "training_eligible", "score", "margin",
    "matched_words", "current_index", "candidate_index", "target_index",
    "target_distance", "current_verse", "candidate_verse", "target_verse",
    "window", "window_words",
)


def smart_slide_event_paths(log_dir: Path) -> list[Path]:
    """Return event logs which have separately reviewed UPS shadow decisions."""
    if log_dir.is_file():
        candidates = [log_dir] if log_dir.name == "events.jsonl" else []
    elif (log_dir / "events.jsonl").is_file():
        candidates = [log_dir / "events.jsonl"]
    else:
        candidates = sorted(log_dir.rglob("events.jsonl"), key=lambda path: str(path))
    return [path.resolve() for path in candidates if path.with_name("smart_slide_reviews.jsonl").is_file()]


def smart_slide_int(value: object) -> int | str:
    if value is None or value == "":
        return ""
    return int_value(value)


def smart_slide_element_verse(event: dict, key: str) -> int | str:
    element = event.get(key)
    if not isinstance(element, dict):
        return ""
    return smart_slide_int(element.get("verse"))


def smart_slide_training_row(entry) -> dict:
    event = entry.event
    review = entry.review
    session = session_metadata(entry.events_path)
    run_mode = str(session.get("mode") or "").strip()
    source_audio = str(session.get("source_audio") or "").strip()
    run_source = "replay" if run_mode == "audio_replay" or source_audio else "live"
    category = str(review.get("review_category") or "").strip()
    action = str(event.get("action") or "").strip()
    stop_seconds = float_value(review.get("operator_stop_seconds"), default=-1.0)
    # This first UPS model is a safety gate for a transition that the rules
    # already proposed. Holds and missed transitions are retained in the CSV,
    # but need a later, distinct "advance or hold" model.
    training_eligible = bool(
        action in SMART_SLIDE_TRANSITION_ACTIONS
        and category in {"correct_transition", "wrong_transition"}
        # A stop before this event cannot describe its safety. It is a
        # browser/replay diagnostic, not a human label for a future decision.
        and (stop_seconds < 0.0 or stop_seconds >= float_value(event.get("replay_seconds")))
    )
    target_confirm = ""
    if training_eligible:
        target_confirm = bool_int(category == "wrong_transition")
    current_index = smart_slide_int(event.get("current_index"))
    target_index = smart_slide_int(event.get("target_index"))
    target_distance = ""
    if isinstance(current_index, int) and isinstance(target_index, int):
        target_distance = target_index - current_index
    window = str(event.get("window") or "")
    return {
        "run": entry.events_path.parent.name,
        "run_source": run_source,
        "source_audio": source_audio,
        "event_id": entry.event_id,
        "passage": str(event.get("passage") or review.get("passage") or ""),
        "slide_mode": str(event.get("slide_mode") or ""),
        "timecode_seconds": float_value(event.get("replay_seconds")),
        "operator_stop_seconds": (
            "" if review.get("operator_stop_seconds") is None
            else float_value(review.get("operator_stop_seconds"))
        ),
        "sequence_id": entry.sequence_id,
        "sequence_position": entry.sequence_position,
        "sequence_total": entry.sequence_total,
        "review_category": category,
        "reviewed_at": str(review.get("reviewed_at") or ""),
        "review_note": str(review.get("note") or ""),
        "action": action,
        "reason": str(event.get("reason") or ""),
        "evidence_source": str(event.get("evidence_source") or ""),
        "target_confirm": target_confirm,
        "training_eligible": bool_int(training_eligible),
        "score": float_value(event.get("score")),
        "margin": float_value(event.get("margin")),
        "matched_words": smart_slide_int(event.get("matched_words")),
        "current_index": current_index,
        "candidate_index": smart_slide_int(event.get("candidate_index")),
        "target_index": target_index,
        "target_distance": target_distance,
        "current_verse": smart_slide_element_verse(event, "current_element"),
        "candidate_verse": smart_slide_element_verse(event, "candidate_element"),
        "target_verse": smart_slide_element_verse(event, "target_element"),
        "window": window,
        "window_words": len(re.findall(r"\w+", window, flags=re.UNICODE)),
    }


def export_smart_slide_training_data(log_dir: Path, output_path: Path, *, asr_engine: str = "") -> dict:
    entries = collect_smart_slide_entries(smart_slide_event_paths(log_dir))
    rows: list[dict] = []
    for entry in entries:
        if not str(entry.review.get("review_category") or "").strip():
            continue
        if asr_engine and str(session_metadata(entry.events_path).get("asr_engine") or "") != asr_engine:
            continue
        rows.append(smart_slide_training_row(entry))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=SMART_SLIDE_TRAINING_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    eligible = [row for row in rows if row["training_eligible"] == 1]
    categories = Counter(str(row["review_category"]) for row in rows)
    return {
        "output": str(output_path),
        "rows": len(rows),
        "eligible_rows": len(eligible),
        "target_confirm": sum(int(row["target_confirm"]) for row in eligible),
        "target_auto": sum(1 - int(row["target_confirm"]) for row in eligible),
        "categories": categories.most_common(),
        "columns": len(SMART_SLIDE_TRAINING_FIELDS),
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
    # Replay batches are stored as <batch>/logs/<run>/trigger_cases.jsonl,
    # while live sessions use <run>/trigger_cases.jsonl.  Accept both shapes
    # so a retrained model is not accidentally built from only one batch.
    return sorted(log_dir.rglob("trigger_cases.jsonl"), key=lambda path: str(path))


def reviewed_trigger_cases(log_dir: Path, *, asr_engine: str = "") -> list[tuple[Path, dict]]:
    cases_by_signature: dict[tuple[str, str, str, str, str], tuple[Path, dict]] = {}
    for cases_path in trigger_case_paths(log_dir):
        if asr_engine and str(session_metadata(cases_path).get("asr_engine") or "") != asr_engine:
            continue
        for case in load_jsonl(cases_path):
            if is_unreviewed(case):
                continue
            cases_by_signature[case_signature(case, cases_path)] = (cases_path, case)
    return list(cases_by_signature.values())


def is_error_category(category: str) -> bool:
    return category in ERROR_CATEGORIES


def risk_score(case: dict) -> float | None:
    payload = payload_with_current_risk(case)
    value = payload.get("risk_score")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def risk_level(case: dict) -> str:
    payload = payload_with_current_risk(case)
    return str(payload.get("risk_level") or "")


def risk_reasons(case: dict) -> list[str]:
    payload = payload_with_current_risk(case)
    reasons = payload.get("risk_reasons") or []
    if not isinstance(reasons, list):
        return []
    return [str(reason) for reason in reasons]


def payload_with_current_risk(case: dict) -> dict:
    payload = dict(case.get("payload") if isinstance(case.get("payload"), dict) else {})
    if payload.get("risk_score") is not None:
        return payload
    payload["vosk_text"] = str(case.get("vosk_text") or "")
    payload["vosk_buffer"] = list(case.get("vosk_buffer") or [])
    asr = case.get("asr") if isinstance(case.get("asr"), dict) else None
    risk = score_reference_risk(payload, asr_result=asr)
    payload["risk_score"] = risk["score"]
    payload["risk_level"] = risk["level"]
    payload["risk_reasons"] = risk["reasons"]
    payload["risk"] = risk
    return payload


def float_value(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def int_value(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def bool_int(value: bool) -> int:
    return 1 if value else 0


def asr_word_items(case: dict) -> list[dict]:
    asr = case.get("asr") if isinstance(case.get("asr"), dict) else {}
    result = asr.get("result") if isinstance(asr.get("result"), list) else []
    return [item for item in result if isinstance(item, dict)]


def asr_metrics(case: dict) -> dict[str, float]:
    words = asr_word_items(case)
    confidences: list[float] = []
    starts: list[float] = []
    ends: list[float] = []
    for item in words:
        if "conf" in item:
            confidences.append(float_value(item.get("conf")))
        if "start" in item and "end" in item:
            starts.append(float_value(item.get("start")))
            ends.append(float_value(item.get("end")))

    metrics: dict[str, float] = {
        "asr_word_count": float(len(words)),
        "asr_min_confidence": 0.0,
        "asr_avg_confidence": 0.0,
        "asr_duration_seconds": 0.0,
        "asr_words_per_second": 0.0,
    }
    if confidences:
        metrics["asr_min_confidence"] = round(min(confidences), 3)
        metrics["asr_avg_confidence"] = round(sum(confidences) / len(confidences), 3)
    if starts and ends and max(ends) > min(starts):
        duration = max(ends) - min(starts)
        metrics["asr_duration_seconds"] = round(duration, 3)
        metrics["asr_words_per_second"] = round(len(words) / duration, 3)
    return metrics


def text_features(text: str) -> dict[str, int]:
    normalized = text.lower().replace("ё", "е")
    words = re.findall(r"\w+", normalized, flags=re.UNICODE)
    numbers = re.findall(r"\b\d+\b", normalized)
    return {
        "text_chars": len(text),
        "text_words": len(words),
        "number_count": len(numbers),
        "unknown_count": len(re.findall(r"\bunk\b", normalized)),
        "has_chapter_word": bool_int(bool(re.search(r"\bглав", normalized))),
        "has_verse_word": bool_int(bool(re.search(r"\bстих", normalized))),
        "has_range_from_word": bool_int(bool(re.search(r"\bс\b", normalized))),
        "has_range_to_word": bool_int(bool(re.search(r"\bпо\b", normalized))),
        "has_epistle_word": bool_int(bool(re.search(r"\bпослани", normalized))),
        "has_gospel_word": bool_int(bool(re.search(r"\bевангел", normalized))),
        "has_prophet_word": bool_int(bool(re.search(r"\bпророк", normalized))),
    }


def training_row(cases_path: Path, case: dict) -> dict:
    payload = case.get("payload") if isinstance(case.get("payload"), dict) else {}
    session = session_metadata(cases_path)
    run_mode = str(session.get("mode") or "").strip()
    source_audio = str(session.get("source_audio") or "").strip()
    run_source = "replay" if run_mode == "audio_replay" or source_audio else "live"
    category = str(case.get("review_category") or "")
    text = str(payload.get("text") or case.get("vosk_text") or "")
    vosk_text = str(case.get("vosk_text") or "")
    source = str(payload.get("source") or "")
    reasons = set(risk_reasons(case))
    start_verse = int_value(payload.get("start_verse"))
    end_verse = int_value(payload.get("end_verse"), start_verse)
    buffer_parts = case.get("vosk_buffer") if isinstance(case.get("vosk_buffer"), list) else []
    score = risk_score(case)
    row = {
        "run": cases_path.parent.name,
        "run_source": run_source,
        "run_source_live": bool_int(run_source == "live"),
        "run_source_replay": bool_int(run_source == "replay"),
        "source_audio": source_audio,
        "case_id": str(case.get("case_id") or ""),
        "timecode_seconds": float_value(case.get("timecode_seconds")),
        "category": category,
        "target_confirm": bool_int(is_error_category(category)),
        "target_true_reference": bool_int(category == "true_reference"),
        "ref": str(case.get("ref") or payload.get("ref") or ""),
        "book": str(payload.get("book") or ""),
        "chapter": int_value(payload.get("chapter")),
        "start_verse": start_verse,
        "end_verse": end_verse,
        "verse_count": max(0, end_verse - start_verse + 1),
        "is_range": bool_int(end_verse > start_verse),
        "source": source,
        "source_parser": bool_int(source == "parser"),
        "source_resolver": bool_int(source == "resolver"),
        "source_parser_suffix": bool_int(source == "parser_suffix"),
        "source_parser_missing_twenty_range": bool_int(source == "parser_missing_twenty_range"),
        "source_parser_repeated_confusable_range": bool_int(source == "parser_repeated_confusable_range"),
        "has_slide": bool_int(bool(payload.get("has_slide"))),
        "risk_score": "" if score is None else score,
        "risk_level": risk_level(case),
        "risk_reasons": "|".join(sorted(reasons)),
        "vosk_text": vosk_text,
        "parser_text": text,
        "vosk_buffer_parts": len(buffer_parts),
        "candidate_attempts": len(payload.get("attempts") or []),
        "note": str(case.get("note") or ""),
    }
    row.update(asr_metrics(case))
    row.update(text_features(f"{text} {vosk_text}"))
    for reason in TRAINING_REASON_COLUMNS:
        row[f"reason_{reason}"] = bool_int(reason in reasons)
    return row


def training_exclusion_reason(cases_path: Path, case: dict) -> str | None:
    if str(case.get("review_category") or "") == "excluded_cascade":
        return "cascade_after_primary_error"
    return TRAINING_EXCLUDED_CASES.get((cases_path.parent.name, str(case.get("case_id") or "")))


def export_training_data(log_dir: Path, output_path: Path, *, asr_engine: str = "") -> dict:
    rows = []
    excluded: list[dict[str, str]] = []
    for cases_path, case in reviewed_trigger_cases(log_dir, asr_engine=asr_engine):
        if not str(case.get("review_category") or ""):
            continue
        exclusion_reason = training_exclusion_reason(cases_path, case)
        if exclusion_reason:
            excluded.append(
                {
                    "run": cases_path.parent.name,
                    "case_id": str(case.get("case_id") or ""),
                    "ref": str(case.get("ref") or ""),
                    "reason": exclusion_reason,
                }
            )
            continue
        rows.append(training_row(cases_path, case))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with output_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    categories = Counter(str(row["category"]) for row in rows)
    return {
        "output": str(output_path),
        "rows": len(rows),
        "target_confirm": sum(int(row["target_confirm"]) for row in rows),
        "target_auto": sum(1 - int(row["target_confirm"]) for row in rows),
        "categories": categories.most_common(),
        "columns": len(fieldnames),
        "excluded": len(excluded),
        "excluded_cases": excluded,
    }


def load_training_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def stratified_split(
    rows: list[dict[str, str]],
    *,
    validation_ratio: float = 0.25,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    by_target: dict[str, list[dict[str, str]]] = {"0": [], "1": []}
    for row in rows:
        target = str(row.get("target_confirm") or "")
        if target in by_target:
            by_target[target].append(row)

    train: list[dict[str, str]] = []
    validation: list[dict[str, str]] = []
    for target, target_rows in by_target.items():
        ordered = sorted(
            target_rows,
            key=lambda row: (
                str(row.get("run") or ""),
                str(row.get("case_id") or ""),
                str(row.get("timecode_seconds") or ""),
            ),
        )
        validation_count = max(1, round(len(ordered) * validation_ratio)) if ordered else 0
        validation_indexes = set()
        if validation_count:
            step = len(ordered) / validation_count
            validation_indexes = {
                min(len(ordered) - 1, round(index * step))
                for index in range(validation_count)
            }
        for index, row in enumerate(ordered):
            if index in validation_indexes:
                validation.append(row)
            else:
                train.append(row)
    return train, validation


def numeric_value(row: dict[str, str], column: str) -> float:
    try:
        return float(row.get(column) or 0.0)
    except ValueError:
        return 0.0


def model_features(row: dict[str, str]) -> set[str]:
    features: set[str] = set()
    for column in MODEL_FEATURE_COLUMNS:
        if str(row.get(column) or "") == "1":
            features.add(column)

    for column in MODEL_NUMERIC_COLUMNS:
        value = numeric_value(row, column)
        if column == "risk_score":
            for threshold in (0.1, 0.2, 0.3, 0.6):
                if value >= threshold:
                    features.add(f"{column}>={threshold}")
        elif column in {"asr_min_confidence", "asr_avg_confidence"}:
            for threshold in (0.65, 0.8, 0.9):
                if value and value < threshold:
                    features.add(f"{column}<{threshold}")
        elif column == "asr_words_per_second":
            for threshold in (3.0, 3.6, 4.8):
                if value >= threshold:
                    features.add(f"{column}>={threshold}")
        elif column in {"vosk_buffer_parts", "candidate_attempts", "verse_count", "number_count"}:
            for threshold in (2, 3):
                if value >= threshold:
                    features.add(f"{column}>={threshold}")
        elif column in {"asr_word_count", "text_words"}:
            for threshold in (3, 6, 10):
                if value >= threshold:
                    features.add(f"{column}>={threshold}")
        elif column == "unknown_count" and value:
            features.add("unknown_count>0")

    for column in ("risk_level", "source", "book", "run_source"):
        value = str(row.get(column) or "").strip()
        if value:
            features.add(f"{column}={value}")

    text = f"{row.get('vosk_text') or ''} {row.get('parser_text') or ''}".lower().replace("ё", "е")
    for token in re.findall(r"\w+", text, flags=re.UNICODE):
        if len(token) >= 4:
            features.add(f"token={token}")
    return features


def train_naive_bayes(rows: list[dict[str, str]]) -> dict:
    class_counts = Counter(str(row.get("target_confirm") or "") for row in rows)
    feature_counts: dict[str, Counter[str]] = {"0": Counter(), "1": Counter()}
    vocabulary: set[str] = set()
    for row in rows:
        target = str(row.get("target_confirm") or "")
        if target not in feature_counts:
            continue
        features = model_features(row)
        vocabulary.update(features)
        feature_counts[target].update(features)

    total_rows = sum(class_counts.values())
    classes = ("0", "1")
    model = {
        "model_type": "bernoulli_naive_bayes",
        "target": "target_confirm",
        "classes": list(classes),
        "class_counts": dict(class_counts),
        "feature_columns": list(MODEL_FEATURE_COLUMNS),
        "numeric_columns": list(MODEL_NUMERIC_COLUMNS),
        "features": {},
    }
    for target in classes:
        class_count = class_counts[target]
        prior = (class_count + 1) / (total_rows + len(classes))
        model.setdefault("log_prior", {})[target] = math.log(prior)
        for feature in sorted(vocabulary):
            count = feature_counts[target][feature]
            probability = (count + 1) / (class_count + 2)
            model["features"].setdefault(feature, {})[target] = math.log(probability)
            model["features"][feature][f"not_{target}"] = math.log(1 - probability)
    return model


def predict_probability(model: dict, row: dict[str, str]) -> float:
    features = model_features(row)
    vocabulary = set(model.get("features") or {})
    scores = {
        target: float((model.get("log_prior") or {}).get(target, 0.0))
        for target in ("0", "1")
    }
    for feature in vocabulary:
        values = model["features"][feature]
        present = feature in features
        for target in ("0", "1"):
            key = target if present else f"not_{target}"
            scores[target] += float(values.get(key, 0.0))
    max_score = max(scores.values())
    exp0 = math.exp(scores["0"] - max_score)
    exp1 = math.exp(scores["1"] - max_score)
    return exp1 / (exp0 + exp1)


def smart_slide_model_features(row: dict[str, str]) -> set[str]:
    """Stable, decision-level features for the separate UPS safety model."""
    features: set[str] = set()
    for column in ("action", "reason", "evidence_source", "slide_mode", "run_source"):
        value = str(row.get(column) or "").strip()
        if value:
            features.add(f"{column}={value}")
    for column, thresholds in {
        "score": (60, 80, 90),
        "margin": (12, 30, 60),
        "matched_words": (2, 4, 8),
        "target_distance": (1, 2),
        "sequence_position": (2, 5),
        "sequence_total": (5, 10),
    }.items():
        value = numeric_value(row, column)
        for threshold in thresholds:
            if value >= threshold:
                features.add(f"{column}>={threshold}")
    return features


def train_smart_slide_naive_bayes(rows: list[dict[str, str]]) -> dict:
    class_counts = Counter(str(row.get("target_confirm") or "") for row in rows)
    feature_counts: dict[str, Counter[str]] = {"0": Counter(), "1": Counter()}
    vocabulary: set[str] = set()
    for row in rows:
        target = str(row.get("target_confirm") or "")
        if target not in feature_counts:
            continue
        features = smart_slide_model_features(row)
        vocabulary.update(features)
        feature_counts[target].update(features)
    total_rows = sum(class_counts.values())
    model = {
        "model_type": "bernoulli_naive_bayes",
        "scope": "smart_slide_transition_safety",
        "target": "target_confirm",
        "classes": ["0", "1"],
        "class_counts": dict(class_counts),
        "features": {},
    }
    for target in ("0", "1"):
        class_count = class_counts[target]
        model.setdefault("log_prior", {})[target] = math.log((class_count + 1) / (total_rows + 2))
        for feature in sorted(vocabulary):
            probability = (feature_counts[target][feature] + 1) / (class_count + 2)
            model["features"].setdefault(feature, {})[target] = math.log(probability)
            model["features"][feature][f"not_{target}"] = math.log(1 - probability)
    return model


def predict_smart_slide_confirmation_probability(model: dict, row: dict[str, str]) -> float:
    features = smart_slide_model_features(row)
    scores = {
        target: float((model.get("log_prior") or {}).get(target, 0.0))
        for target in ("0", "1")
    }
    for feature, values in (model.get("features") or {}).items():
        for target in ("0", "1"):
            scores[target] += float(values.get(target if feature in features else f"not_{target}", 0.0))
    max_score = max(scores.values())
    probability_0 = math.exp(scores["0"] - max_score)
    probability_1 = math.exp(scores["1"] - max_score)
    return probability_1 / (probability_0 + probability_1)


def train_smart_slide_model(training_csv: Path, model_path: Path, report_path: Path) -> dict:
    rows = [
        row for row in load_training_rows(training_csv)
        if str(row.get("training_eligible") or "") == "1" and str(row.get("target_confirm") or "") in {"0", "1"}
    ]
    if not rows:
        raise RuntimeError("Нет размеченных переходов для обучения НБА УПС.")
    train_rows, validation_rows = stratified_split(rows)
    validation_model = train_smart_slide_naive_bayes(train_rows)
    validation = []
    for threshold in (0.2, 0.3, 0.5, 0.7):
        counts = {"error_caught": 0, "error_missed": 0, "true_confirm": 0, "true_auto": 0}
        for row in validation_rows:
            predicted_confirm = predict_smart_slide_confirmation_probability(validation_model, row) >= threshold
            target = str(row["target_confirm"])
            if target == "1":
                counts["error_caught" if predicted_confirm else "error_missed"] += 1
            else:
                counts["true_confirm" if predicted_confirm else "true_auto"] += 1
        total = sum(counts.values())
        validation.append({
            "threshold": threshold,
            "total": total,
            "accuracy": round((counts["error_caught"] + counts["true_auto"]) / total, 3) if total else 0.0,
            **counts,
        })
    model = train_smart_slide_naive_bayes(rows)
    model.update({
        "training_rows": len(rows), "train_rows": len(train_rows),
        "validation_rows": len(validation_rows), "validation": validation,
        "recommended_threshold": 0.2,
    })
    model_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.write_text(json.dumps(model, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = {
        "training_csv": str(training_csv), "model": str(model_path), "rows": len(rows),
        "train_rows": len(train_rows), "validation_rows": len(validation_rows),
        "target_counts": dict(Counter(str(row["target_confirm"]) for row in rows)), "validation": validation,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def evaluate_model(model: dict, rows: list[dict[str, str]], threshold: float) -> dict:
    true_auto = true_confirm = error_caught = error_missed = 0
    for row in rows:
        target = str(row.get("target_confirm") or "")
        predicted_confirm = predict_probability(model, row) >= threshold
        if target == "1":
            if predicted_confirm:
                error_caught += 1
            else:
                error_missed += 1
        elif target == "0":
            if predicted_confirm:
                true_confirm += 1
            else:
                true_auto += 1
    total = error_caught + error_missed + true_confirm + true_auto
    correct = error_caught + true_auto
    return {
        "threshold": threshold,
        "total": total,
        "accuracy": round(correct / total, 3) if total else 0.0,
        "error_caught": error_caught,
        "error_missed": error_missed,
        "true_confirm": true_confirm,
        "true_auto": true_auto,
    }


def train_risk_model(training_csv: Path, model_path: Path, report_path: Path) -> dict:
    rows = load_training_rows(training_csv)
    train_rows, validation_rows = stratified_split(rows)
    validation_model = train_naive_bayes(train_rows)
    validation = [
        evaluate_model(validation_model, validation_rows, threshold)
        for threshold in (0.2, 0.3, 0.5, 0.7)
    ]
    risk_score_baseline = [
        evaluate_risk_score_baseline(validation_rows, threshold)
        for threshold in (0.2, 0.3, 0.6)
    ]
    final_model = train_naive_bayes(rows)
    final_model["training_rows"] = len(rows)
    final_model["train_rows"] = len(train_rows)
    final_model["validation_rows"] = len(validation_rows)
    final_model["validation"] = validation
    final_model["risk_score_baseline"] = risk_score_baseline
    final_model["recommended_threshold"] = 0.2
    model_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.write_text(json.dumps(final_model, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = {
        "training_csv": str(training_csv),
        "model": str(model_path),
        "rows": len(rows),
        "train_rows": len(train_rows),
        "validation_rows": len(validation_rows),
        "target_counts": dict(Counter(str(row.get("target_confirm") or "") for row in rows)),
        "validation": validation,
        "risk_score_baseline": risk_score_baseline,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def evaluate_risk_score_baseline(rows: list[dict[str, str]], threshold: float) -> dict:
    true_auto = true_confirm = error_caught = error_missed = 0
    for row in rows:
        target = str(row.get("target_confirm") or "")
        predicted_confirm = numeric_value(row, "risk_score") >= threshold
        if target == "1":
            if predicted_confirm:
                error_caught += 1
            else:
                error_missed += 1
        elif target == "0":
            if predicted_confirm:
                true_confirm += 1
            else:
                true_auto += 1
    total = error_caught + error_missed + true_confirm + true_auto
    correct = error_caught + true_auto
    return {
        "threshold": threshold,
        "total": total,
        "accuracy": round(correct / total, 3) if total else 0.0,
        "error_caught": error_caught,
        "error_missed": error_missed,
        "true_confirm": true_confirm,
        "true_auto": true_auto,
    }


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


def summarize_performance(log_dir: Path) -> dict:
    """Describe sampled post-ASR intervals, never an entire-session CPU mean."""
    paths = [log_dir] if log_dir.is_file() else sorted(log_dir.rglob("performance.jsonl"))
    fields = (
        "system_cpu_percent", "process_cpu_percent", "asr_final_to_decision_ready_ms",
        "pipeline_call_ms", "reference_parse_ms", "other_post_asr_ms", "audio_queue_items",
    )

    def numeric(value: object) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    def summary(values: list[float]) -> dict:
        values = sorted(values)
        if not values:
            return {"count": 0}
        return {
            "count": len(values),
            "median": round(statistics.median(values), 3),
            "p95": round(values[math.ceil(len(values) * 0.95) - 1], 3),
            "max": round(values[-1], 3),
        }

    def correlation(rows: list[dict], left: str, right: str) -> float | None:
        pairs = [(row[left], row[right]) for row in rows
                 if numeric(row.get(left)) and numeric(row.get(right))]
        if len(pairs) < 2:
            return None
        x, y = zip(*pairs)
        mx, my = statistics.mean(x), statistics.mean(y)
        denominator = math.sqrt(sum((v - mx) ** 2 for v in x) * sum((v - my) ** 2 for v in y))
        return round(sum((a - mx) * (b - my) for a, b in pairs) / denominator, 3) if denominator else None

    sessions = []
    for path in paths:
        rows = [row for row in load_jsonl(path) if row.get("event") == "LIVE_PROCESSING_TIMING"]
        weighted = [row for row in rows if numeric(row.get("system_cpu_percent"))
                    and numeric(row.get("asr_final_to_decision_ready_ms"))
                    and row["asr_final_to_decision_ready_ms"] > 0]
        duration_ms = sum(row["asr_final_to_decision_ready_ms"] for row in weighted)
        groups = []
        for lower, upper in ((0, 70), (70, 90), (90, 101)):
            group = [row for row in rows if numeric(row.get("system_cpu_percent"))
                     and lower <= row["system_cpu_percent"] < upper]
            groups.append({
                "cpu_min_inclusive": lower, "cpu_max_exclusive": upper,
                "measurements": len(group),
                "processing_ms": summary([row["asr_final_to_decision_ready_ms"] for row in group
                                          if numeric(row.get("asr_final_to_decision_ready_ms"))]),
            })
        sessions.append({
            "session": path.parent.name, "measurements": len(rows),
            "metrics": {field: summary([row[field] for row in rows if numeric(row.get(field))])
                        for field in fields},
            "correlations_pearson": {
                "system_cpu_vs_processing": correlation(rows, "system_cpu_percent", "asr_final_to_decision_ready_ms"),
                "system_cpu_vs_queue": correlation(rows, "system_cpu_percent", "audio_queue_items"),
                "parser_vs_processing": correlation(rows, "pipeline_call_ms", "asr_final_to_decision_ready_ms"),
            },
            "cpu_groups": groups,
            "sampled_interval_seconds": round(duration_ms / 1000, 3),
            "sampled_system_cpu_weighted_mean": round(
                sum(row["system_cpu_percent"] * row["asr_final_to_decision_ready_ms"]
                    for row in weighted) / duration_ms, 3
            ) if duration_ms else None,
        })
    return {
        "scope": "CPU is sampled during post-ASR processing only; process 100% equals one busy CPU; correlation is not causation",
        "sessions": sessions,
    }


def verified_absent_quick_closes(events: list[dict]) -> set[int]:
    """Recognize only the already-closed response followed by verified plan return.

    Keep raw failures intact. A later, unrelated success must not hide a failure.
    The live restore helper already handles this exact Holyrics response.
    """
    recovered = set()
    api_events = [(i, r) for i, r in enumerate(events)
                  if r.get("event") in ("holyrics_api_request", "holyrics_api_response")]

    def body(row):
        value = row.get("response_body")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return {}
        return value if isinstance(value, dict) else {}

    for pos, (index, close) in enumerate(api_events):
        if (close.get("event") != "holyrics_api_response" or close.get("ok") is not False
                or close.get("endpoint") != "CloseCurrentQuickPresentation"
                or close.get("http_status") != 200
                or close.get("reason") != "holyrics_error:No quick presentation available"
                or body(close) != {"status": "error", "error": "No quick presentation available"}
                or pos == 0 or pos + 4 >= len(api_events)):
            continue
        close_request = api_events[pos - 1][1]
        if (close_request.get("event") != "holyrics_api_request"
                or close_request.get("endpoint") != close["endpoint"]
                or not close.get("request_id") or close_request.get("request_id") != close["request_id"]
                or not close_request.get("base_url")):
            continue
        (_, show), (_, shown), (_, get), (state_index, state) = api_events[pos + 1:pos + 5]
        if len({close["request_id"], show.get("request_id"), get.get("request_id")}) != 3:
            continue
        valid_pairs = True
        for request, response, endpoint in ((show, shown, "ShowText"), (get, state, "GetCurrentPresentation")):
            if (request.get("event") != "holyrics_api_request" or response.get("event") != "holyrics_api_response"
                    or request.get("endpoint") != endpoint or response.get("endpoint") != endpoint
                    or not request.get("request_id") or request["request_id"] != response.get("request_id")
                    or request.get("base_url") != close_request["base_url"]
                    or response.get("ok") is not True or response.get("http_status") != 200
                    or body(response).get("status") != "ok"):
                valid_pairs = False
        target = show.get("request_body")
        current = body(state).get("data")
        if not valid_pairs or not isinstance(target, dict) or not isinstance(current, dict):
            continue
        slide = target.get("initial_index")
        if (not isinstance(slide, int) or isinstance(slide, bool) or slide < 0 or not target.get("id")
                or current.get("type") != "text" or (current.get("text_id") or current.get("id")) != target["id"]
                or type(current.get("slide_number")) is not int or current["slide_number"] != slide + 1):
            continue
        # The first subsequent control/API event must confirm this operation.
        completion = next((r for r in events[state_index + 1:]
                           if r.get("event") in ("STREAMING_SLIDE_CONTROL", "holyrics_api_request", "holyrics_api_response")), {})
        if (completion.get("event") != "STREAMING_SLIDE_CONTROL" or completion.get("ok") is not True
                or completion.get("reason") != "sermon_plan_restore_verified"
                or completion.get("action") != "complete_range" or completion.get("completed") is not True
                or completion.get("restored_sermon_plan") is not True):
            continue
        try:
            times = [datetime.fromisoformat(r["ts"]) for r in (close_request, close, show, shown, get, state, completion)]
            if all(a <= b for a, b in zip(times, times[1:])) and 0 <= (times[-1] - times[0]).total_seconds() <= 5:
                recovered.add(index)
        except (KeyError, TypeError, ValueError):
            continue
    return recovered


def check_live_session(session: Path) -> dict:
    """Conservative technical check, never proof of projector output or a whole service."""
    report = {
        "session": str(session), "status": "insufficient", "version": "неизвестна",
        "failures": [], "missing": [], "notes": [], "metrics": {},
        "policy": {"min_speech_span_seconds": 300, "min_finals": 20,
                   "p95_delay_ms": 3000, "max_delay_ms": 6000},
        "limitations": [
            "Задержка измеряется от последнего аудиоблока фразы, без ожидания паузы говорящего.",
            "Ответы Holyrics не подтверждают изображение на экране. Проверьте адреса, список и УПС лично.",
            "Короткий тест не гарантирует устойчивость всего богослужения; нужен оператор и ручное управление.",
        ],
    }
    failures, missing = report["failures"], report["missing"]

    def read_rows(name):
        path = session / name
        if not path.is_file():
            missing.append(f"Отсутствует {name}.")
            return []
        rows = []
        for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{name}, строка {number}: ожидается запись журнала.")
            rows.append(value)
        return rows

    def nonnegative(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0

    def stats(values):
        values = sorted(values)
        if not values:
            return {"count": 0}
        return {"count": len(values), "median": round(statistics.median(values), 1),
                "p95": round(values[math.ceil(.95 * len(values)) - 1], 1),
                "max": round(values[-1], 1)}

    try:
        metadata = json.loads((session / "session.json").read_text(encoding="utf-8-sig"))
        if not isinstance(metadata, dict):
            raise ValueError("session.json должен содержать объект.")
        report["version"] = str(metadata.get("liverse_version") or "неизвестна")
        report["host"] = metadata.get("host") or {}
        if not isinstance(report["host"], dict):
            report["host"] = {}
            missing.append("Повреждены сведения о компьютере в session.json.")
        events = read_rows("events.jsonl")
        performance = read_rows("performance.jsonl")
        finals = [r for r in events if r.get("event") == "final_raw" and str(r.get("text") or "").strip()]
        timing = [r for r in performance if r.get("event") == "LIVE_PROCESSING_TIMING"]
        paused = {r.get("audio_bytes_seen") for r in events
                  if r.get("event") == "TEMPORARY_VERSE_READING" and r.get("action") == "recognition_paused"}
        processed = [r for r in finals if r.get("audio_bytes_seen") not in paused or r.get("audio_bytes_seen") is None]
        report["finals"] = len(finals)
        report["measurements"] = len(timing)
        if metadata.get("mode") not in (None, "microphone", "live"):
            missing.append("Это не живой сеанс с микрофоном; допуск к богослужению по нему не выдаётся.")
        if report["version"] == "неизвестна":
            missing.append("Не записана версия LV.")
        if metadata.get("diagnostic_test_profile"):
            report["limitations"].insert(0, "Сеанс с ограничением ресурсов ноутбука: имитация не откалибрована под церковный процессор.")
        if not finals:
            missing.append("Нет распознанных непустых фраз.")
        if len(finals) < report["policy"]["min_finals"]:
            missing.append("Нужно не менее 20 непустых фраз.")
        timestamps = [datetime.fromisoformat(r["ts"]) for r in finals]
        if any(a > b for a, b in zip(timestamps, timestamps[1:])):
            raise ValueError("Нарушен порядок времени распознанных фраз.")
        span = (timestamps[-1] - timestamps[0]).total_seconds() if timestamps else 0
        report["speech_span_seconds"] = round(span, 1)
        if span < report["policy"]["min_speech_span_seconds"]:
            missing.append("Между первой и последней непустой фразой должно пройти не менее 5 минут; читайте непрерывно.")
        # A stop can interrupt the final interval. Never silently treat lost
        # processing records (or duplicated records) as successful coverage.
        ids = [r.get("audio_bytes_seen") for r in processed]
        measured_ids = [r.get("audio_bytes_seen") for r in timing]
        if (len(timing) != len(processed) or any(not isinstance(v, int) or isinstance(v, bool) or v <= 0 for v in ids + measured_ids)
                or len(set(ids)) != len(ids) or ids != measured_ids):
            missing.append(f"Неполные или несогласованные замеры: фраз для обработки {len(processed)}, измерений {len(timing)}.")
        if len(timing) < 10:
            missing.append("Нужно не менее 10 измерений обработки фраз.")
        stops = [r for r in events if r.get("event") == "session_stopped"]
        if not stops:
            missing.append("Нет записи штатной остановки. Остановите распознавание перед проверкой.")
        elif stops[-1].get("reason") not in ("operator_stop", "keyboard_interrupt"):
            failures.append("Распознавание завершилось нештатно.")
        if stops and any(r.get("event") == "final_raw" for r in events[events.index(stops[-1]) + 1:]):
            missing.append("После остановки появились новые фразы; выберите завершённый сеанс.")
        for field in ("audio_callback_to_decision_ready_ms", "asr_final_to_decision_ready_ms",
                      "pipeline_call_ms", "reference_parse_ms", "audio_queue_items", "system_cpu_percent"):
            values = [r[field] for r in timing if nonnegative(r.get(field))]
            report["metrics"][field] = stats(values)
            if field in ("audio_callback_to_decision_ready_ms", "audio_queue_items") and len(values) != len(timing):
                missing.append(f"Не все записи содержат корректное поле {field}.")
        lag = [r.get("live_timing", {}).get("audio_callback_to_asr_final_ms") for r in events if r.get("event") == "final_raw"]
        report["metrics"]["audio_callback_to_asr_final_ms"] = stats([v for v in lag if nonnegative(v)])
        if not lag or any(not nonnegative(v) for v in lag):
            missing.append("Не все финальные результаты содержат корректную задержку готовности текста.")
        for field, label in (("audio_callback_to_decision_ready_ms", "Подготовка решения"),
                             ("audio_callback_to_asr_final_ms", "Готовность распознанного текста")):
            measured = report["metrics"][field]
            if measured.get("p95", 0) > report["policy"]["p95_delay_ms"]:
                failures.append(f"{label}: 95% значений укладываются только в {measured['p95']/1000:.1f} с (порог 3 с).")
            if measured.get("max", 0) > report["policy"]["max_delay_ms"]:
                failures.append(f"{label}: максимальная задержка {measured['max']/1000:.1f} с (порог 6 с).")
        block = metadata.get("blocksize")
        rate = metadata.get("samplerate")
        if nonnegative(block) and nonnegative(rate) and block > 0 and rate > 0:
            queue_seconds = [r["audio_queue_items"] * block / rate for r in timing if nonnegative(r.get("audio_queue_items"))]
            report["metrics"]["queue_seconds"] = stats(queue_seconds)
            if queue_seconds and max(queue_seconds) > 6:
                failures.append(f"В очереди накопилось до {max(queue_seconds):.1f} с звука (порог 6 с).")
            third = max(1, len(queue_seconds) // 3)
            if len(queue_seconds) >= 6 and statistics.median(queue_seconds[-third:]) > statistics.median(queue_seconds[:third]) + 2:
                failures.append("Очередь к концу теста выросла более чем на 2 секунды по медианам первой и последней трети.")
            if stops and nonnegative(stops[-1].get("audio_queue_items")):
                tail = stops[-1]["audio_queue_items"] * block / rate
                report["queue_at_stop_seconds"] = round(tail, 1)
                if tail > 2:
                    failures.append(f"При остановке оставалось {tail:.1f} с необработанного звука.")
            elif stops:
                missing.append("Не записан остаток очереди при остановке.")
        else:
            missing.append("Не записаны корректные частота звука и размер блока; очередь нельзя перевести в секунды.")
        api = [r for r in events if r.get("event") == "holyrics_api_response"]
        verified_closes = verified_absent_quick_closes(events)
        raw_bad_api = [r for r in api if r.get("ok") is not True]
        bad_api = [r for i, r in enumerate(events)
                   if r.get("event") == "holyrics_api_response" and r.get("ok") is not True and i not in verified_closes]
        report["api_replies"] = len(api)
        report["api_failed_replies"] = len(raw_bad_api)
        report["api_verified_absent_quick_closes"] = len(verified_closes)
        if verified_closes:
            report["notes"].append(f"Ответов Holyrics «быстрая презентация уже отсутствует»: {len(verified_closes)}. "
                                   "Последующий возврат к нужному слайду плана подтверждён; эти ответы не считаются сбоем показа.")
        if bad_api:
            failures.append(f"Неуспешных ответов Holyrics: {len(bad_api)}. Возможен сбой или неизвестный результат показа.")
        if not api:
            missing.append("Нет ответов Holyrics; вывод на презентацию не проверялся.")
        if not any(r.get("endpoint") in ("ShowVerse", "ShowText", "ShowQuickPresentation") and r.get("ok") is True for r in api):
            missing.append("Нет успешной команды показа; одних запросов состояния Holyrics недостаточно.")
        critical = [r for r in events if r.get("event") in (
            "audio_open_error", "audio_stream_error", "asr_startup_error", "audio_callback_error",
            "audio_status", "text_detection_startup_error",
        ) or (r.get("event") == "STREAMING_SLIDE_CONTROL" and r.get("ok") is False)]
        if critical:
            failures.append(f"Ошибок звука, распознавания или управления УПС: {len(critical)}.")
        # Actual display remains an operator check. These are only logged
        # indications that the requested scenarios were exercised.
        parsed = [r.get("payload") or {} for r in events if r.get("event") == "parsed"]
        report["coverage"] = {
            "references": sum(bool(p.get("ref")) for p in parsed),
            "reference_lists": sum(bool(p.get("reference_list")) for p in parsed),
            "slide_transitions": sum(r.get("event") == "STREAMING_SLIDE_CONTROL" and r.get("ok") is True
                                     and isinstance(r.get("target_index"), int) and isinstance(r.get("current_index"), int)
                                     and r["target_index"] > r["current_index"] for r in events),
        }
        if not report["coverage"]["references"]:
            missing.append("Не проверено распознавание адресов.")
        if not report["coverage"]["reference_lists"]:
            missing.append("Не проверен список ссылок.")
        if not report["coverage"]["slide_transitions"]:
            missing.append("Не проверены переходы УПС при чтении длинного отрывка.")
        report["limitations"].append("Пороговые значения — правило предварительной проверки, а не измеренная граница возможностей церковного ПК.")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        missing.append(f"Не удалось полностью прочитать диагностические данные: {exc}")
    report["status"] = "failed" if failures else "insufficient" if missing else "conditional"
    return report


def format_session_check(report: dict) -> str:
    titles = {"conditional": "УСЛОВНО МОЖНО ИСПОЛЬЗОВАТЬ ПОД КОНТРОЛЕМ ОПЕРАТОРА",
              "failed": "НЕ РЕКОМЕНДУЕТСЯ ЗАПУСКАТЬ НА БОГОСЛУЖЕНИИ",
              "insufficient": "НЕДОСТАТОЧНО ДАННЫХ ДЛЯ РЕШЕНИЯ"}
    lines = [titles[report["status"]], f"LV {report['version']}; сеанс: {report['session']}",
             f"Непустых фраз: {report.get('finals', 0)}; замеров: {report.get('measurements', 0)}; интервал речи: {report.get('speech_span_seconds', 0)} с."]
    if report.get("host"):
        host = report["host"]
        lines.append(f"Компьютер: {host.get('cpu_name', 'неизвестен')}; логических CPU: {host.get('logical_cpu_count', '?')}; ОС: {host.get('os', '?')}.")
    for field, label, divisor, unit in (
        ("audio_callback_to_asr_final_ms", "Готовность текста", 1000, "с"),
        ("audio_callback_to_decision_ready_ms", "Подготовка решения", 1000, "с"),
        ("reference_parse_ms", "Разбор адреса", 1000, "с"),
        ("queue_seconds", "Очередь звука", 1, "с"),
        ("system_cpu_percent", "Загрузка всего компьютера в измеренных интервалах", 1, "%"),
    ):
        values = report["metrics"].get(field, {})
        if values.get("count"):
            lines.append(f"{label}: медиана {values['median']/divisor:.2f} {unit}; 95% ≤ {values['p95']/divisor:.2f} {unit}; максимум {values['max']/divisor:.2f} {unit}.")
    lines.extend(f"Проблема: {reason}" for reason in report["failures"])
    lines.extend(f"Не проверено: {reason}" for reason in report["missing"])
    lines.extend(f"Примечание: {reason}" for reason in report.get("notes", []))
    coverage = report.get("coverage", {})
    lines.append(f"В журнале: ссылки {coverage.get('references', 0)}, списки {coverage.get('reference_lists', 0)}, успешные команды перехода УПС {coverage.get('slide_transitions', 0)}.")
    lines.extend(report["limitations"])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize vosk_grammar_probe JSONL logs.")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--check-session", action="store_true", help="Offline readiness check of one stopped live session.")
    parser.add_argument(
        "--performance", action="store_true",
        help="Print JSON statistics and CPU correlations for saved performance.jsonl intervals.",
    )
    parser.add_argument(
        "--export-training-data",
        type=Path,
        help="Write reviewed trigger cases as CSV for ML experiments.",
    )
    parser.add_argument(
        "--export-smart-slide-training",
        type=Path,
        help="Write reviewed UPS shadow decisions as a separate CSV for its Naive Bayes safety model.",
    )
    parser.add_argument(
        "--asr-engine",
        default="",
        help="For training export, include only sessions from this ASR engine, for example sherpa-0.54.",
    )
    parser.add_argument(
        "--train-risk-model",
        type=Path,
        help="Train a simple stdlib Naive Bayes risk model from training CSV.",
    )
    parser.add_argument(
        "--train-smart-slide-model",
        type=Path,
        help="Train the separate Naive Bayes safety model for proposed UPS transitions.",
    )
    parser.add_argument(
        "--model-output",
        type=Path,
        default=Path(".cache") / "liverse" / "ml" / "risk_model.json",
        help="Where to write --train-risk-model JSON model.",
    )
    parser.add_argument(
        "--model-report",
        type=Path,
        default=Path(".cache") / "liverse" / "ml" / "risk_model_report.json",
        help="Where to write --train-risk-model validation report.",
    )
    parser.add_argument(
        "--smart-slide-model-output",
        type=Path,
        default=Path(".cache") / "liverse" / "ml" / "smart_slide_model.json",
        help="Where to write --train-smart-slide-model JSON model.",
    )
    parser.add_argument(
        "--smart-slide-model-report",
        type=Path,
        default=Path(".cache") / "liverse" / "ml" / "smart_slide_model_report.json",
        help="Where to write --train-smart-slide-model validation report.",
    )
    args = parser.parse_args()

    if args.check_session:
        report = check_live_session(args.log_dir)
        print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else format_session_check(report))
        return {"conditional": 0, "failed": 1, "insufficient": 2}[report["status"]]

    if args.performance:
        print(json.dumps(summarize_performance(args.log_dir), ensure_ascii=False, indent=2))
        return 0

    if args.export_training_data:
        export = export_training_data(
            args.log_dir,
            args.export_training_data,
            asr_engine=args.asr_engine,
        )
        print(f"Обучающий CSV: {export['output']}")
        print(f"Строк: {export['rows']}  столбцов: {export['columns']}")
        if export["excluded"]:
            print(f"Исключено из обучения: {export['excluded']}")
            for item in export["excluded_cases"]:
                print(f"  - {item['run']} {item['case_id']} {item['ref']}: {item['reason']}")
        print(f"target_confirm=1: {export['target_confirm']}  target_confirm=0: {export['target_auto']}")
        print("Категории:")
        for category, count in export["categories"]:
            label = CATEGORY_LABELS.get(category, category or "без категории")
            print(f"  {count:>3}  {category} ({label})")
        return 0

    if args.export_smart_slide_training:
        export = export_smart_slide_training_data(
            args.log_dir,
            args.export_smart_slide_training,
            asr_engine=args.asr_engine,
        )
        print(f"CSV УПС: {export['output']}")
        print(f"Всего размеченных решений: {export['rows']}  столбцов: {export['columns']}")
        print(
            "Для НБА безопасности перехода: "
            f"{export['eligible_rows']} (неверных: {export['target_confirm']}, "
            f"верных: {export['target_auto']})"
        )
        print("Категории:")
        for category, count in export["categories"]:
            label = CATEGORY_LABELS.get(category) or dict(SMART_SLIDE_CATEGORIES.values()).get(category, category)
            print(f"  {count:>3}  {category} ({label})")
        return 0

    if args.train_risk_model:
        report = train_risk_model(args.train_risk_model, args.model_output, args.model_report)
        print(f"Модель: {report['model']}")
        print(f"Отчёт: {args.model_report}")
        print(
            f"Строк: {report['rows']}  train: {report['train_rows']}  "
            f"validation: {report['validation_rows']}"
        )
        print("Классы:")
        for target, count in sorted(report["target_counts"].items()):
            label = "оператору" if target == "1" else "автоматически"
            print(f"  {target}: {count} ({label})")
        print("Validation:")
        for item in report["validation"]:
            print(
                f"  threshold >= {item['threshold']}: accuracy={item['accuracy']} "
                f"ошибок оператору {item['error_caught']}, "
                f"ошибок пропущено {item['error_missed']}, "
                f"верных оператору {item['true_confirm']}, "
                f"верных автоматически {item['true_auto']}"
            )
        print("Risk score baseline на той же validation:")
        for item in report["risk_score_baseline"]:
            print(
                f"  risk_score >= {item['threshold']}: accuracy={item['accuracy']} "
                f"ошибок оператору {item['error_caught']}, "
                f"ошибок пропущено {item['error_missed']}, "
                f"верных оператору {item['true_confirm']}, "
                f"верных автоматически {item['true_auto']}"
            )
        return 0

    if args.train_smart_slide_model:
        report = train_smart_slide_model(
            args.train_smart_slide_model,
            args.smart_slide_model_output,
            args.smart_slide_model_report,
        )
        print(f"Модель УПС: {report['model']}")
        print(f"Отчёт: {args.smart_slide_model_report}")
        print(
            f"Строк: {report['rows']}  train: {report['train_rows']}  "
            f"validation: {report['validation_rows']}"
        )
        print("Классы: 0 — переход безопасен, 1 — переход нужно передать оператору.")
        for target, count in sorted(report["target_counts"].items()):
            print(f"  {target}: {count}")
        return 0

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
