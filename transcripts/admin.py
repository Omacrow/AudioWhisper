from django.contrib import admin

from .models import AudioUpload, Transcript


class TranscriptInline(admin.StackedInline):
    model = Transcript
    extra = 0
    readonly_fields = ["full_text", "segments", "engine", "audio_duration_seconds",
                        "segment_count", "broken_segment_count", "created_at"]


@admin.register(AudioUpload)
class AudioUploadAdmin(admin.ModelAdmin):
    list_display = ["id", "original_filename", "status", "retry_count", "created_at", "updated_at"]
    list_filter = ["status", "model_size"]
    search_fields = ["original_filename", "id"]
    readonly_fields = ["id", "created_at", "updated_at"]
    inlines = [TranscriptInline]


@admin.register(Transcript)
class TranscriptAdmin(admin.ModelAdmin):
    list_display = ["audio_upload", "engine", "segment_count", "broken_segment_count", "created_at"]
    readonly_fields = ["audio_upload", "full_text", "segments", "engine",
                       "audio_duration_seconds", "segment_count", "broken_segment_count", "created_at"]
