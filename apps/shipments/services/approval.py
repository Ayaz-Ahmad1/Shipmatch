"""Who may approve a shipment: role, open errors, maker-checker, and approval limits.

Fails closed: if a limit applies and an amount cannot be converted to the home currency,
the shipment needs an approver without a limit.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.db.models import Count

from apps.core.models import AuditEvent
from apps.core.permissions import has_perm, membership_for
from apps.documents.models import Document
from apps.shipments.models import Shipment, ValidationIssue

# Actions that make a person a "maker" (preparer) of a shipment.
MAKER_ACTIONS = {"field.corrected", "document.moved", "document.received", "document.type_changed", "issue.resolved"}

# Other apps add approval rules without editing this file: call register_approval_blocker(fn) from their
# AppConfig.ready(). fn(shipment, user) returns a list of reasons (empty = no objection).
APPROVAL_BLOCKERS = []


def register_approval_blocker(fn) -> None:
    if fn not in APPROVAL_BLOCKERS:
        APPROVAL_BLOCKERS.append(fn)


# Reasons an approved shipment's bills must not be posted yet (e.g. an invoice shared with shipments that
# aren't approved). fn(shipment) returns a list of reasons; register from AppConfig.ready().
POSTING_BLOCKERS = []


def register_posting_blocker(fn) -> None:
    if fn not in POSTING_BLOCKERS:
        POSTING_BLOCKERS.append(fn)


# Rules that only ever object to shipments with something special (a dispute waiting, a shared invoice) are marked
# `fn.skip_when_quiet = True` and register a provider here: fn(shipment_ids) -> the ids that have such a thing.
# A list of many shipments (My work) then runs those rules only for the few shipments they can apply to.
QUIET_PROVIDERS = []


def register_quiet_provider(fn) -> None:
    if fn not in QUIET_PROVIDERS:
        QUIET_PROVIDERS.append(fn)


def posting_blockers(shipment: Shipment) -> list[str]:
    reasons: list[str] = []
    for blocker in POSTING_BLOCKERS:
        reasons.extend(blocker(shipment))
    return reasons


@dataclass
class Totals:
    by_currency: dict[str, Decimal] = field(default_factory=dict)  # invoices minus credit notes
    home: Decimal | None = Decimal("0.00")      # None if some currency has no exchange rate
    missing_rates: list[str] = field(default_factory=list)
    credits: dict[str, Decimal] = field(default_factory=dict)       # credit notes, as positive amounts


def shipment_totals(shipment: Shipment) -> Totals:
    """What the shipment costs: payable invoices net of credit notes, per currency and in the home currency."""
    return totals_from_documents(shipment.organization, shipment.documents.prefetch_related("fields"))


def totals_from_documents(org, documents) -> Totals:
    t = Totals()
    for doc in documents:
        if not doc.posts_to_accounting:
            continue
        try:
            amount = Decimal(str(doc.field("total_amount"))).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError, TypeError):
            continue
        cur = (doc.field("currency") or org.home_currency).upper()
        if doc.is_credit:
            amount = abs(amount)
            t.credits[cur] = t.credits.get(cur, Decimal("0.00")) + amount
            amount = -amount
        t.by_currency[cur] = t.by_currency.get(cur, Decimal("0.00")) + amount
    for cur, amount in t.by_currency.items():
        converted = org.to_home(amount, cur)
        if converted is None:
            t.missing_rates.append(cur)
        elif t.home is not None:
            t.home += converted
    if t.missing_rates:
        t.home = None
    return t


def makers_for(shipment_ids: list[int]) -> dict[int, set[int]]:
    """`makers` for many shipments with one query per table: {shipment id: user ids}."""
    from apps.shipments.models import MatchLink

    doc_owner = {}      # document id (as text) -> shipment id
    for shipment_id, doc_id in (MatchLink.objects.filter(shipment_id__in=shipment_ids)
                                .values_list("shipment_id", "document_id")):
        doc_owner[str(doc_id)] = shipment_id
    issue_owner = {str(pk): shipment_id for pk, shipment_id in
                   ValidationIssue.objects.filter(shipment_id__in=shipment_ids).values_list("pk", "shipment_id")}
    out: dict[int, set[int]] = defaultdict(set)
    events = AuditEvent.objects.filter(action__in=MAKER_ACTIONS, actor__isnull=False)
    for object_type, owners in (("Document", doc_owner), ("ValidationIssue", issue_owner)):
        keys = list(owners)
        for start in range(0, len(keys), 500):   # keep each IN list well inside the database's variable limit
            rows = events.filter(object_type=object_type, object_id__in=keys[start:start + 500])
            for object_id, actor_id in rows.values_list("object_id", "actor_id"):
                out[owners[object_id]].add(actor_id)
    return dict(out)


def makers(shipment: Shipment) -> set[int]:
    """User IDs who prepared this shipment (uploaded, edited, moved documents or accepted issues)."""
    doc_ids = [str(pk) for pk in Document.objects.filter(match__shipment=shipment).values_list("pk", flat=True)]
    issue_ids = [str(pk) for pk in ValidationIssue.objects.filter(shipment=shipment).values_list("pk", flat=True)]
    events = AuditEvent.objects.filter(action__in=MAKER_ACTIONS, actor__isnull=False).filter(
        object_type__in=["Document", "ValidationIssue"])
    ids = set()
    for e in events.filter(object_type="Document", object_id__in=doc_ids).values_list("actor_id", flat=True):
        ids.add(e)
    for e in events.filter(object_type="ValidationIssue", object_id__in=issue_ids).values_list("actor_id", flat=True):
        ids.add(e)
    return ids


@dataclass
class ApprovalHints:
    """What `approval_blockers` would otherwise look up one shipment at a time, read for many shipments at once."""

    error_counts: dict[int, int] = field(default_factory=dict)
    makers: dict[int, set[int]] = field(default_factory=dict)
    totals: dict[int, Totals] = field(default_factory=dict)
    busy: set[int] = field(default_factory=set)   # shipments the skip_when_quiet rules could object to


def bulk_hints(shipments: list[Shipment], *, with_totals: bool = False) -> ApprovalHints:
    """Hints for a list of shipments in a handful of queries (instead of about seven per shipment)."""
    ids = [s.pk for s in shipments]
    hints = ApprovalHints()
    if not ids:
        return hints
    hints.error_counts = dict(ValidationIssue.objects.filter(
        shipment_id__in=ids, resolved=False, severity=ValidationIssue.Severity.ERROR)
        .values_list("shipment_id").annotate(n=Count("id")))
    hints.makers = makers_for(ids)
    for provider in QUIET_PROVIDERS:
        hints.busy |= set(provider(ids))
    if with_totals:
        by_ship: dict[int, list] = defaultdict(list)
        for doc in (Document.objects.filter(match__shipment_id__in=ids).select_related("match")
                    .prefetch_related("fields")):
            by_ship[doc.match.shipment_id].append(doc)
        hints.totals = {s.pk: totals_from_documents(s.organization, by_ship.get(s.pk, [])) for s in shipments}
    return hints


def approval_blockers(shipment: Shipment, user, hints: ApprovalHints | None = None) -> list[str]:
    """Reasons this user cannot approve this shipment now. Empty list = may approve.

    `hints` (from `bulk_hints`) only saves queries when many shipments are checked; the answer is the same."""
    org = shipment.organization
    reasons: list[str] = []
    if shipment.is_locked:
        return ["This shipment is already approved."]
    if shipment.status == Shipment.Status.REJECTED:
        reasons.append("This shipment was rejected. Reopen it first.")
    if hints is not None:
        errors = hints.error_counts.get(shipment.pk, 0)
    else:
        errors = shipment.issues.filter(resolved=False, severity=ValidationIssue.Severity.ERROR).count()
    if errors:
        reasons.append(f"Resolve {errors} open error{'s' if errors != 1 else ''} first.")
    for blocker in APPROVAL_BLOCKERS:
        if hints is not None and getattr(blocker, "skip_when_quiet", False) and shipment.pk not in hints.busy:
            continue
        reasons.extend(blocker(shipment, user))
    if not has_perm(user, org, "approve"):
        reasons.append("Only approvers and admins can approve shipments.")
        return reasons
    prepared_by = hints.makers.get(shipment.pk, set()) if hints is not None else None
    if org.maker_checker and user.pk in (makers(shipment) if prepared_by is None else prepared_by):
        reasons.append("You prepared this shipment, so another approver must approve it (maker-checker rule).")
    membership = membership_for(user, org)
    limit = membership.approval_limit if membership else None
    if limit is not None:
        if hints is not None and shipment.pk in hints.totals:
            totals = hints.totals[shipment.pk]
        else:
            totals = shipment_totals(shipment)
        if totals.home is None:
            reasons.append(f"No exchange rate set for {', '.join(totals.missing_rates)}, so your approval limit "
                           "cannot be checked. An admin can add the rate, or an approver without a limit can approve.")
        elif totals.home > limit:
            reasons.append(f"Shipment total {org.home_currency} {totals.home:,.2f} is above your approval limit "
                           f"of {org.home_currency} {limit:,.2f}.")
    return reasons
