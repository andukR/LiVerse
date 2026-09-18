"""Pure decision rules for following a known ordered text sequence."""

from __future__ import annotations

from typing import Any, Callable, Mapping


ASSISTED_MIN_SCORE = 45.0
ASSISTED_MIN_MARGIN = 8.0
ASSISTED_MIN_MATCHED_WORDS = 3
INITIAL_SLIDE_MIN_MATCHED_WORDS = 3
NEARBY_SHORT_MIN_SCORE = ASSISTED_MIN_SCORE
NEARBY_SHORT_MIN_MARGIN = ASSISTED_MIN_MARGIN
NEARBY_SHORT_MIN_MATCHED_WORDS = 2
# After a weak/assisted move, do not let a short continuation of the same ASR
# tail immediately advance the newly shown verse.  Five content words is still
# below the normal strong-match evidence, but is enough to distinguish a fresh
# verse ending from the two-to-four-word tail that caused the assisted move.
ASSISTED_CURRENT_CONFIRMATION_MIN_MATCHED_WORDS = 5
# While waiting for a fresh confirmation after a synchronization, allow a
# robust local match of the immediately following verse to recover from a
# detector result that was not promoted to TEXT_ACCEPTED. This never applies
# to a distant candidate.
AWAITING_ADJACENT_NEXT_MIN_SCORE = 60.0
AWAITING_ADJACENT_NEXT_MIN_MARGIN = 30.0
AWAITING_ADJACENT_NEXT_MIN_MATCHED_WORDS = 5
ASSISTED_REASONS = {
    "candidate_ready",
    "margin_below_threshold",
    "not_enough_matched_content_words",
    "pending_confirmation",
    "score_below_threshold",
}


def _value(candidate: Any, name: str, default: Any = None) -> Any:
    if isinstance(candidate, Mapping):
        return candidate.get(name, default)
    return getattr(candidate, name, default)


def _position(chapter: object, verse: object) -> tuple[int, int]:
    return int(chapter or 0), int(verse or 0)


def _target_bounds(target: Mapping[str, object]) -> tuple[tuple[int, int], tuple[int, int]]:
    end = _position(target.get("chapter"), target.get("verse"))
    start = _position(
        target.get("start_chapter", target.get("chapter")),
        target.get("start_verse", target.get("verse")),
    )
    return start, end


def _candidate_bounds(candidate: Any) -> tuple[tuple[int, int], tuple[int, int]]:
    start = _position(_value(candidate, "chapter"), _value(candidate, "start_verse"))
    end = _position(
        _value(candidate, "end_chapter", _value(candidate, "chapter")),
        _value(candidate, "end_verse", _value(candidate, "start_verse")),
    )
    return start, end


def _candidate_target_index(targets: list[Mapping[str, object]], candidate: Any) -> int | None:
    candidate_start, candidate_end = _candidate_bounds(candidate)
    for index, target in enumerate(targets):
        target_start, target_end = _target_bounds(target)
        if candidate_start <= target_end and candidate_end >= target_start:
            return index
    return None


