import os

from django import forms

from .models import AudioUpload
from .transcription_core import SUPPORTED_EXTENSIONS

MODEL_SIZE_CHOICES = [
    ("tiny", "Tiny (fastest, least accurate)"),
    ("base", "Base"),
    ("small", "Small"),
    ("medium", "Medium"),
    ("large", "Large (slowest, most accurate)"),
]


class AudioUploadForm(forms.ModelForm):
    model_size = forms.ChoiceField(choices=MODEL_SIZE_CHOICES, initial="base")

    class Meta:
        model = AudioUpload
        fields = ["file", "model_size"]
        widgets = {
            "file": forms.ClearableFileInput(
                attrs={"accept": ",".join(sorted(SUPPORTED_EXTENSIONS))}
            ),
        }

    def clean_file(self):
        file = self.cleaned_data["file"]
        ext = os.path.splitext(file.name)[1].lower()
        if ext not in SUPPORTED_EXTENSIONS:
            raise forms.ValidationError(
                f"Unsupported file extension '{ext}'. Expected one of: "
                f"{', '.join(sorted(SUPPORTED_EXTENSIONS))}"
            )
        return file

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.original_filename = instance.file.name
        if commit:
            instance.save()
        return instance
