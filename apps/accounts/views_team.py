"""Team management for organization admins: invite, change role or limit, remove, reset 2FA."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.views.decorators.http import require_POST

from apps.core.models import Membership
from apps.core.permissions import require
from apps.core.utils import audit, current_org

from .services import mfa


def _limit(raw: str) -> Decimal | None:
    raw = (raw or "").replace(",", "").strip()
    if not raw:
        return None
    try:
        value = Decimal(raw).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise ValueError("Approval limit must be a number, or empty for no limit.")
    if value < 0:
        raise ValueError("Approval limit can't be negative.")
    return value


def set_password_link(request, user) -> str:
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    return request.build_absolute_uri(reverse("accounts:password_reset_confirm", args=[uid, token]))


@login_required
def team(request):
    org = current_org(request)
    require(request.user, org, "manage")
    members = (Membership.objects.filter(organization=org).select_related("user", "user__profile")
               .order_by("user__first_name", "user__username"))
    return render(request, "team/list.html", {
        "members": members, "roles": Membership.Role.choices,
        "invite_link": request.session.pop("invite_link", None),
    })


@login_required
@require_POST
def invite(request):
    org = current_org(request)
    require(request.user, org, "manage")
    email = request.POST.get("email", "").strip().lower()
    name = request.POST.get("name", "").strip()
    role = request.POST.get("role", Membership.Role.REVIEWER)
    try:
        validate_email(email)
        if role not in Membership.Role.values:
            raise ValueError("Choose a role.")
        limit = _limit(request.POST.get("approval_limit", ""))
    except (ValidationError, ValueError) as e:
        messages.error(request, e.messages[0] if isinstance(e, ValidationError) else str(e))
        return redirect("core:team")
    from apps.billing.usage import seat_limit_message

    full = seat_limit_message(org)
    if full:
        messages.error(request, full)
        return redirect("core:team")

    User = get_user_model()
    with transaction.atomic():
        user = User.objects.filter(email__iexact=email).first() or User.objects.filter(username__iexact=email).first()
        created = user is None
        if created:
            first, _, last = name.partition(" ")
            user = User(username=email, email=email, first_name=first[:150], last_name=last[:150])
            user.set_unusable_password()
            user.save()
        membership, new_member = Membership.objects.get_or_create(
            user=user, organization=org, defaults={"role": role, "approval_limit": limit})
    if not new_member:
        messages.info(request, f"{email} is already a member of {org.name}.")
        return redirect("core:team")
    audit(org, "team.invited", membership, actor=request.user, email=email, role=role, limit=limit)

    if created:
        link = set_password_link(request, user)
        send_mail(
            subject=f"You're invited to {org.name} on ShipMatch",
            message=(f"{request.user.get_full_name() or request.user.get_username()} invited you to review shipments "
                     f"for {org.name}.\n\nSet your password here (the link works for 3 days):\n{link}\n"),
            from_email=None, recipient_list=[email], fail_silently=True,
        )
        request.session["invite_link"] = {"email": email, "link": link}
        messages.success(request, f"Invited {email}. We emailed a link to set a password; you can also copy it below.")
    else:
        messages.success(request, f"Added {email} to {org.name}. They can sign in with their existing password.")
    return redirect("core:team")


def _admins_left(org, excluding: Membership) -> int:
    return Membership.objects.filter(organization=org, role=Membership.Role.ADMIN, user__is_active=True
                                     ).exclude(pk=excluding.pk).count()


@login_required
@require_POST
def update_member(request, pk):
    org = current_org(request)
    require(request.user, org, "manage")
    m = get_object_or_404(Membership, pk=pk, organization=org)
    role = request.POST.get("role", m.role)
    try:
        if role not in Membership.Role.values:
            raise ValueError("Choose a role.")
        if m.user_id == request.user.pk and role != m.role:
            raise ValueError("You can't change your own role. Ask another admin.")
        if m.role == Membership.Role.ADMIN and role != Membership.Role.ADMIN and _admins_left(org, m) == 0:
            raise ValueError("Every organization needs at least one admin.")
        limit = _limit(request.POST.get("approval_limit", ""))
    except ValueError as e:
        messages.error(request, str(e))
        return redirect("core:team")
    old = {"role": m.role, "limit": m.approval_limit}
    m.role, m.approval_limit = role, limit
    m.save(update_fields=["role", "approval_limit"])
    audit(org, "team.updated", m, actor=request.user, user=m.user.get_username(),
          old_role=old["role"], role=role, old_limit=old["limit"], limit=limit)
    messages.success(request, f"Updated {m.user.get_full_name() or m.user.get_username()}.")
    return redirect("core:team")


@login_required
@require_POST
def remove_member(request, pk):
    org = current_org(request)
    require(request.user, org, "manage")
    m = get_object_or_404(Membership, pk=pk, organization=org)
    if m.user_id == request.user.pk:
        messages.error(request, "You can't remove yourself. Ask another admin.")
    elif m.role == Membership.Role.ADMIN and _admins_left(org, m) == 0:
        messages.error(request, "Every organization needs at least one admin.")
    else:
        user = m.user
        m.delete()
        if not user.memberships.exists() and not user.is_superuser:
            user.is_active = False
            user.save(update_fields=["is_active"])
        audit(org, "team.removed", user, actor=request.user, user=user.get_username())
        messages.success(request, f"Removed {user.get_full_name() or user.get_username()} from {org.name}.")
    return redirect("core:team")


@login_required
@require_POST
def reset_member_mfa(request, pk):
    org = current_org(request)
    require(request.user, org, "manage")
    m = get_object_or_404(Membership, pk=pk, organization=org)
    mfa.disable(m.user)
    audit(org, "team.mfa_reset", m.user, actor=request.user, user=m.user.get_username())
    messages.success(request, f"Two-factor authentication reset for {m.user.get_username()}. "
                              "They will set it up again at next sign-in if your organization requires it.")
    return redirect("core:team")


@login_required
@require_POST
def send_password_link(request, pk):
    org = current_org(request)
    require(request.user, org, "manage")
    m = get_object_or_404(Membership, pk=pk, organization=org)
    link = set_password_link(request, m.user)
    if m.user.email:
        send_mail(subject="Set your ShipMatch password", message=f"Set a new password here (works for 3 days):\n{link}\n",
                  from_email=None, recipient_list=[m.user.email], fail_silently=True)
    request.session["invite_link"] = {"email": m.user.email or m.user.get_username(), "link": link}
    audit(org, "team.password_link", m.user, actor=request.user, user=m.user.get_username())
    messages.success(request, "Password link created. It was emailed if the user has an email address.")
    return redirect("core:team")
