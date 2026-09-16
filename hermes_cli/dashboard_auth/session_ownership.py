"""Exact persisted CUI ownership shared by HTTP history and RPC resume."""

from hermes_cli.dashboard_auth.identity import (
    SESSION_ADMIN_ROLE_ALIASES,
    is_complete_authenticated_identity,
    normalize_role,
)


def has_exact_cui_session_owner(config: dict, actor: dict) -> bool:
    """Accept complete, consistent owner stamps, including scope-less legacy rows.

    This is a positive proof only; callers retain their own non-owner policies.
    """
    if actor.get("_restricted") or not is_complete_authenticated_identity(actor):
        return False
    owner_keys = {
        "tenant_id": "_cui_tenant_id",
        "actor_id": "_cui_actor_id",
        "role": "_cui_actor_role",
    }
    owners = []
    if any(key in config for key in owner_keys.values()):
        owners.append({key: config.get(stored) for key, stored in owner_keys.items()})
    if "_cui_actor_context" in config:
        owners.append(config["_cui_actor_context"])
    if not owners:
        return False
    for owner in owners:
        # Never combine incomplete identities or ignore conflicting duplicates.
        if not isinstance(owner, dict) or not is_complete_authenticated_identity(owner):
            return False
        if (owner["tenant_id"] != actor["tenant_id"]
                or owner["actor_id"] != actor["actor_id"]
                or normalize_role(owner["role"]) != normalize_role(actor["role"])):
            return False
        # Older authenticated producers persisted ownership without a scope.
        # An explicit scope must still agree with the producer's raw role policy
        # (not the normalized role: aiwerk_admin was stamped customer).
        expected_scope = (
            "admin" if owner["role"].strip().lower() in SESSION_ADMIN_ROLE_ALIASES else "customer"
        )
        if "_cui_visibility_scope" in config and config["_cui_visibility_scope"] != expected_scope:
            return False
    return True
