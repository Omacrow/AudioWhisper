# Design Decisions


## How would you handle concurrent uploads?

Accept the upload instantly and hand it off to a background worker
(Celery + Redis), assigning the job status as `"pending"`. This way,
multiple uploads just become multiple queued jobs rather than blocking
the web server from handling other requests. The web server never does
CPU/GPU-heavy transcription work itself — that's handed off entirely to
Celery workers, which means concurrent uploads scale by adding more
worker processes, not by adding more web servers.

## How would you store audio and transcripts?

*Transcripts*: stored in a relational database, with `start`, `end`,
`text`, `confidence`, and `engine` (which Whisper model size was used)
fitting well as structured fields — with the segment list itself as a
JSON field, since it's naturally list-shaped and doesn't need to be
queried row-by-row. If searching *within* segment text became a
requirement later, that would be added via Postgres full-text search or
a dedicated search index like Elasticsearch, rather than restructuring
the core schema.

*Audio*: audio files can be huge and are immutable once uploaded.
Storing large binary blobs directly in a database hurts performance and
scalability (bloated backups, slower queries, harder replication), so an
object storage service such as Amazon S3 is the better fit — audio isn't
kept on local disk or in the database at all in a production setup.

## How do you retry or recover failed transcriptions?

Each job's status is tracked explicitly through `pending → processing →
done / failed`. On failure, the specific error is logged, and the
per-chunk segmentation also helps pinpoint exactly which part of the
audio the model failed on, along with a timestamp. Celery's built-in
retry mechanism is used to retry a job automatically up to a max count
(3 attempts); if it still fails after that, the job is marked `failed`
permanently, with the error message and stage recorded rather than
retrying forever or failing silently. Retries are given the same job ID
rather than creating a new one, so a retried job overwrites its own
result instead of producing duplicate transcript records.

## How would you expose this as an API?

Django (with Django REST Framework) makes setting up API endpoints
straightforward:
- `POST /api/transcripts/` — upload an audio file, returns immediately
  with a job ID and `status: "pending"`.
- `GET /api/transcripts/{id}/` — returns current status, and once
  `status: "done"`, the full JSON transcript (text, segments,
  timestamps, confidence).

This shape is intentionally frontend-agnostic — any client (web app,
mobile app, another backend service) can upload and poll without needing
to know anything about Whisper, Celery, or how transcription actually
happens under the hood.

## Why Django + Postgres (not something lighter, or MongoDB)

Django was chosen because the core value of this system isn't just
"call Whisper" — it's job/status tracking with retry visibility, and
Django's admin panel gives that almost for free, alongside a mature ORM
and migrations that fit a job-tracking schema well.

Postgres was chosen over MongoDB or sqlite because the actual data is
fundamentally relational: jobs, statuses, and foreign keys between an
upload and its transcript, with just one field (the segment list) that's
semi-structured. Postgres's `JSONField` handles that one flexible field
fine without needing to abandon relational modeling for everything else.
MongoDB would make more sense if the entire record's shape were
unpredictable — it isn't, since every engine's output is normalized into
the same `{start, end, text, confidence, broken}` shape before storage.
Sqlite works for local dev/testing but its single-writer lock is a poor
fit for concurrent Celery workers updating job status at the same time,
which is exactly the concurrency scenario this system needs to handle.