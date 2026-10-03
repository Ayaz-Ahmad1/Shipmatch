"""Reviewer corrections: edit a field, then re-match and re-validate what it affects."""
from __future__ import annotations

from django.db import transaction

from apps.core.utils import audit
from apps.documents.models import Document, ExtractedField
from apps.documents.schemas import SCHEMAS, TABLE_FIELDS
from apps.documents.services.locate import safe_locate

LIST_FIELDS = {"container_numbers", "po_numbers"}
KEY_FIELDS = {"bl_number", "container_numbers", "po_numbers", "entry_number"}   # entry number: apps/customs matching
EDITABLE = {name for schema in SCHEMAS.values() for name in schema.model_fields} - set(TABLE_FIELDS)


def parse_input(name: str, raw: str):
    raw = (raw or "").strip()
    if name in LIST_FIELDS:
        return [p.strip().upper().replace(" ", "") for p in raw.replace(";", ",").split(",") if p.strip()]
    return raw or None


@transaction.atomic
def correct_field(doc: Document, name: str, raw_value: str, user) -> ExtractedField | None:
    """Save a reviewer's value. Returns None when the value did not change (nothing is recorded)."""
    if name not in EDITABLE:
        raise ValueError(f"{name} cannot be edited here")
    value = parse_input(name, raw_value)
    field = ExtractedField.objects.filter(document=doc, name=name).first()
    if field is not None and field.value == value:
        return None
    if field is None and value in (None, []):
        return None
    field = field or ExtractedField(document=doc, name=name)
    old = field.value
    field.value, field.confidence, field.source = value, 1.0, ExtractedField.Source.HUMAN
    field.save()
    audit(doc.organization, "field.corrected", doc, actor=user, field=name, old=old, new=value)
    safe_locate(doc)  # find the new value on the page; a typed value that isn't printed gets no box
    field.refresh_from_db(fields=["location", "page"])
    return field


def after_correction(doc: Document, name: str) -> None:
    from apps.shipments.services.matching import match_document, refresh_keys
    from apps.shipments.services.validation import validate_shipment

    old_shipment = doc.match.shipment if hasattr(doc, "match") else None
    if name in KEY_FIELDS:
        new_shipment = match_document(doc)
        if old_shipment and (not new_shipment or old_shipment.pk != new_shipment.pk):
            if old_shipment.links.exists():
                refresh_keys(old_shipment)
                validate_shipment(old_shipment)
            else:
                old_shipment.delete()
    doc.refresh_from_db()
    if hasattr(doc, "match"):
        validate_shipment(doc.match.shipment)
