
from __future__ import annotations
import argparse
import json
import os
import sys
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


def find_chunk_boundaries(audio: AudioSegment) -> List[tuple]:
    """
    Return a list of (start_ms, end_ms) chunk boundaries, cutting at
    detected silence rather than at arbitrary fixed intervals -- this
    avoids slicing through the middle of a word.
    """
    nonsilent_ranges = detect_nonsilent(
        audio,
        min_silence_len=MIN_SILENCE_LEN_MS,
        silence_thresh=SILENCE_THRESH_DB,
    )

    if not nonsilent_ranges:
        # entire file is "silent" by this threshold -- treat as one chunk
        # rather than producing zero chunks
        return [(0, len(audio))]

    boundaries = []
    for start, end in nonsilent_ranges:
        start = max(0, start - KEEP_SILENCE_MS)
        end = min(len(audio), end + KEEP_SILENCE_MS)

        # further split any single non-silent stretch that's still too
        # long (e.g. continuous speech with no real pauses)
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


def transcribe_chunk_mock(chunk_path: str, chunk_index: int) -> List[dict]:
    """Fake per-chunk output for testing without a model download."""
    import wave
    with wave.open(chunk_path, "rb") as f:
        duration = f.getnframes() / float(f.getframerate())
    # simulate an occasional broken/empty segment, since real ASR
    # sometimes fails on a pure-silence or noise-only chunk
    if chunk_index % 4 == 3:
        return [{"start": 0.0, "end": duration, "text": "", "avg_logprob": -2.0}]
    return [{
        "start": 0.0,
        "end": duration,
        "text": f"mock transcribed text for chunk {chunk_index}",
        "avg_logprob": -0.3,
    }]


def process_audio(audio_path: str, use_mock: bool, model_size: str) -> List[Segment]:
    audio = AudioSegment.from_file(audio_path)
    boundaries = find_chunk_boundaries(audio)
    print(f"Split into {len(boundaries)} chunk(s) based on detected silence.")

    model = None
    if not use_mock:
        import whisper
        model = whisper.load_model(model_size)

    all_segments: List[Segment] = []

    with tempfile.TemporaryDirectory() as work_dir:
        for i, (start_ms, end_ms) in enumerate(boundaries):
            chunk = audio[start_ms:end_ms]
            chunk_path = os.path.join(work_dir, f"chunk_{i}.wav")
            chunk.export(chunk_path, format="wav")

            if use_mock:
                raw_segments = transcribe_chunk_mock(chunk_path, i)
            else:
                raw_segments = transcribe_chunk_whisper(model, chunk_path)

            offset_sec = start_ms / 1000.0

            for seg in raw_segments:
                text = seg["text"].strip()
                confidence = logprob_to_confidence(seg.get("avg_logprob", -1.0))
                is_broken = (not text) or confidence < LOW_CONFIDENCE_THRESHOLD

                segment = Segment(
                    start=round(offset_sec + seg["start"], 2),
                    end=round(offset_sec + seg["end"], 2),
                    text=text if text else "[unintelligible/silent segment]",
                    confidence=confidence,
                    broken=is_broken,
                )
                all_segments.append(segment)

                # print progressively, per chunk, rather than waiting for
                # the whole file -- this is the "instant output" behavior
                flag = " <-- FLAGGED (low confidence / empty)" if is_broken else ""
                print(f"[{segment.start:>7.2f}s - {segment.end:>7.2f}s] {segment.text}{flag}")

    return all_segments


def main():
    parser = argparse.ArgumentParser(description="Chunked transcription for long audio files")
    parser.add_argument("audio_path")
    parser.add_argument("--model", default="base",
                         choices=["tiny", "base", "small", "medium", "large"])
    parser.add_argument("--mock", action="store_true",
                         help="Use fake output instead of loading a real Whisper model")
    parser.add_argument(
        "--output", "-o", default=None,
        help="Output JSON path. If omitted, defaults to <audio filename>.json "
             "(e.g. audio1.wav -> audio1.json) written next to this script.",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.audio_path):
        print(f"ERROR: file not found: {args.audio_path}", file=sys.stderr)
        return 1

    # Derive a default output filename from the input audio filename if the
    # user didn't specify one, e.g. "audio1.wav" -> "audio1.json".
    if args.output:
        output_path = args.output
    else:
        base_name = os.path.splitext(os.path.basename(args.audio_path))[0]
        output_path = f"{base_name}.json"

    segments = process_audio(args.audio_path, use_mock=args.mock, model_size=args.model)

    broken_count = sum(1 for s in segments if s.broken)
    result = {
        "source_file": os.path.basename(args.audio_path),
        "full_text": " ".join(s.text for s in segments if not s.broken),
        "segments": [asdict(s) for s in segments],
        "metadata": {
            "engine": "mock" if args.mock else f"whisper-{args.model}",
            "segment_count": len(segments),
            "broken_segment_count": broken_count,
            "audio_duration_seconds": segments[-1].end if segments else 0,
        },
    }

    print(f"\nDone. {len(segments)} segments, {broken_count} flagged as broken/low-confidence.")

    # Always write the JSON file -- this is the full transcript with
    # timestamps, confidence, and broken-segment flags per segment.
    with open(output_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"Full JSON written to {output_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())