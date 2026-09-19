"""Synthetic root-policy fixtures; never opens live authority or profile stores."""
import importlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

# Optional external RED recorder; enabled only by explicit -p on the focused gate.
_RED_ROOT = None

def pytest_addoption(parser):
    parser.addoption("--red-evidence-dir", default=None)

def pytest_configure(config):
    global _RED_ROOT
    _RED_ROOT = config.getoption("--red-evidence-dir")

def pytest_collection_finish(session):
    _red_evidence({"kind": "collection", "nodes": [i.nodeid for i in session.items]})

def pytest_runtest_logstart(nodeid, location):
    _red_evidence({"kind": "start", "node": nodeid})

def pytest_runtest_logreport(report):
    _red_evidence({"kind": "report", "node": report.nodeid, "when": report.when,
                   "outcome": report.outcome, "cause": str(report.longrepr) if report.failed else ""})

def _red_evidence(event):
    import json
    root = _RED_ROOT
    if root:
        evidence_root = Path(root)
        evidence_root.mkdir(parents=True, exist_ok=True)
        with (evidence_root / (str(os.getpid()) + ".jsonl")).open("a") as stream:
            stream.write(json.dumps(event) + "\n")

EMPLOYEE = dict(tenant_id="tenant-a", actor_id="employee", role="user", provider="test-idp")
ADMIN = dict(tenant_id="tenant-a", actor_id="admin", role="admin", provider="test-idp")
OWN_ACTIONS = ["profile.discover", "profile.use", "session.list", "session.search",
               "session.read", "session.resume", "session.mutate"]
READ_ACTIONS = ["session.list", "session.search", "session.read"]
BEHAVIOR_ACTIONS = ["behavior.read", "behavior.write"]


def capability(module):
    try:
        return importlib.import_module(module)
    except ModuleNotFoundError as exc:
        if exc.name != module:
            raise
        pytest.fail(f"Missing authorization capability: {module}")


def install_policy(tmp_path, monkeypatch):
    from hermes_cli.dashboard_auth import profile_policy as policy
    import hermes_state
    # Keep the live-root guard anchored before synthetic Path.home overrides.
    # Do not disable the guard: real production roots remain forbidden.
    guarded_root = hermes_state._real_platform_state_root()
    monkeypatch.setattr(hermes_state, "_real_platform_state_root", lambda: guarded_root)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    root = home / ".hermes"
    root.mkdir()
    marker = tmp_path / "required"
    path = tmp_path / "policy.yaml"
    marker.write_bytes(b"required-v1\n")
    marker.chmod(0o600)
    monkeypatch.setattr(policy, "REQUIRED_MARKER_PATH", str(marker))
    monkeypatch.setattr(policy, "PROFILE_POLICY_PATH", str(path))
    monkeypatch.setattr(policy, "TRUSTED_ROOT_UID", os.getuid())
    document = {
        "version": 1,
        "profiles": [dict(profile_id=p, tenant_id=t, kind="employee", enabled=e)
                     for p, t, e in [("employee-home", "tenant-a", True),
                                     ("default", "tenant-a", True),
                                     ("peer-home", "tenant-a", True),
                                     ("other-home", "tenant-b", True),
                                     ("disabled", "tenant-a", False)]],
        "actors": [dict(tenant_id="tenant-a", actor_id=a, default_profile=p)
                   for a, p in [("employee", "employee-home"), ("admin", "default"),
                                ("peer", "peer-home")]],
        "memberships": [
            dict(tenant_id="tenant-a", actor_id="employee", profile_id="employee-home", actions=OWN_ACTIONS.copy()),
            dict(tenant_id="tenant-a", actor_id="peer", profile_id="peer-home", actions=OWN_ACTIONS.copy()),
            dict(tenant_id="tenant-a", actor_id="admin", profile_id="default", actions=list(policy.PROFILE_ACTIONS)),
            dict(tenant_id="tenant-a", actor_id="admin", profile_id="employee-home", actions=BEHAVIOR_ACTIONS.copy()),
            dict(tenant_id="tenant-a", actor_id="admin", profile_id="*", actions=READ_ACTIONS.copy()),
        ],
    }

    def save():
        path.write_text(yaml.safe_dump(document), encoding="utf-8")
        path.chmod(0o600)

    def revoke(actor="employee", action=None):
        for row in document["memberships"]:
            if row["actor_id"] == actor:
                row["actions"] = [a for a in row["actions"] if a != action] if action else ["profile.discover"]
        save()

    save()
    return SimpleNamespace(home=home, root=root, marker=marker, path=path,
                           document=document, save=save, revoke=revoke)


def owner(actor):
    from agent.agent_init import _stamp_authenticated_cui_session_owner
    from agent.cui_actor_context import bind_cui_actor_context, reset_cui_actor_context
    token = bind_cui_actor_context(actor)
    try:
        result = {}
        _stamp_authenticated_cui_session_owner(result)
        return result
    finally:
        reset_cui_actor_context(token)


def seed_store(env, profile="employee-home"):
    from hermes_state import SessionDB
    home = env.root if profile == "default" else env.root / "profiles" / profile
    home.mkdir(parents=True, exist_ok=True)
    db = SessionDB(db_path=home / "state.db")
    db.create_session("owned", source="web", model_config=owner(EMPLOYEE), profile_name=profile)
    db.append_message("owned", "user", "synthetic hello")
    db.create_session("ambiguous", source="web", model_config={}, profile_name=profile)
    db.append_message("ambiguous", "user", "ambiguous hidden")
    db.create_session("foreign", source="web", model_config=owner({**EMPLOYEE, "actor_id": "unmapped"}), profile_name=profile)
    db.append_message("foreign", "user", "unmapped hidden")
    return db, home
