"""Shared streaming Sherpa-ONNX adapter for live and replay recognition."""

from __future__ import annotations

import json
import hashlib
import math
import ssl
import time
from pathlib import Path
from urllib.request import Request, urlopen


SHERPA_MODEL_REVISION = "5fbd908cb21cbd585fa21461f463ba41fbcbcb68"
DEFAULT_SHERPA_THREADS = 1
SHERPA_MODEL_BASE_URL = (
    "https://huggingface.co/alphacep/vosk-model-small-streaming-ru/resolve/"
    f"{SHERPA_MODEL_REVISION}"
)
SHERPA_MODEL_FILES = {
    "am-onnx/encoder.onnx": "e9c27453e618bc97cf8a10169f34c104bd478166522907fcd122a46a88c78c69",
    "am-onnx/decoder.onnx": "89b3088a9e20e1ef7f2e85ce1a3478afe6a9c4ac57369cabcc4beb8e95328ea0",
    "am-onnx/joiner.onnx": "dde0c7f3be0a16113a3e042c79a492c48667c07a8c1e9422ffe81c768aad4838",
    "lang/tokens.txt": "93bbbc0bae6b78c0bbb743d4aa9fded3bb5ff3aac5f0200e3a769a5a05e0fdf6",
}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_sherpa_model(model_path: Path, *, attempts: int = 3) -> None:
    """Download the pinned 0.54 model once and verify every file."""
    try:
        import certifi

        ssl_context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ssl_context = ssl.create_default_context()
    for relative_path, expected_hash in SHERPA_MODEL_FILES.items():
        destination = model_path / relative_path
        if destination.is_file() and file_sha256(destination) == expected_hash:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f"{destination.name}.part")
        request = Request(
            f"{SHERPA_MODEL_BASE_URL}/{relative_path}?download=true",
            headers={"User-Agent": "LiVerse-model-installer"},
        )
        last_error: Exception | None = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                print(f"LiVerse: скачивается модель Vosk 0.54: {relative_path}", flush=True)
                with urlopen(request, timeout=120, context=ssl_context) as response, temporary.open("wb") as target:
                    while chunk := response.read(1024 * 1024):
                        target.write(chunk)
                if file_sha256(temporary) != expected_hash:
                    raise RuntimeError(f"контрольная сумма не совпала: {relative_path}")
                temporary.replace(destination)
                last_error = None
                break
            except (OSError, RuntimeError) as exc:
                last_error = exc
                temporary.unlink(missing_ok=True)
                if attempt < attempts:
                    time.sleep(2 * attempt)
        if last_error is not None:
            raise RuntimeError(
                f"Не удалось скачать модель Vosk 0.54 ({relative_path}): {last_error}"
            ) from last_error


def sherpa_result_to_vosk_result(result: object, *, time_offset: float = 0.0) -> dict:
    """Convert Sherpa subword output to the result shape used by LiVerse."""
    text = str(getattr(result, "text", "") or "").strip()
    tokens = list(getattr(result, "tokens", []) or [])
    timestamps = list(getattr(result, "timestamps", []) or [])
    probabilities = list(getattr(result, "ys_probs", []) or [])
    grouped: list[dict] = []
    current: dict | None = None

    for index, token_value in enumerate(tokens):
        token = str(token_value)
        starts_word = token.startswith(" ")
        piece = token.lstrip() if starts_word else token
        if not piece:
            continue
        timestamp = float(timestamps[index]) if index < len(timestamps) else 0.0
        log_probability = float(probabilities[index]) if index < len(probabilities) else 0.0
        if starts_word or current is None:
            if current is not None:
                grouped.append(current)
            current = {
                "word": piece,
                "start": time_offset + timestamp,
                "last_timestamp": time_offset + timestamp,
                "log_probabilities": [log_probability],
            }
        else:
            current["word"] += piece
            current["last_timestamp"] = time_offset + timestamp
            current["log_probabilities"].append(log_probability)
    if current is not None:
        grouped.append(current)

    words: list[dict] = []
    for index, item in enumerate(grouped):
        start = float(item["start"])
        if index + 1 < len(grouped):
            end = max(start, float(grouped[index + 1]["start"]))
        else:
            end = max(start, float(item["last_timestamp"]) + 0.2)
        log_probabilities = list(item["log_probabilities"])
        confidence = math.exp(sum(log_probabilities) / len(log_probabilities))
        words.append(
            {
                "word": str(item["word"]),
                "start": round(start, 3),
                "end": round(end, 3),
                "conf": round(max(0.0, min(1.0, confidence)), 6),
            }
        )
    return {"text": text, "result": words}


