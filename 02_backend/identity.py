from __future__ import annotations

import uuid

FIELD_ACTOR_NAMESPACE = uuid.UUID("71b996cd-70a4-5b82-ae05-bc420cc2ddc8")


def field_actor_id(username: str) -> str:
    """Return a stable UUID for UUID audit fields in the current fuel_* family.

    Authentication remains canonical in fcc.app_user (bigint primary key). The
    deterministic UUID prevents mixing app_user.id into UUID created_by,
    updated_by, and uploaded_by columns while keeping one stable actor per user.
    """
    return str(uuid.uuid5(FIELD_ACTOR_NAMESPACE, str(username or "").strip().upper()))