def decide_sequence_advance(
    state: Mapping[str, object] | None,
    candidate: Any,
    *,
    accepted: bool,
    reason: str,
    score: float,
    margin: float,
    matched_words: int,
    speech_continues: bool = True,
) -> dict[str, object]:
    """Return a JSON-safe shadow decision without changing presentation state."""
    if not isinstance(state, Mapping):
        return {"active": False, "action": "ignore", "reason": "inactive"}
    targets = [target for target in state.get("targets") or [] if isinstance(target, Mapping)]
    if not targets or candidate is None:
        return {"active": bool(targets), "action": "ignore", "reason": "no_candidate"}
    current_index = max(0, min(int(state.get("current_index") or 0), len(targets) - 1))
    candidate_book_id = int(_value(candidate, "book_id", 0) or 0)
    expected_book_id = int(state.get("book_id") or 0)
    candidate_index = _candidate_target_index(targets, candidate)
    base = {
        "active": True,
        "current_index": current_index,
        "candidate_index": candidate_index,
        "current_element": dict(targets[current_index]),
        "candidate_element": (
            dict(targets[candidate_index]) if candidate_index is not None else None
        ),
        "expected_indices": [
            index for index in (current_index, current_index + 1) if index < len(targets)
        ],
        "candidate": str(_value(candidate, "reference", "") or ""),
        "score": round(float(score), 3),
        "margin": round(float(margin), 3),
        "matched_words": int(matched_words),
    }
    if expected_book_id and candidate_book_id != expected_book_id:
        return {**base, "action": "ignore", "reason": "outside_sequence_book"}
    if candidate_index is None:
        return {**base, "action": "ignore", "reason": "outside_sequence"}
    if candidate_index < current_index:
        return {**base, "action": "ignore", "reason": "automatic_backward_move_forbidden"}

    strong = bool(accepted or reason == "pending_confirmation")
    awaiting_current_confirmation = bool(state.get("await_current_confirmation"))
    assisted = bool(
        speech_continues
        and reason in ASSISTED_REASONS
        and float(score) >= ASSISTED_MIN_SCORE
        and float(margin) >= ASSISTED_MIN_MARGIN
        and int(matched_words) >= ASSISTED_MIN_MATCHED_WORDS
    )
    nearby_short = bool(
        speech_continues
        and candidate_index <= current_index + 1
        and reason in ASSISTED_REASONS
        and float(score) >= NEARBY_SHORT_MIN_SCORE
        and float(margin) >= NEARBY_SHORT_MIN_MARGIN
        and int(matched_words) >= NEARBY_SHORT_MIN_MATCHED_WORDS
        and int(matched_words) < ASSISTED_MIN_MATCHED_WORDS
    )
    assisted = assisted or nearby_short
    target_start, target_end = _target_bounds(targets[current_index])
    candidate_start, candidate_end = _candidate_bounds(candidate)
    reaches_current_boundary = candidate_start <= target_end <= candidate_end
    ending_overlap_words = int(_value(candidate, "ending_overlap_words", 0) or 0)

    # A distant recovery identifies a later verse, but the same recognition
    # window often continues to contain that verse.  Do not interpret its
    # repeated words as the end of the newly selected slide.
    if state.get("recovered_element_pending"):
        if candidate_index == current_index:
            return {
                **base,
                "action": "keep",
                "reason": "await_recovered_element_confirmation",
            }
        if candidate_index > current_index:
            if isinstance(state, dict):
                state.pop("recovered_element_pending", None)

    # An assisted move can be based on a short/weak tail from the previous
    # verse. Do not let another weak candidate immediately cascade to the
    # following slide: first require a strong match for the verse now shown.
    # This is evidence-gated rather than time-gated, so a clear next-verse
    # match may still synchronize immediately.
    awaiting_adjacent_next_recovery = bool(
        awaiting_current_confirmation
        and candidate_index == current_index + 1
        and speech_continues
        and reason in ASSISTED_REASONS
        and float(score) >= AWAITING_ADJACENT_NEXT_MIN_SCORE
        and float(margin) >= AWAITING_ADJACENT_NEXT_MIN_MARGIN
        and int(matched_words) >= AWAITING_ADJACENT_NEXT_MIN_MATCHED_WORDS
    )
    if awaiting_current_confirmation and not strong and not awaiting_adjacent_next_recovery:
        if candidate_index in {current_index, current_index + 1}:
            return {
                **base,
                "action": "keep",
                "reason": "await_current_element_confirmation",
            }
    if awaiting_current_confirmation and candidate_index == current_index and strong:
        # A strong result can still be only the completed tail of the same
        # recognition window that triggered an assisted transition.  Require
        # a little more fresh content before advancing again; a strong match
        # of the *next* element remains allowed below and can synchronize
        # without waiting for a short current verse to finish twice.
        if int(matched_words) < ASSISTED_CURRENT_CONFIRMATION_MIN_MATCHED_WORDS:
            return {
                **base,
                "action": "keep",
                "reason": "await_current_element_confirmation",
            }

    if candidate_index == current_index:
        # A long range is announced first, so its first verse is not yet
        # necessarily visible.  Do not wait for the end of that verse: a
        # sufficiently supported match is evidence that reading has begun.
        # The explicit False preserves the legacy behaviour for callers that
        # do not model the visible state of the initial slide.
        if (
            state.get("current_slide_visible") is False
            and int(matched_words) >= INITIAL_SLIDE_MIN_MATCHED_WORDS
            and (strong or assisted)
        ):
            activated = {
                **base,
                "action": "activate",
                "reason": (
                    "strong_initial_element" if strong else "assisted_initial_element"
                ),
            }
            if strong:
                activated["confirmation_state"] = "confirmed"
            return activated
        if not reaches_current_boundary:
            return {
                **base,
                "action": "keep",
                **({"confirmation_state": "confirmed"} if strong else {}),
                "reason": "inside_current_element",
            }
        if ending_overlap_words < 2:
            return {
                **base,
                "action": "keep",
                **({"confirmation_state": "confirmed"} if strong else {}),
                "reason": "current_end_not_heard",
            }
        if not (strong or assisted):
            return {**base, "action": "keep", "reason": "current_boundary_not_confident"}
        if current_index == len(targets) - 1:
            return {
                **base,
                "action": "complete",
                "confirmation_state": "confirmed" if strong else "required",
                "reason": "strong_final_boundary" if strong else "assisted_final_boundary",
            }
        # Once the end of the current verse is clearly heard, prefer showing
        # the next known verse immediately.  In live reading it is less
        # harmful to briefly show an unread next verse than to leave the
        # congregation on a verse that has already been read.  The ending
        # overlap and confidence checks above still prevent weak/partial
        # matches from triggering this transition.
        return {
            **base,
            "action": "advance" if strong else "assisted_advance",
            "confirmation_state": "confirmed" if strong else "required",
            "target_index": current_index + 1,
            "target_element": dict(targets[current_index + 1]),
            "reason": (
                "strong_current_boundary_auto_next"
                if strong
                else "assisted_current_boundary_auto_next"
            ),
        }

    if candidate_index == current_index + 1 and (strong or assisted):
        return {
            **base,
            "action": "synchronize_forward" if strong else "assisted_synchronize_forward",
            # A next-verse match identifies the slide to show, but the same
            # recognition window may immediately contain the end of that
            # verse.  Require a fresh confirmation before that tail can move
            # on again; this prevents a strong-next cascade as well as an
            # assisted cascade.
            "confirmation_state": "required",
            "target_index": candidate_index,
            "target_element": dict(targets[candidate_index]),
            "reason": (
                "strong_next_element"
                if strong
                else (
                    "nearby_short_next_element"
                    if nearby_short
                    else "assisted_next_element"
                )
            ),
        }
    distant_recovery = bool(
        state.get("allow_distant_skip")
        and candidate_index > current_index + 1
        and float(score) >= 80.0
        and float(margin) >= 30.0
        and int(matched_words) >= 5
        and (accepted or reason == "duplicate_cooldown")
    )
    if candidate_index > current_index + 1 and (strong or distant_recovery):
        if distant_recovery:
            if isinstance(state, dict):
                state["recovered_element_pending"] = True
            return {
                **base,
                "action": "synchronize_forward",
                # A skipped-verse recovery identifies the new current slide,
                # but does not prove that its ending was heard.  Require a
                # fresh confirmation so a repeated fragment of the same
                # verse cannot immediately advance to the next unread slide.
                "confirmation_state": "required",
                "target_index": candidate_index,
                "target_element": dict(targets[candidate_index]),
                "skipped_indices": list(range(current_index + 1, candidate_index)),
                "reason": "strong_later_element_skip",
            }
        # A single recognition window may contain enough text from a later
        # verse to identify it directly.  Do not jump over intermediate
        # slides: show exactly one next verse and let the next recognition
        # window confirm/advance it.
        return {
            **base,
            "action": "synchronize_forward",
            "confirmation_state": "confirmed",
            "target_index": current_index + 1,
            "target_element": dict(targets[current_index + 1]),
            "reason": "strong_later_element",
        }
    return {**base, "action": "ignore", "reason": "weak_distant_element"}


