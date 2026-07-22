from django import forms

from .models import AudioUpload

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

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.original_filename = instance.file.name
        if commit:
            instance.save()
        return instance