def estimate_recent_speech_rate(result: object, *, window_words: int = 6) -> float | None:
    """Estimate recent words/second from Sherpa token timestamps.

    Partial Sherpa hypotheses can revise their tail, so this deliberately uses
    only a short recent window and returns no estimate until three word starts
    are available.
    """
    if isinstance(result, dict) and isinstance(result.get("result"), list):
        timed = result["result"]
    else:
        timed = sherpa_result_to_vosk_result(result).get("result", [])
    starts = [float(word["start"]) for word in timed[-max(3, int(window_words)):]]
    if len(starts) < 3:
        return None
    elapsed = starts[-1] - starts[0]
    if elapsed < 0.4:
        return None
    rate = (len(starts) - 1) / elapsed
    if not math.isfinite(rate) or rate <= 0.0:
        return None
    return round(rate, 3)


def adaptive_sherpa_segment_seconds(
    speech_rate_wps: float | None,
    *,
    target_words: float = 10.0,
    minimum_seconds: float = 3.0,
    maximum_seconds: float = 10.0,
) -> float:
    """Choose a replay segment length that aims for a similar word count."""
    if speech_rate_wps is None or not math.isfinite(float(speech_rate_wps)) or speech_rate_wps <= 0:
        return float(maximum_seconds)
    raw_seconds = float(target_words) / float(speech_rate_wps)
    bounded = max(float(minimum_seconds), min(float(maximum_seconds), raw_seconds))
    # Audio arrives in 250 ms chunks; quantize the threshold to that cadence.
    return round(bounded * 4.0) / 4.0


