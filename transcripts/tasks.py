import logging

from celery import shared_task
from django.utils import timezone

from .models import AudioUpload, Transcript
from .transcription_core import PreprocessError, TranscriptionError, process_audio_file

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 30  # base delay; Celery's retry_backoff multiplies this per attempt


@shared_task(bind=True, max_retries=MAX_RETRIES, ignore_result=True)
def transcribe_audio_task(self, audio_upload_id: str):
    """
    Background job: load the AudioUpload, run it through the chunked
    transcription pipeline, and persist a Transcript on success.

    Retry behavior:
    - Transient failures (e.g. a brief storage read error) get retried
      automatically with exponential backoff, up to MAX_RETRIES.
    - Each attempt increments retry_count on the record so it's visible
      via the API/admin, not just in worker logs.
    - After MAX_RETRIES is exhausted, the job is marked 'failed'
      permanently with the error stored -- it does not retry forever,
      and it does not fail silently.
    - This task is safe to re-run for the same audio_upload_id: it always
      overwrites any existing Transcript for that upload rather than
      creating a duplicate, so a manual retry or Celery's own retry
      doesn't leave stale/duplicate data behind.
    """
    try:
        upload = AudioUpload.objects.get(id=audio_upload_id)
    except AudioUpload.DoesNotExist:
        logger.error("AudioUpload %s not found -- cannot process", audio_upload_id)
        return

    upload.status = AudioUpload.STATUS_PROCESSING
    upload.save(update_fields=["status", "updated_at"])

    try:
        result = process_audio_file(upload.file.path, model_size=upload.model_size)
    except PreprocessError as e:
        return _handle_failure(self, upload, stage="preprocess", message=str(e))
    except TranscriptionError as e:
        return _handle_failure(self, upload, stage="transcribe", message=str(e))
    except Exception as e:
        # anything unexpected (storage error, OOM, etc.) -- still caught
        # and recorded, never left as an unhandled worker crash
        return _handle_failure(self, upload, stage="unexpected", message=str(e))

    Transcript.objects.update_or_create(
        audio_upload=upload,
        defaults={
            "full_text": result["full_text"],
            "segments": result["segments"],
            "engine": result["engine"],
            "audio_duration_seconds": result["audio_duration_seconds"],
            "segment_count": result["segment_count"],
            "broken_segment_count": result["broken_segment_count"],
        },
    )

    upload.status = AudioUpload.STATUS_DONE
    upload.error_stage = ""
    upload.error_message = ""
    upload.save(update_fields=["status", "error_stage", "error_message", "updated_at"])
    logger.info("Transcription done for %s", audio_upload_id)


def _handle_failure(task, upload: AudioUpload, stage: str, message: str):
    upload.retry_count += 1
    upload.error_stage = stage
    upload.error_message = message
    upload.save(update_fields=["retry_count", "error_stage", "error_message", "updated_at"])

    if task.request.retries < task.max_retries:
        logger.warning(
            "Transcription failed for %s at stage '%s' (attempt %d/%d), retrying: %s",
            upload.id, stage, task.request.retries + 1, task.max_retries + 1, message,
        )
        raise task.retry(countdown=RETRY_BACKOFF_SECONDS * (2 ** task.request.retries))

    upload.status = AudioUpload.STATUS_FAILED
    upload.save(update_fields=["status", "updated_at"])
    logger.error(
        "Transcription permanently failed for %s at stage '%s' after %d attempts: %s",
        upload.id, stage, upload.retry_count, message,
    )
