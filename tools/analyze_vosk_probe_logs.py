#!/usr/bin/env python3
"""Summarize vosk_grammar_probe JSONL logs."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import Counter
from pathlib import Path

from bible_parser_core.live_pipeline import score_reference_risk
from tools.review_trigger_cases import CATEGORY_LABELS, case_signature, is_unreviewed, load_jsonl, session_metadata


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
    return TRAINING_EXCLUDED_CASES.get((cases_path.parent.name, str(case.get("case_id") or "")))


def export_training_data(log_dir: Path, output_path: Path) -> dict:
    rows = []
    excluded: list[dict[str, str]] = []
    for cases_path, case in reviewed_trigger_cases(log_dir):
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
        for feature in vocabulary:
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize vosk_grammar_probe JSONL logs.")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument(
        "--export-training-data",
        type=Path,
        help="Write reviewed trigger cases as CSV for ML experiments.",
    )
    parser.add_argument(
        "--train-risk-model",
        type=Path,
        help="Train a simple stdlib Naive Bayes risk model from training CSV.",
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
    args = parser.parse_args()

    if args.export_training_data:
        export = export_training_data(args.log_dir, args.export_training_data)
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