def boundary_window_target_words(
    current_word_count: int,
    next_word_count: int,
    *,
    minimum_words: int = 6,
    maximum_words: int = 12,
) -> int:
    """Choose a small word budget around one current-to-next verse boundary."""
    current = max(0, int(current_word_count))
    following = max(0, int(next_word_count))
    current_tail = min(4, max(2, current // 5)) if current else 2
    next_head = min(5, max(2, following // 3)) if following else 2
    return max(minimum_words, min(maximum_words, current_tail + next_head + 2))


def load_sherpa_recognizer(
    model_path: Path,
    *,
    sample_rate: int,
    num_threads: int,
    min_utterance_length: float = 10.0,
) -> object:
    required = {
        "encoder": model_path / "am-onnx" / "encoder.onnx",
        "decoder": model_path / "am-onnx" / "decoder.onnx",
        "joiner": model_path / "am-onnx" / "joiner.onnx",
        "tokens": model_path / "lang" / "tokens.txt",
    }
    missing = [str(path) for path in required.values() if not path.is_file()]
    if missing:
        raise RuntimeError(
            "Не найдены файлы потоковой модели Vosk 0.54:\n  - "
            + "\n  - ".join(missing)
            + "\nПовторно запустите установщик LiVerse при подключённом Интернете."
        )
    try:
        import sherpa_onnx
    except ImportError as error:
        raise RuntimeError(
            "Не установлена библиотека sherpa-onnx. Повторно запустите установщик LiVerse."
        ) from error
    return sherpa_onnx.OnlineRecognizer.from_transducer(
        encoder=str(required["encoder"]),
        decoder=str(required["decoder"]),
        joiner=str(required["joiner"]),
        tokens=str(required["tokens"]),
        num_threads=max(1, num_threads),
        sample_rate=sample_rate,
        feature_dim=80,
        dither=3e-5,
        decoding_method="modified_beam_search",
        max_active_paths=10,
        enable_endpoint_detection=True,
        rule1_min_trailing_silence=2.4,
        rule2_min_trailing_silence=0.8,
        rule3_min_utterance_length=max(0.0, float(min_utterance_length)),
    )


class SherpaStreamingRecognizer:
    """Adapter with the KaldiRecognizer methods used by live LiVerse."""

    def __init__(self, recognizer: object, sample_rate: int) -> None:
        self.recognizer = recognizer
        self.sample_rate = sample_rate
        self.stream = recognizer.create_stream()
        self.time_offset = 0.0
        self.seconds_seen = 0.0
        self.last_result: dict = {"text": "", "result": []}

    def AcceptWaveform(self, data: bytes) -> bool:  # noqa: N802 - Vosk-compatible name
        import numpy as np

        samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        self.seconds_seen += len(samples) / float(self.sample_rate)
        self.stream.accept_waveform(self.sample_rate, samples)
        while self.recognizer.is_ready(self.stream):
            self.recognizer.decode_stream(self.stream)
        if not self.recognizer.is_endpoint(self.stream):
            return False
        self.last_result = sherpa_result_to_vosk_result(
            self.recognizer.get_result_all(self.stream),
            time_offset=self.time_offset,
        )
        self.recognizer.reset(self.stream)
        self.time_offset = self.seconds_seen
        return True

    def Result(self) -> str:  # noqa: N802 - Vosk-compatible name
        return json.dumps(self.last_result, ensure_ascii=False)

    def PartialResult(self) -> str:  # noqa: N802 - Vosk-compatible name
        raw_result = self.recognizer.get_result_all(self.stream)
        return json.dumps(
            {"partial": str(getattr(raw_result, "text", "") or "").strip()},
            ensure_ascii=False,
        )


class SherpaReplayRecognizer:
    """Adapter used by saved-audio replay."""

    def __init__(
        self,
        recognizer: object,
        sample_rate: int,
        *,
        adaptive_segmentation: bool = False,
    ) -> None:
        self.recognizer = recognizer
        self.sample_rate = sample_rate
        self.stream = recognizer.create_stream()
        self.time_offset = 0.0
        self.segment_start_seconds = 0.0
        self.adaptive_segmentation = adaptive_segmentation
        self.speech_rate_wps: float | None = None
        self.last_partial_raw_result: object | None = None
        self.boundary_target_words: float = 10.0

    def set_boundary_word_target(self, current_text: str | None, next_text: str | None) -> None:
        """Set the replay-only word budget for the current adjacent boundary."""
        current_count = len(str(current_text or "").split())
        next_count = len(str(next_text or "").split())
        self.boundary_target_words = float(
            boundary_window_target_words(current_count, next_count)
        )

    def _finish_segment(
        self,
        replay_seconds: float,
        *,
        reason: str,
        speech_rate_wps: float | None = None,
        adaptive_limit_seconds: float | None = None,
    ) -> list[dict]:
        if reason == "adaptive_interval":
            self.stream.input_finished()
            while self.recognizer.is_ready(self.stream):
                self.recognizer.decode_stream(self.stream)
        raw_result = self.recognizer.get_result_all(self.stream)
        result = sherpa_result_to_vosk_result(
            raw_result,
            time_offset=self.time_offset,
        )
        self.recognizer.reset(self.stream)
        segment_duration = max(0.0, replay_seconds - self.segment_start_seconds)
        self.time_offset = replay_seconds
        self.segment_start_seconds = replay_seconds
        self.speech_rate_wps = None
        self.last_partial_raw_result = None
        if result["text"]:
            result["segmentation_reason"] = reason
            result["segment_duration_seconds"] = round(segment_duration, 3)
            result["speech_rate_wps"] = speech_rate_wps
            result["adaptive_limit_seconds"] = adaptive_limit_seconds
            result["boundary_target_words"] = self.boundary_target_words
            return [result]
        return []

    def accept_waveform(self, data: bytes, replay_seconds: float) -> list[dict]:
        import numpy as np

        samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        self.stream.accept_waveform(self.sample_rate, samples)
        while self.recognizer.is_ready(self.stream):
            self.recognizer.decode_stream(self.stream)
        adaptive_limit = None
        if self.adaptive_segmentation:
            raw_result = self.recognizer.get_result_all(self.stream)
            self.last_partial_raw_result = raw_result
            measured_rate = estimate_recent_speech_rate(raw_result)
            if measured_rate is not None:
                self.speech_rate_wps = measured_rate
            adaptive_limit = adaptive_sherpa_segment_seconds(
                self.speech_rate_wps,
                target_words=self.boundary_target_words,
            )
        if self.recognizer.is_endpoint(self.stream):
            return self._finish_segment(
                replay_seconds,
                reason="sherpa_endpoint",
                speech_rate_wps=self.speech_rate_wps,
                adaptive_limit_seconds=adaptive_limit,
            )
        if not self.adaptive_segmentation:
            return []
        segment_limit = adaptive_limit or adaptive_sherpa_segment_seconds(
            self.speech_rate_wps,
            target_words=self.boundary_target_words,
        )
        if replay_seconds - self.segment_start_seconds >= segment_limit:
            return self._finish_segment(
                replay_seconds,
                reason="adaptive_interval",
                speech_rate_wps=self.speech_rate_wps,
                adaptive_limit_seconds=segment_limit,
            )
        return []

    def partial_result(self) -> str:
        """Return the current non-final text without resetting the stream."""
        raw_result = self.recognizer.get_result_all(self.stream)
        return str(getattr(raw_result, "text", "") or "").strip()

    def partial_result_with_speech_rate(self) -> tuple[str, float | None]:
        """Return current partial text and a local recent speech-rate estimate."""
        raw_result = self.last_partial_raw_result
        if raw_result is None:
            raw_result = self.recognizer.get_result_all(self.stream)
        text = str(getattr(raw_result, "text", "") or "").strip()
        rate = estimate_recent_speech_rate(raw_result)
        return text, rate if rate is not None else self.speech_rate_wps

    def final_results(self) -> list[dict]:
        import numpy as np

        self.stream.accept_waveform(
            self.sample_rate,
            np.zeros(int(self.sample_rate * 0.6), dtype=np.float32),
        )
        self.stream.input_finished()
        while self.recognizer.is_ready(self.stream):
            self.recognizer.decode_stream(self.stream)
        result = sherpa_result_to_vosk_result(
            self.recognizer.get_result_all(self.stream),
            time_offset=self.time_offset,
        )
        return [result] if result["text"] else []
