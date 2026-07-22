import uuid

from django.db import models


class AudioUpload(models.Model):
   

    STATUS_PENDING = "pending"
    STATUS_PROCESSING = "processing"
    STATUS_DONE = "done"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_DONE, "Done"),
        (STATUS_FAILED, "Failed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    
    file = models.FileField(upload_to="audio/%Y/%m/%d/")
    original_filename = models.CharField(max_length=255)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    model_size = models.CharField(max_length=20, default="base")

    error_stage = models.CharField(max_length=50, blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    retry_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.original_filename} ({self.status})"


class Transcript(models.Model):
  

    audio_upload = models.OneToOneField(
        AudioUpload, on_delete=models.CASCADE, related_name="transcript"
    )
    full_text = models.TextField()
    segments = models.JSONField()  # list of {start, end, text, confidence, broken}
    engine = models.CharField(max_length=50)
    audio_duration_seconds = models.FloatField()
    segment_count = models.PositiveIntegerField()
    broken_segment_count = models.PositiveIntegerField()

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Transcript for {self.audio_upload_id}"