def decide_sequence_advance_from_text(
    state: Mapping[str, object] | None,
    global_decision: Any,
    sequence_decision: Any,
) -> dict[str, object]:
    """Prefer nearby sequence evidence, retaining strong global catch-up."""
    evaluated: list[tuple[str, dict[str, object]]] = []
    for source, text_decision in (
        ("sequence_scoped", sequence_decision),
        ("global_fallback", global_decision),
    ):
        candidate = _value(text_decision, "top_candidate")
        if candidate is None:
            continue
        decision = decide_sequence_advance(
            state,
            candidate,
            accepted=bool(_value(text_decision, "accepted", False)),
            reason=str(_value(text_decision, "reason", "") or ""),
            score=float(_value(text_decision, "score", 0.0) or 0.0),
            margin=float(_value(text_decision, "margin", 0.0) or 0.0),
            matched_words=int(_value(text_decision, "matched_words", 0) or 0),
            speech_continues=True,
        )
        evaluated.append((source, decision))
        if decision.get("action") in {
            "advance", "assisted_advance", "synchronize_forward",
            "assisted_synchronize_forward", "complete",
        }:
            return {**decision, "evidence_source": source}
    if evaluated:
        source, decision = evaluated[0]
        return {**decision, "evidence_source": source}
    return {
        "active": bool(state),
        "action": "ignore",
        "reason": "no_candidate",
        "evidence_source": "none",
    }


