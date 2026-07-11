"""Naive Bayes risk model helpers for LiVerse semi-automatic mode."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any


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


def bool_int(value: bool) -> int:
    return 1 if value else 0


def int_value(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def numeric_value(row: dict[str, Any], column: str) -> float:
    try:
        return float(row.get(column) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def text_features(text: str) -> dict[str, int]:
    normalized = text.lower().replace("ё", "е")
    words = re.findall(r"\w+", normalized, flags=re.UNICODE)
    numbers = re.findall(r"\b\d+\b", normalized)
    return {
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


def asr_metrics(asr_result: dict | None) -> dict[str, float]:
    words = asr_result.get("result") if isinstance(asr_result, dict) else []
    word_items = [item for item in words if isinstance(item, dict)]
    confidences: list[float] = []
    starts: list[float] = []
    ends: list[float] = []
    for item in word_items:
        if "conf" in item:
            confidences.append(numeric_value(item, "conf"))
        if "start" in item and "end" in item:
            starts.append(numeric_value(item, "start"))
            ends.append(numeric_value(item, "end"))

    metrics: dict[str, float] = {
        "asr_word_count": float(len(word_items)),
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
        metrics["asr_words_per_second"] = round(len(word_items) / duration, 3)
    return metrics


def model_row_from_payload(payload: dict, asr_result: dict | None = None) -> dict[str, Any]:
    parsed = payload.get("parsed") if isinstance(payload.get("parsed"), dict) else {}
    text = str(payload.get("text") or "")
    vosk_text = str(payload.get("vosk_text") or "")
    source = str(payload.get("source") or "")
    reasons = set(str(reason) for reason in (payload.get("risk_reasons") or []))
    start_verse = int_value(parsed.get("start_verse") or payload.get("start_verse"))
    end_verse = int_value(parsed.get("end_verse") or payload.get("end_verse"), start_verse)
    row: dict[str, Any] = {
        "book": str(parsed.get("book") or payload.get("book") or ""),
        "source": source,
        "run_source": "live",
        "risk_level": str(payload.get("risk_level") or ""),
        "risk_score": payload.get("risk_score") or 0.0,
        "source_parser": bool_int(source == "parser"),
        "source_resolver": bool_int(source == "resolver"),
        "source_parser_suffix": bool_int(source == "parser_suffix"),
        "source_parser_missing_twenty_range": bool_int(source == "parser_missing_twenty_range"),
        "source_parser_repeated_confusable_range": bool_int(source == "parser_repeated_confusable_range"),
        "run_source_live": 1,
        "run_source_replay": 0,
        "has_slide": bool_int(bool(payload.get("slide") or payload.get("has_slide"))),
        "is_range": bool_int(end_verse > start_verse),
        "verse_count": max(0, end_verse - start_verse + 1),
        "vosk_buffer_parts": len(payload.get("vosk_buffer") or []),
        "candidate_attempts": len(payload.get("attempts") or []),
        "vosk_text": vosk_text,
        "parser_text": text,
    }
    row.update(asr_metrics(asr_result))
    row.update(text_features(f"{text} {vosk_text}"))
    for column in MODEL_FEATURE_COLUMNS:
        if column.startswith("reason_"):
            row[column] = bool_int(column.removeprefix("reason_") in reasons)
    return row


def model_features(row: dict[str, Any]) -> set[str]:
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


def load_risk_model(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def predict_confirm_probability(model: dict, row: dict[str, Any]) -> float:
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


def score_payload_with_model(payload: dict, model: dict, asr_result: dict | None = None) -> dict[str, Any]:
    row = model_row_from_payload(payload, asr_result=asr_result)
    probability = predict_confirm_probability(model, row)
    threshold = float(model.get("recommended_threshold") or 0.3)
    return {
        "confirm_probability": round(probability, 3),
        "threshold": threshold,
        "needs_confirmation": probability >= threshold,
    }
