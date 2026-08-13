from __future__ import annotations

# Canonical authorization matrix. Frontend reads the capabilities returned by
# /api/auth/login and /api/auth/me; server-side table write gates mirror this.
ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    "SUPER_ADMIN": frozenset({"*"}),
    "ADMIN": frozenset({"*"}),
    "SUPERVISOR": frozenset({
        "dashboard.read", "history.read",
        "transfer.write", "flowmeter.write", "hm.write",
        "receiving.write", "drainage.write", "sounding.write", "cleanliness.write",
        "closing.read", "discrepancy.read", "master.read", "reporting.read",
    }),
    "FIELD": frozenset({"dashboard.read", "history.read", "transfer.write", "flowmeter.write", "hm.write", "receiving.write", "drainage.write", "sounding.write", "cleanliness.write"}),
    "FUELMAN": frozenset({"dashboard.read", "history.read", "transfer.write", "flowmeter.write", "cleanliness.write"}),
    "DRIVER": frozenset({"dashboard.read", "history.read", "hm.write", "cleanliness.write"}),
    "PENERIMAAN": frozenset({"dashboard.read", "history.read", "receiving.write", "drainage.write", "sounding.write", "cleanliness.write"}),
    "GROUP_LEADER": frozenset({"dashboard.read", "history.read", "closing.read", "closing.write", "discrepancy.read", "discrepancy.write", "master.read", "reporting.read"}),
    "VENDOR": frozenset({"dashboard.read", "history.read"}),
}

VALID_ROLES = frozenset(ROLE_CAPABILITIES)


def capabilities_for(role: str) -> list[str]:
    caps = ROLE_CAPABILITIES.get(str(role or "").upper(), frozenset())
    return sorted(caps)


def has_capability(role: str, capability: str) -> bool:
    caps = ROLE_CAPABILITIES.get(str(role or "").upper(), frozenset())
    return "*" in caps or capability in caps
