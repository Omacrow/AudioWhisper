from django.urls import reverse_lazy
from django.views.generic import CreateView, DetailView
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .forms import AudioUploadForm
from .models import AudioUpload
from .serializers import AudioUploadCreateSerializer, AudioUploadStatusSerializer
from .tasks import transcribe_audio_task


class AudioUploadCreateView(APIView):
    """
    POST /api/transcripts/

    Accepts a multipart file upload. Saves it, creates a job record with
    status='pending', and enqueues the background transcription task --
    then returns immediately with the job id, rather than blocking the
    request on however long transcription takes.
    """

    def post(self, request):
        serializer = AudioUploadCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        upload = serializer.save()

        # .delay() hands this off to a Celery worker over the message
        # broker -- this call returns immediately, it does not run
        # transcription inline in the request.
        transcribe_audio_task.delay(str(upload.id))

        return Response(
            AudioUploadCreateSerializer(upload).data,
            status=status.HTTP_202_ACCEPTED,
        )


class AudioUploadStatusView(APIView):
    """
    GET /api/transcripts/{id}/

    Poll this for job status. Once status='done', the nested
    'transcript' field contains the full result (text, segments,
    timestamps, confidence). If status='failed', error_stage and
    error_message explain what went wrong.
    """

    def get(self, request, pk):
        try:
            upload = AudioUpload.objects.select_related("transcript").get(pk=pk)
        except AudioUpload.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        return Response(AudioUploadStatusSerializer(upload).data)


class UploadPageView(CreateView):
    """
    GET/POST /  -- server-rendered upload form. On success, enqueues the
    same Celery task the JSON API uses and redirects to the status page
    for that job.
    """

    model = AudioUpload
    form_class = AudioUploadForm
    template_name = "transcripts/upload.html"

    def form_valid(self, form):
        response = super().form_valid(form)
        transcribe_audio_task.delay(str(self.object.id))
        return response

    def get_success_url(self):
        return reverse_lazy("status-page", kwargs={"pk": self.object.pk})


class StatusPageView(DetailView):
    """
    GET /transcripts/<uuid>/  -- status page. Renders the job's current
    state server-side (works with no JS), and client-side JS polls the
    JSON status API to update in place while pending/processing.
    """

    model = AudioUpload
    template_name = "transcripts/status.html"
    context_object_name = "upload"

    def get_queryset(self):
        return AudioUpload.objects.select_related("transcript")
