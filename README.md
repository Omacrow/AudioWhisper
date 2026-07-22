# Design Decisions



## 1. Transcriber as an abstract interface, not a hardcoded Whisper call

Rather than scattering `whisper.load_model()` / `model.transcribe()` calls
throughout the codebase, a `Transcriber` abstract base class defines a
single contract: any transcriber must implement `transcribe(audio_path)`
and return segments with start, end, text, and confidence. `WhisperTranscriber`
and `MockTranscriber` both implement that contract but do completely
different things internally.

Why this matters:
- **Swappability** — switching to AssemblyAI, Google STT, or AWS Transcribe
  later means writing one new class; nothing else in the pipeline changes.
- **Testability** — the entire pipeline (validation, conversion, cleanup,
  JSON output) can be tested using `MockTranscriber`, without loading a
  multi-hundred-MB model or needing a GPU.
- **Decoupling** — high-level code (the pipeline) depends on an abstraction,
  not a concrete implementation (dependency inversion).

## 2. Whisper as the engine, with a derived (not native) confidence score

OpenAI Whisper was chosen as the actual engine: open-source, runs locally,
no API key/network dependency once weights are downloaded, and gives
segment/word-level timestamps out of the box.

Whisper doesn't expose a calibrated 0-1 confidence score -- only
`avg_logprob`, a negative log-probability. This is converted into a 0-1
score via a documented heuristic linear mapping (`logprob_to_confidence`),
clamped to a reasonable range. This is explicitly *not* a statistically
calibrated probability  it's the closest signal Whisper exposes, treated
honestly as an approximation rather than pretending it's more precise
than it is.

## 3. Explicit audio preprocessing, not relying on the model to handle it

Every input file is validated (exists, non-empty, supported extension)
*before* any processing, so bad input fails immediately with a clear
error rather than being passed to ffmpeg or the model and failing
unpredictably later.

All audio is normalized to 16kHz mono PCM via ffmpeg -- the format Whisper
expects internally -- regardless of the original format, sample rate, or
channel count. If the input is already in the right format (checked via
Python's `wave` module), the conversion step is skipped entirely to avoid
wasted work. This matters because feeding the model audio in the "wrong"
format doesn't necessarily error -- it can silently produce worse
transcription quality, so normalizing explicitly is a deliberate choice,
not a formatting nicety.

## 4. Chunking long audio at silence, not fixed intervals

Long audio is split into chunks at detected silence points
(`pydub.silence.detect_nonsilent`) rather than at arbitrary fixed-time
intervals, so cuts land in natural pauses instead of slicing through the
middle of a word. A hard cap (30 seconds) forces a split even during
continuous speech with no real pauses, so one long uninterrupted stretch
can't become a single giant unsplit chunk. Padding (`KEEP_SILENCE_MS`) is
kept around each chunk's edges so a slightly-off cut doesn't clip the
start/end of a word.

Chunks are processed **sequentially, one at a time**, with each chunk's
result printed/available as soon as it's ready -- giving progressive
output instead of making the caller wait for the entire file to finish.

## 5. Timestamps are corrected to absolute time, per segment

Whisper reports timestamps relative to whatever chunk it was given, not
the original file. Each chunk's start position (`offset_sec`) is added
back onto its segments' timestamps, so the final output has correct
absolute timestamps across the whole file -- without this, every chunk
after the first would report timestamps restarting near zero. Keeping
timestamps at the segment level (not just one timestamp for the whole
file) is what makes the output usable for subtitles, jumping to a
specific moment in the audio, or flagging exactly where a problem
occurred.

## 6. Broken/low-confidence segments are flagged and isolated, not hidden

A segment is marked `broken` if its text came back empty or its
confidence fell below a threshold. If transcribing a single chunk throws
an exception outright, that failure is recorded as one flagged segment
spanning that chunk (with the error message included) and the rest of
the file continues processing -- a bad 30-second chunk in a 10-minute
file doesn't take down the other 9.5 minutes. The final `full_text`
excludes broken segments (so the "clean" transcript isn't polluted by
failure placeholders), but every segment -- broken or not -- is still
preserved individually in the segment list so a caller can see exactly
what happened and where.

## 7. Mock backend for testing without a model download

`MockTranscriber` implements the same interface as the real Whisper
backend, deriving realistic segment timing from the audio's actual
duration rather than returning arbitrary fake data. This lets the entire
pipeline -- validation, chunking, timestamp correction, JSON output -- be
exercised and verified without a model download, GPU, or long inference
time, which mattered directly for testing this system in an environment
with restricted network access.

## 8. Stage-tagged error handling

Errors are caught and re-raised with an explicit stage attached
(`preprocess`, `transcribe`, `unexpected`) rather than collapsing
everything into a generic exception. A caller -- a web handler, a queue
worker -- needs to know *which* stage failed to decide whether to retry,
reject, or alert, so the failure mode is surfaced distinctly rather than
hidden behind one catch-all error type.

## 9. Concurrent uploads: accept instantly, process in the background

Uploads are never transcribed inline in the request/response cycle.
The file is accepted, saved, and a job record is created with status
`pending` -- the actual transcription work is handed off to a background
task queue (Celery + Redis as the broker). This means the web server
never does CPU/GPU-heavy work itself, and concurrent uploads scale by
adding more worker processes, not more web servers.

## 10. Storage: object storage for audio, relational DB for transcripts

**Audio** is large and immutable once uploaded -- it doesn't belong in the
database or on a single app server's local disk (neither durable nor
scalable past one machine). Object storage (S3/GCS) is the right fit:
audio just needs a stable key/URL, not to be queried.

