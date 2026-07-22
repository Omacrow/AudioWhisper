from django.urls import path

from .views import AudioUploadCreateView, AudioUploadStatusView

urlpatterns = [
    path("transcripts/", AudioUploadCreateView.as_view(), name="transcript-create"),
    path("transcripts/<uuid:pk>/", AudioUploadStatusView.as_view(), name="transcript-status"),
]
