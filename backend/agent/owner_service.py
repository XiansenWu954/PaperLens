"""Phase 2 Batch C (Task 4.4) — database owner lease service.

Acquisition/renewal/release use ``select_for_update`` row locking with a
fixed 300-second lease. The owner token exists only in the private
``ProjectRun.owner_token`` DB column and process memory — it is never part
of events, logs, API responses, serializers or Celery broker payloads.

Semantics:
  acquire  -> True if no valid owner (or expired); False if a valid lease
              exists. Taking over an expired lease bumps resume_count.
  renew    -> True if the caller still owns the run (token matches) and the
              lease is extended by another 300s.
  release  -> True if the caller owned the run; owner cleared.
  is_owner -> True if the given token matches the current valid owner.
"""
from __future__ import annotations

import secrets
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

LEASE_SECONDS = 300


def new_owner_token() -> str:
    """Random owner token — process memory / private DB column only."""
    return secrets.token_hex(16)


@transaction.atomic
def acquire_owner(run, token: str | None = None) -> bool:
    """Atomically acquire the 300s owner lease for one run.

    Returns True when this caller becomes the owner (either free or expired
    lease). Returns False when another valid owner holds the lease — the
    duplicate task must exit safely without touching the graph.
    """
    from api.models import ProjectRun

    locked = ProjectRun.objects.select_for_update().get(id=run.id)
    now = timezone.now()
    token = token or new_owner_token()
    if locked.owner_token and locked.lease_expires_at and \
            locked.lease_expires_at > now:
        return False
    took_over = bool(locked.owner_token)
    locked.owner_token = token
    locked.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
    if took_over:
        locked.resume_count = (locked.resume_count or 0) + 1
    fields = ["owner_token", "lease_expires_at", "updated_at"]
    if took_over:
        fields.append("resume_count")
    locked.save(update_fields=fields)
    return True


@transaction.atomic
def renew_owner(run, token: str) -> bool:
    """Extend the lease for another 300s if ``token`` is still the owner
    AND the lease is still valid.

    P2-C-CX-02: an expired lease means the owner may already have been
    superseded — renewal fails so the old executor stops writing side
    effects and exits safely.
    """
    from api.models import ProjectRun

    locked = ProjectRun.objects.select_for_update().get(id=run.id)
    if not locked.owner_token or locked.owner_token != token:
        return False
    if not (locked.lease_expires_at and locked.lease_expires_at > timezone.now()):
        return False
    locked.lease_expires_at = timezone.now() + timedelta(seconds=LEASE_SECONDS)
    locked.save(update_fields=["lease_expires_at", "updated_at"])
    return True


@transaction.atomic
def release_owner(run, token: str) -> bool:
    """Release the lease if ``token`` is the current owner."""
    from api.models import ProjectRun

    locked = ProjectRun.objects.select_for_update().get(id=run.id)
    if not locked.owner_token or locked.owner_token != token:
        return False
    locked.owner_token = ""
    locked.lease_expires_at = None
    locked.save(update_fields=["owner_token", "lease_expires_at",
                               "updated_at"])
    return True


def is_owner(run, token: str) -> bool:
    """Non-locking ownership check (caller must still handle races)."""
    from api.models import ProjectRun

    row = ProjectRun.objects.only(
        "owner_token", "lease_expires_at").get(id=run.id)
    if not row.owner_token or row.owner_token != token:
        return False
    return bool(row.lease_expires_at and row.lease_expires_at > timezone.now())