**Transcripts** fit a relational database: status, engine used, duration,
and timestamps are naturally structured columns, while the segment list
(start/end/text/confidence) is stored as a JSON field since it's
read-mostly and doesn't need per-segment querying at this scope. If
searching *within* segment text became a requirement, that would be
added via Postgres full-text search or a dedicated index like
Elasticsearch, rather than restructuring the core schema.

## 11. Retry and recovery: explicit status, bounded retries, no silent failure

Each job's status is tracked explicitly through
`pending -> processing -> done / failed`. On failure, the specific error
and stage are logged -- the chunk-level segmentation also helps pinpoint
exactly which part of the audio failed, with a timestamp. Celery's
built-in retry mechanism retries automatically with exponential backoff,
up to a max count (3 attempts). After that, the job is marked `failed`
permanently with the error preserved, rather than retrying forever or
failing silently. A retried job reuses the same job ID and overwrites
its own result (`update_or_create`) rather than creating a duplicate
transcript record.

## 12. Exposing this as an API

Django REST Framework endpoints:
- `POST /api/transcripts/` -- upload a file, returns immediately with a
  job ID and `status: "pending"`.
- `GET /api/transcripts/{id}/` -- returns current status, and once
  `status: "done"`, the full JSON transcript (text, segments,
  timestamps, confidence).

This shape is deliberately frontend-agnostic -- any client can upload and
poll without knowing anything about Whisper, Celery, or chunking.

## 13. Why Django (not a lighter framework like FastAPI)

The core value of this system beyond "call Whisper" is job/status
tracking with retry visibility -- Django's admin panel gives that almost
for free, alongside a mature ORM and migrations that fit a job-tracking
schema well. The actual heavy work (transcription) already happens in
Celery workers, not in the request/response cycle, so the web layer's
job is just "save file, enqueue, return 202" and "look up status" -- not
something that benefits much from an async-native framework. FastAPI
would be the better call for a high-throughput, stateless API with no
admin/ops need; that's not what this is.

## 14. Why Postgres (not MongoDB or SQLite)

The actual data is fundamentally relational: jobs, statuses, and foreign
keys between an upload and its transcript, with one field (the segment
list) that's semi-structured. Postgres's `JSONField`/JSONB handles that
one flexible field without needing to abandon relational modeling for
everything else. MongoDB would make more sense if the *entire* record's
shape were unpredictable -- it isn't, since every engine's output is
normalized into the same `{start, end, text, confidence, broken}` shape
before it's ever stored. SQLite is fine for local dev/testing, but its
single-writer lock is a poor fit for concurrent Celery workers updating
job status at the same time -- exactly the concurrency scenario this
system needs to handle in production.