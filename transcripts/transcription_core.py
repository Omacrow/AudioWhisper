"""
Core chunked-transcription logic.

This is the same algorithm as the standalone chunked_transcribe.py script
(silence-based chunking, sequential per-chunk transcription, timestamp
offset correction, broken-segment flagging) -- factored out into a plain
function with no CLI/argparse/print dependencies, so it can be called
from a Celery task, a management command, tests, or anything else,
without pulling in console-output side effects.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import asdict, dataclass
from typing import List

from pydub import AudioSegment
from pydub.silence import detect_nonsilent

MIN_SILENCE_LEN_MS = 500
SILENCE_THRESH_DB = -40
KEEP_SILENCE_MS = 200
MAX_CHUNK_MS = 30_000
LOW_CONFIDENCE_THRESHOLD = 0.55


@dataclass
class Segment:
    start: float
    end: float
    text: str
    confidence: float
    broken: bool = False


class TranscriptionError(Exception):
    """Raised when a chunk/file can't be transcribed at all (vs. just low confidence)."""


def find_chunk_boundaries(audio: AudioSegment) -> List[tuple]:
    nonsilent_ranges = detect_nonsilent(
        audio, min_silence_len=MIN_SILENCE_LEN_MS, silence_thresh=SILENCE_THRESH_DB,
    )
    if not nonsilent_ranges:
        return [(0, len(audio))]

    boundaries = []
    for start, end in nonsilent_ranges:
        start = max(0, start - KEEP_SILENCE_MS)
        end = min(len(audio), end + KEEP_SILENCE_MS)
        span = end - start
        if span <= MAX_CHUNK_MS:
            boundaries.append((start, end))
        else:
            pos = start
            while pos < end:
                boundaries.append((pos, min(pos + MAX_CHUNK_MS, end)))
                pos += MAX_CHUNK_MS
    return boundaries


def logprob_to_confidence(avg_logprob: float) -> float:
    clamped = max(min(avg_logprob, 0.0), -1.5)
    return round(1.0 + (clamped / 1.5), 3)


def transcribe_chunk_whisper(model, chunk_path: str) -> List[dict]:
    result = model.transcribe(chunk_path, word_timestamps=True, verbose=False)
    return result.get("segments", [])


_model_cache: dict = {}


def _get_whisper_model(model_size: str):
    """
    Cache loaded models per process. In a Celery worker this means the
    (slow, memory-heavy) model load happens once per worker process, not
    once per task -- subsequent tasks on the same worker reuse it.
    """
    if model_size not in _model_cache:
        import whisper
        _model_cache[model_size] = whisper.load_model(model_size)
    return _model_cache[model_size]


def process_audio_file(audio_path: str, model_size: str = "base") -> dict:
    """
    Run the full chunked-transcription pipeline on a single audio file
    and return a plain dict (JSON-serializable) with full_text, segments,
    and metadata. Raises TranscriptionError if the file can't be loaded
    or produces no usable output at all.
    """
    try:
        audio = AudioSegment.from_file(audio_path)
    except Exception as e:
        raise TranscriptionError(f"could not load audio file: {e}") from e

    boundaries = find_chunk_boundaries(audio)
    model = _get_whisper_model(model_size)

    all_segments: List[Segment] = []

    with tempfile.TemporaryDirectory() as work_dir:
        for i, (start_ms, end_ms) in enumerate(boundaries):
            chunk = audio[start_ms:end_ms]
            chunk_path = os.path.join(work_dir, f"chunk_{i}.wav")
            chunk.export(chunk_path, format="wav")

            try:
                raw_segments = transcribe_chunk_whisper(model, chunk_path)
            except Exception as e:
                # a single chunk failing shouldn't take down the whole file --
                # record it as one broken segment spanning that chunk and continue
                all_segments.append(Segment(
                    start=round(start_ms / 1000.0, 2),
                    end=round(end_ms / 1000.0, 2),
                    text=f"[chunk failed: {e}]",
                    confidence=0.0,
                    broken=True,
                ))
                continue

            offset_sec = start_ms / 1000.0
            for seg in raw_segments:
                text = seg["text"].strip()
                confidence = logprob_to_confidence(seg.get("avg_logprob", -1.0))
                is_broken = (not text) or confidence < LOW_CONFIDENCE_THRESHOLD
                all_segments.append(Segment(
                    start=round(offset_sec + seg["start"], 2),
                    end=round(offset_sec + seg["end"], 2),
                    text=text if text else "[unintelligible/silent segment]",
                    confidence=confidence,
                    broken=is_broken,
                ))

    if not all_segments:
        raise TranscriptionError("no segments produced (silent or unreadable audio)")

    broken_count = sum(1 for s in all_segments if s.broken)
    return {
        "full_text": " ".join(s.text for s in all_segments if not s.broken),
        "segments": [asdict(s) for s in all_segments],
        "engine": f"whisper-{model_size}",
        "audio_duration_seconds": all_segments[-1].end,
        "segment_count": len(all_segments),
        "broken_segment_count": broken_count,
    }
