from __future__ import annotations

from decimal import Decimal

from django import forms
from django.forms import inlineformset_factory

from apps.accounting.models import vendor_key

from . import charges, lanes
from .models import ApprovedAccessorial, Quote, QuoteCharge, RateSettings


def _currency(value: str) -> str:
    cur = (value or "").strip().upper()
    if len(cur) != 3 or not cur.isalpha():
        raise forms.ValidationError("Use a three-letter currency code such as USD.")
    return cur


def _vendor(value: str) -> str:
    name = (value or "").strip()
    if not vendor_key(name):
        raise forms.ValidationError("Enter the vendor's name as it appears on its invoices.")
    return name


class DateInput(forms.DateInput):
    input_type = "date"

    def __init__(self, **kwargs):
        super().__init__(format="%Y-%m-%d", **kwargs)


class QuoteForm(forms.ModelForm):
    class Meta:
        model = Quote
        fields = ["vendor_name", "reference", "origin", "destination", "equipment", "valid_from", "valid_to",
                  "currency", "all_in", "notes"]
        widgets = {
            "vendor_name": forms.TextInput(attrs={"list": "vendor-names", "autocomplete": "off", "class": "w-full"}),
            "reference": forms.TextInput(attrs={"class": "w-full"}),
            "origin": forms.TextInput(attrs={"list": "port-names", "autocomplete": "off", "class": "w-full"}),
            "destination": forms.TextInput(attrs={"list": "port-names", "autocomplete": "off", "class": "w-full"}),
            "valid_from": DateInput(),
            "valid_to": DateInput(),
            "currency": forms.TextInput(attrs={"maxlength": 3, "size": 5}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }
        labels = {"vendor_name": "Vendor", "reference": "Quote reference", "origin": "Origin (port of loading)",
                  "destination": "Destination (port of discharge)", "all_in": "All-in rate"}

    def clean_vendor_name(self):
        return _vendor(self.cleaned_data.get("vendor_name"))

    def clean_currency(self):
        return _currency(self.cleaned_data.get("currency"))

    def clean(self):
        data = super().clean()
        start, end = data.get("valid_from"), data.get("valid_to")
        if start and end and end < start:
            self.add_error("valid_to", "The end date is before the start date.")
        return data


class ChargeForm(forms.ModelForm):
    class Meta:
        model = QuoteCharge
        fields = ["code", "description", "amount", "basis"]
        widgets = {"description": forms.TextInput(attrs={"placeholder": "As the vendor writes it (optional)"}),
                   "amount": forms.NumberInput(attrs={"step": "0.01", "min": "0", "class": "num"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["code"].choices = [("", "Choose a charge")] + charges.CODE_CHOICES

    def clean_amount(self):
        amount = self.cleaned_data.get("amount")
        if amount is not None and amount < 0:
            raise forms.ValidationError("Use a positive amount. Enter discounts as a lower rate.")
        return amount


ChargeFormSet = inlineformset_factory(Quote, QuoteCharge, form=ChargeForm, extra=3, can_delete=True,
                                      min_num=1, validate_min=True, max_num=60)


class AccessorialForm(forms.ModelForm):
    class Meta:
        model = ApprovedAccessorial
        fields = ["vendor_name", "code", "unit", "free_units", "max_per_unit", "max_amount", "currency",
                  "valid_from", "valid_to", "notes"]
        widgets = {
            "vendor_name": forms.TextInput(attrs={"list": "vendor-names", "autocomplete": "off", "class": "w-full"}),
            "free_units": forms.NumberInput(attrs={"min": "0", "step": "1"}),
            "max_per_unit": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "max_amount": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "currency": forms.TextInput(attrs={"maxlength": 3, "size": 5}),
            "valid_from": DateInput(),
            "valid_to": DateInput(),
            "notes": forms.TextInput(attrs={"class": "w-full", "placeholder": "Where this was agreed, e.g. contract clause 4.2"}),
        }
        labels = {"vendor_name": "Vendor", "code": "Extra charge", "unit": "Charged", "free_units": "Free time",
                  "max_per_unit": "Most per day, hour or time", "max_amount": "Most per invoice"}

    def clean_vendor_name(self):
        return _vendor(self.cleaned_data.get("vendor_name"))

    def clean_currency(self):
        return _currency(self.cleaned_data.get("currency"))

    def clean(self):
        data = super().clean()
        for name in ("max_per_unit", "max_amount"):
            if data.get(name) is not None and data[name] < 0:
                self.add_error(name, "Use a positive amount, or leave it empty for no cap.")
        if data.get("valid_from") and data.get("valid_to") and data["valid_to"] < data["valid_from"]:
            self.add_error("valid_to", "The end date is before the start date.")
        return data


class RateSettingsForm(forms.ModelForm):
    class Meta:
        model = RateSettings
        fields = ["tolerance_percent", "tolerance_amount", "warn_no_quote", "check_unlisted_vendors", "ai_classify"]
        widgets = {"tolerance_percent": forms.NumberInput(attrs={"step": "0.1", "min": "0", "max": "50"}),
                   "tolerance_amount": forms.NumberInput(attrs={"step": "0.01", "min": "0"})}

    def clean_tolerance_percent(self):
        v = self.cleaned_data.get("tolerance_percent")
        if v is None or not Decimal("0") <= v <= Decimal("50"):
            raise forms.ValidationError("Use a percentage from 0 to 50.")
        return v

    def clean_tolerance_amount(self):
        v = self.cleaned_data.get("tolerance_amount")
        if v is None or v < 0:
            raise forms.ValidationError("Use zero or a positive amount.")
        return v


class AliasForm(forms.Form):
    example = forms.CharField(max_length=200)
    code = forms.ChoiceField(choices=charges.CODE_CHOICES)

    def clean_example(self):
        text = self.cleaned_data["example"].strip()
        if not charges.alias_key(text):
            raise forms.ValidationError("Enter the charge name as it appears on the invoice, with words in it.")
        return text


EQUIPMENT_FILTER = [(c, label) for c, label in lanes.EQUIPMENT_CHOICES if c]
