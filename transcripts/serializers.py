import os

from rest_framework import serializers

from .models import AudioUpload, Transcript
from .transcription_core import SUPPORTED_EXTENSIONS


class AudioUploadCreateSerializer(serializers.ModelSerializer):
    """Used for POST /transcripts/ -- accepts the file, nothing else required."""

    class Meta:
        model = AudioUpload
        fields = ["id", "file", "model_size", "status", "created_at"]
        read_only_fields = ["id", "status", "created_at"]

    def validate_file(self, value):
        ext = os.path.splitext(value.name)[1].lower()
        if ext not in SUPPORTED_EXTENSIONS:
            raise serializers.ValidationError(
                f"Unsupported file extension '{ext}'. Expected one of: "
                f"{', '.join(sorted(SUPPORTED_EXTENSIONS))}"
            )
        return value

    def create(self, validated_data):
        validated_data["original_filename"] = validated_data["file"].name
        return super().create(validated_data)


class TranscriptSerializer(serializers.ModelSerializer):
    class Meta:
        model = Transcript
        fields = [
            "full_text", "segments", "engine",
            "audio_duration_seconds", "segment_count", "broken_segment_count",
        ]


class AudioUploadStatusSerializer(serializers.ModelSerializer):
    """Used for GET /transcripts/{id}/ -- status plus the transcript once done."""

    transcript = TranscriptSerializer(read_only=True)

    class Meta:
        model = AudioUpload
        fields = [
            "id", "original_filename", "status", "model_size",
            "error_stage", "error_message", "retry_count",
            "created_at", "updated_at", "transcript",
        ]