def decide_sequence_progress_from_text(
    state: Mapping[str, object] | None,
    global_decision: Any,
    sequence_evaluator: Callable[[Mapping[str, object]], Any],
    *,
    max_steps: int = 1,
) -> tuple[dict[str, object], Any]:
    """Process one speech window and move by at most one slide."""
    working_state = dict(state) if isinstance(state, Mapping) else {}
    original_index = int(working_state.get("current_index") or 0)
    steps: list[dict[str, object]] = []
    step_evidence: list[Any] = []
    evidence_decision = None
    for _unused in range(max(1, int(max_steps))):
        evidence_decision = sequence_evaluator(working_state)
        decision = decide_sequence_advance_from_text(
            working_state,
            global_decision,
            evidence_decision,
        )
        target_index = decision.get("target_index")
        if decision.get("action") not in {
            "advance", "assisted_advance", "synchronize_forward",
            "assisted_synchronize_forward",
        } or not isinstance(target_index, int):
            if not steps:
                return decision, evidence_decision
            break
        steps.append(decision)
        step_evidence.append(evidence_decision)
        working_state["current_index"] = target_index

    if len(steps) == 1:
        return steps[0], step_evidence[0]
    final = dict(steps[-1])
    final["current_index"] = original_index
    targets = [item for item in working_state.get("targets") or [] if isinstance(item, Mapping)]
    if 0 <= original_index < len(targets):
        final["current_element"] = dict(targets[original_index])
    final["action"] = "synchronize_forward"
    final["reason"] = "sequential_window_catch_up"
    final["sequence_steps"] = [
        {
            "candidate": step.get("candidate"),
            "target_index": step.get("target_index"),
            "reason": step.get("reason"),
        }
        for step in steps
    ]
    return final, step_evidence[-1]
