from django import forms
from .models import SiteSetting

class SiteSettingForm(forms.ModelForm):
    class Meta:
        model = SiteSetting
        fields = ["brand_name", "brand_logo"]
        widgets = {"brand_name": forms.TextInput(attrs={"class": "form-control"})}
