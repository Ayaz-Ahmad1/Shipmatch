"""Small helpers shared by every app."""
from __future__ import annotations

from typing import Any

from django.http import Http404

from .context import client_ip_var, request_id_var
from .models import AuditEvent, Organization


def audit(organization: Organization | None, action: str, obj: Any, actor=None, **data: Any) -> AuditEvent:
    """Write one audit row. Call this for every state change a client may ask about later."""
    return AuditEvent.objects.create(
        organization=organization,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        action=action,
        object_type=type(obj).__name__,
        object_id=str(getattr(obj, "pk", obj)),
        data=_jsonable(data),
        ip=client_ip_var.get(),
        request_id=request_id_var.get()[:40],
    )


def _jsonable(value):
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def orgs_for_user(user):
    """Organizations a user may access. Superusers see all."""
    if not getattr(user, "is_authenticated", False):
        return Organization.objects.none()
    if user.is_superuser:
        return Organization.objects.all()
    return Organization.objects.filter(memberships__user=user)


def current_org(request) -> Organization:
    """The organization the user is working in (switchable with ?org=<slug>)."""
    org = getattr(request, "org", None)
    if org is None:
        raise Http404("You are not a member of any organization yet. Ask an admin to invite you.")
    return org


def use_org(request, org: Organization) -> None:
    """Make an object's organization the current one (after access was checked).

    The two-factor middleware only checks the organization that was current when the request arrived, so the
    rule of the organization switched to here is checked again: a member without two-factor can't act in an
    organization that requires it by opening one of its objects from another organization."""
    request.org = org
    request.session["org"] = org.slug
    from django.core.exceptions import PermissionDenied

    from .permissions import mfa_missing

    if mfa_missing(getattr(request, "user", None), org):
        raise PermissionDenied(f"{org.name} requires two-factor authentication. Set it up under Security and "
                               "sign-in, then try again.")
