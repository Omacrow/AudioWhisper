from django.urls import path

from .views import StatusPageView, UploadPageView

urlpatterns = [
    path("", UploadPageView.as_view(), name="upload-page"),
    path("transcripts/<uuid:pk>/", StatusPageView.as_view(), name="status-page"),
]
