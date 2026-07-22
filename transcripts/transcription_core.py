from __future__ import annotations
import os
import tempfile
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import List, Optional
from pydub import AudioSegment
from pydub.silence import detect_nonsilent

MIN_SILENCE_LEN_MS = 500
SILENCE_THRESH_DB = -40
KEEP_SILENCE_MS = 200
MAX_CHUNK_MS = 30_000
LOW_CONFIDENCE_THRESHOLD = 0.55

SUPPORTED_EXTENSIONS = {
    ".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".wma", ".opus", ".webm",
}


@dataclass
class Segment:
    start: float
    end: float
    text: str
    confidence: float
    broken: bool = False


class TranscriptionError(Exception):
    """Raised when a chunk/file can't be transcribed at all (vs. just low confidence)."""


class PreprocessError(TranscriptionError):
    """Raised when the input file fails validation before any transcription is attempted."""


def validate_audio_file(audio_path: str) -> None:
    """
    Cheap checks run before the file is handed to ffmpeg/Whisper, so bad
    input (missing, empty, wrong type) fails immediately with a clear
    error instead of failing unpredictably deep inside the pipeline.
    """
    if not os.path.isfile(audio_path):
        raise PreprocessError(f"file not found: {audio_path}")

    if os.path.getsize(audio_path) == 0:
        raise PreprocessError(f"file is empty: {audio_path}")

    ext = os.path.splitext(audio_path)[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise PreprocessError(
            f"unsupported file extension '{ext}' -- expected one of "
            f"{', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )


def find_chunk_boundaries(audio: AudioSegment) -> List[tuple]:
    nonsilent_ranges = detect_nonsilent(
        audio, 
        min_silence_len=MIN_SILENCE_LEN_MS, 
        silence_thresh=SILENCE_THRESH_DB,
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


class Transcriber(ABC):
    """
    Contract any transcription engine must implement: given the path to
    one audio chunk, return a list of {start, end, text, confidence}
    dicts for that chunk. Swapping engines (AssemblyAI, Google STT, AWS
    Transcribe, ...) means writing one new class -- nothing else in the
    pipeline changes.
    """

    engine_name: str

    @abstractmethod
    def transcribe(self, chunk_path: str) -> List[dict]:
        raise NotImplementedError


_model_cache: dict = {}


class WhisperTranscriber(Transcriber):
    """Real engine: OpenAI Whisper, run locally."""

    def __init__(self, model_size: str):
        self.model_size = model_size
        self.engine_name = f"whisper-{model_size}"
        self._model = self._get_model(model_size)

    @staticmethod
    def _get_model(model_size: str):
        """
        Cache loaded models per process. In a Celery worker this means
        the (slow, memory-heavy) model load happens once per worker
        process, not once per task -- subsequent tasks on the same
        worker reuse it.
        """
        if model_size not in _model_cache:
            import whisper
            _model_cache[model_size] = whisper.load_model(model_size)
        return _model_cache[model_size]

    def transcribe(self, chunk_path: str) -> List[dict]:
        result = self._model.transcribe(chunk_path, word_timestamps=True, verbose=False)
        return [
            {
                "start": seg["start"],
                "end": seg["end"],
                "text": seg["text"],
                # Whisper doesn't expose a calibrated 0-1 confidence score,
                # only avg_logprob -- derive an approximation from it.
                "confidence": logprob_to_confidence(seg.get("avg_logprob", -1.0)),
            }
            for seg in result.get("segments", [])
        ]


class MockTranscriber(Transcriber):
    """
    Fake transcription for testing without a model download, GPU, or
    long inference time. Derives realistic segment timing from the
    chunk's actual duration rather than returning arbitrary fake data,
    and periodically simulates a broken/empty segment the way real ASR
    sometimes does on a silence- or noise-only chunk.
    """

    engine_name = "mock"

    def __init__(self):
        self._chunk_index = 0

    def transcribe(self, chunk_path: str) -> List[dict]:
        duration = len(AudioSegment.from_file(chunk_path)) / 1000.0
        i = self._chunk_index
        self._chunk_index += 1

        if i % 4 == 3:
            return [{"start": 0.0, "end": duration, "text": "", "confidence": 0.0}]
        return [{
            "start": 0.0,
            "end": duration,
            "text": f"mock transcribed text for chunk {i}",
            "confidence": 0.95,
        }]


def process_audio_file(
    audio_path: str,
    model_size: str = "base",
    transcriber: Optional[Transcriber] = None,
) -> dict:
    """
    Run the full chunked-transcription pipeline on a single audio file
    and return a plain dict (JSON-serializable) with full_text, segments,
    and metadata. Raises PreprocessError if the file fails basic
    validation, or TranscriptionError if it can't be loaded or produces
    no usable output at all.

    `transcriber` defaults to a real WhisperTranscriber for `model_size`.
    Pass a MockTranscriber() instead to exercise the rest of the pipeline
    (validation, chunking, timestamp correction, broken-segment handling)
    without a model download, GPU, or long inference time.
    """
    validate_audio_file(audio_path)

    try:
        audio = AudioSegment.from_file(audio_path)
    except Exception as e:
        raise TranscriptionError(f"could not load audio file: {e}") from e

    boundaries = find_chunk_boundaries(audio)
    if transcriber is None:
        transcriber = WhisperTranscriber(model_size)

    all_segments: List[Segment] = []

    with tempfile.TemporaryDirectory() as work_dir:
        for i, (start_ms, end_ms) in enumerate(boundaries):
            chunk = audio[start_ms:end_ms]
            chunk_path = os.path.join(work_dir, f"chunk_{i}.wav")
            chunk.export(chunk_path, format="wav")

            try:
                raw_segments = transcriber.transcribe(chunk_path)
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
                confidence = seg["confidence"]
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
        "engine": transcriber.engine_name,
        "audio_duration_seconds": all_segments[-1].end,
        "segment_count": len(all_segments),
        "broken_segment_count": broken_count,
    }
