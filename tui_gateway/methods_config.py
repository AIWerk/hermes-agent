"""Config / projects / setup JSON-RPC handlers. Bodies are rebound onto server.py's globals
(method_ctx.bind_module) and reference them bare. ``config.set`` lives in methods_config_set.
"""

from .method_ctx import HandlerRegistry, bind_module

from hermes_constants import DEFAULT_INDICATOR_STYLE, INDICATOR_STYLES
from hermes_constants import display_hermes_home as _display_hermes_home

_registry = HandlerRegistry()
method = _registry.method
_profile_scoped = _registry.profile_scoped


def _projects_handler(name: str):
    """``@method(name)`` (profile-scoped) whose body's uncaught exception becomes ``_err(rid, 5061)``."""
    def deco(fn):
        def handler(rid, params: dict) -> dict:
            try:
                return fn(rid, params)
            except Exception as e:
                return _err(rid, 5061, str(e))
        return method(name)(_profile_scoped(handler))
    return deco


def _reconcile_repo_discovery(pdb, conn, policy, policy_key):
    pdb.reconcile_discovered_repos_policy(conn, policy_key,
                                          preserve_unversioned=_repo_discovery_policy_is_default(policy))


def _readiness_check(rid, params, probe):
    """Shared shell for setup readiness checks, optionally scoped to a named profile."""
    import contextlib
    profile = str(params.get("profile") or "").strip() if isinstance(params, dict) else ""
    scope = contextlib.nullcontext()
    if profile:
        from hermes_cli import profiles as profiles_mod
        if not profiles_mod.profile_exists(profile):
            return _ok(rid, {"ok": False, "profile": params.get("profile"),
                             "error": f"Profile '{profile}' does not exist on this backend."})
        home = _profile_home(profile)
        if home is not None:
            scope = _session_profile_runtime_scope({"profile_home": str(home)})
    with scope:
        payload = probe(profile, {"profile": profile} if profile else {})
    return _ok(rid, payload)


@_projects_handler("projects.discover_repos")
def _(rid, params: dict) -> dict:
    """Repos for the desktop overview: scanned-from-disk (cached) ∪ session-derived."""
    with _profile_db(params) as db:
        if db is None:
            return _ok(rid, {"repos": []})
        from hermes_cli import projects_db as pdb
        policy = _repo_discovery_policy()
        with pdb.connect_closing() as conn:
            _reconcile_repo_discovery(pdb, conn, policy, _repo_discovery_policy_key(policy))
            # `scan=true` (remote-gateway desktop): its native scan only sees its own filesystem,
            # so the host scans the policy roots so zero-session repos surface.
            # See #81723.
            if params.get("scan") and policy["enabled"]:
                _scan_discovered_repos_remote(conn, policy)
            repos = _discover_repos_payload(db, conn=conn, include_cached=policy["enabled"])
        return _ok(rid, {"repos": repos, "discovery_policy": policy})


@_projects_handler("projects.record_repos")
def _(rid, params: dict) -> dict:
    """Persist repo roots found by the client's (desktop-side) scan; return the merged list."""
    from hermes_cli import projects_db as pdb
    policy = _repo_discovery_policy()
    policy_key = _repo_discovery_policy_key(policy)
    incoming = params.get("discovery_policy")
    if isinstance(incoming, dict):
        accepted = _repo_discovery_policy_key(_repo_discovery_policy(incoming)) == policy_key
    else:
        accepted = _repo_discovery_policy_is_default(policy)  # legacy client without a policy
    accepted = bool(policy["enabled"] and accepted)
    pairs = [(item, None) if isinstance(item, str) else (str(item["root"]), item.get("label"))
             for item in params.get("repos") or []
             if isinstance(item, str) or (isinstance(item, dict) and item.get("root"))]
    with pdb.connect_closing() as conn:
        _reconcile_repo_discovery(pdb, conn, policy, policy_key)
        if accepted:
            pdb.record_discovered_repos(conn, pairs, replace=True, policy_key=policy_key)
        elif not policy["enabled"]:
            pdb.clear_discovered_repos(conn, policy_key=policy_key)
    with _profile_db(params) as db:
        repos = [] if db is None else _discover_repos_payload(db, include_cached=policy["enabled"])
        return _ok(rid, {"repos": repos, "accepted": accepted, "discovery_policy": policy})


def _stamped_project_tree(db, params, **kwargs):
    """``_build_project_tree`` + profile stamping shared by the two tree RPCs."""
    from tui_gateway.project_tree import stamp_profile
    tree, active_id = _build_project_tree(db, **kwargs)
    stamp_profile(tree["projects"], _response_profile_name(params.get("profile")))
    return tree, active_id


@_projects_handler("projects.tree")
def _(rid, params: dict) -> dict:
    """Project -> repo -> lane overview with counts + a few preview sessions per project, plus the
    flat set of session ids claimed by any project (excluded from flat Recents). Lanes carry no
    session rows; drill-in uses ``projects.project_sessions``."""
    with _profile_db(params) as db:
        if db is None:
            return _ok(rid, {"projects": [], "active_id": None, "scoped_session_ids": []})
        tree, active_id = _stamped_project_tree(
            db, params, preview_limit=int(params.get("preview_limit") or 3), hydrate=False,
            session_limit=int(params.get("session_limit") or 2000), include_discovered=True)
        return _ok(rid, {"projects": tree["projects"], "active_id": active_id,
                         "scoped_session_ids": tree["scoped_session_ids"]})


@_projects_handler("projects.project_sessions")
def _(rid, params: dict) -> dict:
    """Fully hydrated lanes for one project, from the same grouping as ``projects.tree``."""
    project_id = str(params.get("project_id") or "")
    if not project_id:
        return _err(rid, 5063, "project_id required")
    with _profile_db(params) as db:
        if db is None:
            return _ok(rid, {"project": None})
        # Drill-in only needs the entered project: skip the zero-session discovery tier.
        tree, _active = _stamped_project_tree(
            db, params, preview_limit=0, hydrate=True,
            session_limit=int(params.get("session_limit") or 5000), include_discovered=False)
        return _ok(rid, {"project": next((p for p in tree["projects"] if p["id"] == project_id), None)})


# ── config.get — one getter per key returning the result payload.

def _display_raw() -> dict:
    return _load_cfg().get("display") or {}


def _display_word(key: str, default: str, allowed) -> str:
    """Normalised ``display.<key>``; unknown/garbage values read back as ``default``."""
    raw = str(_display_raw().get(key, default) or "").strip().lower()
    return raw if raw in allowed else default


_THINKING_MODES = frozenset({"collapsed", "truncated", "full"})


def _cfg_get_provider(params):
    from hermes_cli.models import list_available_providers, normalize_provider
    model = _resolve_model()
    parts = model.split("/", 1)
    return {"model": model, "provider": normalize_provider(parts[0]) if len(parts) > 1 else "unknown",
            "providers": list_available_providers()}


def _cfg_get_project(params):
    raw = str(params.get("cwd", "") or (_load_cfg().get("terminal") or {}).get("cwd", "") or "").strip()
    cwd = _completion_cwd({"cwd": raw} if raw else {})
    return {"cwd": cwd, "branch": git_probe.branch(cwd)}


def _cfg_get_personality(params):
    # EFFECTIVE personality via the single owner — a stale/unknown name must not show as active.
    from hermes_cli.personality import active_personality_name
    return {"value": active_personality_name(_load_cfg()) or "none"}


def _cfg_get_reasoning(params):
    cfg = _load_cfg()
    session = _sessions.get(params.get("session_id", "")) or {}
    reasoning_config = session.get("create_reasoning_override")
    if session and not isinstance(reasoning_config, dict):
        reasoning_config = getattr(session.get("agent"), "reasoning_config", None)
    if isinstance(reasoning_config, dict):
        enabled = reasoning_config.get("enabled") is not False
        effort = str(reasoning_config.get("effort") or "medium") if enabled else "none"
    else:
        raw_effort = (cfg.get("agent") or {}).get("reasoning_effort", "")
        # YAML `reasoning_effort: false` means thinking disabled, not "unset".
        effort = "none" if raw_effort is False else str(raw_effort or "medium")
    display = "show" if (cfg.get("display") or {}).get("show_reasoning", True) else "hide"
    return {"value": effort, "display": display}


def _cfg_get_fast(params):
    # `config.set fast` is session-scoped: prefer the session's live/pinned value over the
    # global key (a pre-build session keeps its pin in create_service_tier_override).
    session = _sessions.get(params.get("session_id", "")) or {}
    agent = session.get("agent")
    tier = (getattr(agent, "service_tier", None) if agent is not None
            else session.get("create_service_tier_override"))
    if tier is None:
        tier = _load_service_tier()
    return {"value": "fast" if tier == "priority" else "normal"}


def _cfg_get_thinking_mode(params):
    raw = _display_word("thinking_mode", "", _THINKING_MODES)
    if not raw:  # legacy details_mode fallback
        raw = "full" if _display_word("details_mode", "collapsed", _DETAIL_MODES) == "expanded" else "collapsed"
    return {"value": raw}


def _cfg_get_mtime(params):
    cfg_path = _hermes_home / "config.yaml"
    try:
        mtime = cfg_path.stat().st_mtime if cfg_path.exists() else 0
    except Exception:
        return {"mtime": 0}
    # mcp_rev: hash of the MCP-relevant sections so the poller reloads MCP servers only when
    # their config changed — a /skin write bumps mtime but must not cost an MCP reconnect.
    return {"mtime": mtime, "mcp_rev": _compute_mcp_rev()}


# key -> getter(params); bind_module rebinds the table's functions onto server.py's globals.
_CONFIG_GETTERS = {
    "provider": _cfg_get_provider,
    "profile": lambda params: {"home": str(_hermes_home), "display": _display_hermes_home()},
    "project": _cfg_get_project,
    "full": lambda params: {"config": _load_cfg()},
    "prompt": lambda params: {"prompt": _load_cfg().get("custom_prompt", "")},
    "skin": lambda params: {"value": _display_raw().get("skin", "default")},
    # Normalised like the TUI renders it (frontend falls back to the default for the same inputs).
    "indicator": lambda params: {
        "value": _display_word("tui_status_indicator", DEFAULT_INDICATOR_STYLE, INDICATOR_STYLES)},
    "personality": _cfg_get_personality,
    "reasoning": _cfg_get_reasoning,
    "fast": _cfg_get_fast,
    "busy": lambda params: {"value": _load_busy_input_mode()},
    "approval_mode": lambda params: {"value": _load_approval_mode()},
    "approvals.mode": lambda params: {"value": _load_approval_mode()},
    "details_mode": lambda params: {"value": _display_word("details_mode", "collapsed", _DETAIL_MODES)},
    "thinking_mode": _cfg_get_thinking_mode,
    "density": lambda params: {"value": "on" if bool(_display_raw().get("tui_compact", False)) else "off"},
    "theme": lambda params: {"value": _display_word("tui_theme", "auto", {"auto", "light", "dark"})},
    "statusbar": lambda params: {"value": _coerce_statusbar(_display_cfg().get("tui_statusbar", "top"))},
    "focus": lambda params: {"value": "on" if bool(_display_cfg().get("focus_view", False)) else "off",
                             "tool_progress": _load_tool_progress_mode()},
    "mouse": lambda params: {"value": _display_mouse_tracking(_load_cfg().get("display"))},
    "mtime": _cfg_get_mtime}
# Getters whose failure is a JSON-RPC error of this code (others propagate to dispatch).
_CONFIG_GET_ERR = {"provider": 5013, "approval_mode": 5001, "approvals.mode": 5001}


@method("config.get")
@_profile_scoped
def _(rid, params: dict) -> dict:
    key = params.get("key", "")
    if key == "provider":
        try:
            from hermes_cli.models import list_available_providers, normalize_provider

            model = _resolve_model()
            parts = model.split("/", 1)
            return _ok(
                rid,
                {
                    "model": model,
                    "provider": (
                        normalize_provider(parts[0]) if len(parts) > 1 else "unknown"
                    ),
                    "providers": list_available_providers(),
                },
            )
        except Exception as e:
            return _err(rid, 5013, str(e))
    if key == "profile":
        from hermes_constants import display_hermes_home

        return _ok(rid, {"home": str(_hermes_home), "display": display_hermes_home()})
    if key == "project":
        cfg_terminal = _load_cfg().get("terminal") or {}
        raw = str(params.get("cwd", "") or cfg_terminal.get("cwd", "") or "").strip()
        cwd = _completion_cwd({"cwd": raw} if raw else {})
        return _ok(rid, {"cwd": cwd, "branch": _git_branch_for_cwd(cwd)})
    if key == "full":
        return _ok(rid, {"config": _load_cfg()})
    if key == "prompt":
        return _ok(rid, {"prompt": _load_cfg().get("custom_prompt", "")})
    if key == "skin":
        return _ok(
            rid, {"value": (_load_cfg().get("display") or {}).get("skin", "default")}
        )
    if key == "indicator":
        # Normalize so a hand-edited config.yaml with stray casing or
        # an unknown value reads back the SAME value the TUI actually
        # rendered (frontend's `normalizeIndicatorStyle` falls back to
        # `DEFAULT_INDICATOR_STYLE` for the same inputs).  Otherwise
        # `/indicator` would print one thing while the UI shows another.
        raw = (_load_cfg().get("display") or {}).get("tui_status_indicator", "")
        norm = str(raw).strip().lower()
        return _ok(
            rid,
            {"value": norm if norm in INDICATOR_STYLES else DEFAULT_INDICATOR_STYLE},
        )
    if key == "personality":
        # Report the EFFECTIVE personality via the single owner — a stale or
        # unknown name in config must not display as active.
        from hermes_cli.personality import active_personality_name

        return _ok(
            rid,
            {"value": active_personality_name(_load_cfg()) or "none"},
        )
    if key == "reasoning":
        cfg = _load_cfg()
        session = _sessions.get(params.get("session_id", ""))
        reasoning_config = None
        if session is not None:
            if isinstance(session.get("create_reasoning_override"), dict):
                reasoning_config = session.get("create_reasoning_override")
            else:
                agent = session.get("agent")
                agent_reasoning = getattr(agent, "reasoning_config", None)
                if isinstance(agent_reasoning, dict):
                    reasoning_config = agent_reasoning

        if isinstance(reasoning_config, dict):
            if reasoning_config.get("enabled") is False:
                effort = "none"
            else:
                effort = str(reasoning_config.get("effort") or "medium")
        else:
            raw_effort = (cfg.get("agent") or {}).get("reasoning_effort", "")
            if raw_effort is False:
                # YAML `reasoning_effort: false`/`off`/`no` — thinking
                # disabled, not "unset, show the medium default".
                effort = "none"
            else:
                effort = str(raw_effort or "medium")
        display = (
            "show"
            if bool((cfg.get("display") or {}).get("show_reasoning", True))
            else "hide"
        )
        return _ok(rid, {"value": effort, "display": display})
    if key == "fast":
        # Prefer the session's live/pinned value — `config.set fast` is
        # session-scoped, so the global key may not reflect this chat. A
        # pre-build session keeps its pin in create_service_tier_override.
        session = _sessions.get(params.get("session_id", ""))
        tier = None
        if session is not None:
            agent = session.get("agent")
            if agent is not None:
                tier = getattr(agent, "service_tier", None)
            elif session.get("create_service_tier_override") is not None:
                tier = session["create_service_tier_override"]
        if tier is None:
            tier = _load_service_tier()
        return _ok(rid, {"value": "fast" if tier == "priority" else "normal"})
    if key == "busy":
        return _ok(rid, {"value": _load_busy_input_mode()})
    if key == "yolo":
        try:
            session = _sessions.get(params.get("session_id", ""))
            if session is not None:
                from tools.approval import is_session_yolo_enabled

                enabled = is_session_yolo_enabled(session["session_key"])
            else:
                enabled = str(os.environ.get("HERMES_YOLO_MODE", "")).strip().lower() in {
                    "1", "true", "yes", "on",
                }
            return _ok(rid, {"value": "on" if enabled else "off"})
        except Exception as e:
            return _err(rid, 5001, str(e))
    if key in {"approval_mode", "approvals.mode"}:
        try:
            return _ok(rid, {"value": _load_approval_mode()})
        except Exception as e:
            return _err(rid, 5001, str(e))
    if key == "details_mode":
        allowed_dm = frozenset({"hidden", "collapsed", "expanded"})
        raw = (
            str(
                (_load_cfg().get("display") or {}).get("details_mode", "collapsed")
                or "collapsed"
            )
            .strip()
            .lower()
        )
        nv = raw if raw in allowed_dm else "collapsed"
        return _ok(rid, {"value": nv})
    if key == "thinking_mode":
        allowed_tm = frozenset({"collapsed", "truncated", "full"})
        cfg = _load_cfg()
        raw = (
            str((cfg.get("display") or {}).get("thinking_mode", "") or "")
            .strip()
            .lower()
        )
        if raw in allowed_tm:
            nv = raw
        else:
            dm = (
                str(
                    (cfg.get("display") or {}).get("details_mode", "collapsed")
                    or "collapsed"
                )
                .strip()
                .lower()
            )
            nv = "full" if dm == "expanded" else "collapsed"
        return _ok(rid, {"value": nv})
    if key == "density":
        on = bool((_load_cfg().get("display") or {}).get("tui_compact", False))
        return _ok(rid, {"value": "on" if on else "off"})
    if key == "theme":
        display = _load_cfg().get("display")
        raw = (
            str(
                display.get("tui_theme", "auto")
                if isinstance(display, dict)
                else "auto"
            )
            .strip()
            .lower()
        )
        return _ok(rid, {"value": raw if raw in {"auto", "light", "dark"} else "auto"})
    if key == "statusbar":
        display = _load_cfg().get("display")
        raw = (
            display.get("tui_statusbar", "top") if isinstance(display, dict) else "top"
        )
        return _ok(rid, {"value": _coerce_statusbar(raw)})
    if key == "focus":
        display = _load_cfg().get("display")
        on = (
            bool(display.get("focus_view", False))
            if isinstance(display, dict)
            else False
        )
        return _ok(
            rid,
            {
                "value": "on" if on else "off",
                "tool_progress": _load_tool_progress_mode(),
            },
        )
    if key == "mouse":
        display = _load_cfg().get("display")
        return _ok(rid, {"value": _display_mouse_tracking(display)})
    if key == "mtime":
        cfg_path = _hermes_home / "config.yaml"
        try:
            mtime = cfg_path.stat().st_mtime if cfg_path.exists() else 0
        except Exception:
            return _ok(rid, {"mtime": 0})
        # Revision hash of the MCP-relevant config sections. The TUI's
        # config-change poller uses it to reload MCP servers only when their
        # config actually changed — a /skin or /statusbar write bumps mtime
        # but must not cost a multi-second MCP reconnect.
        return _ok(rid, {"mtime": mtime, "mcp_rev": _compute_mcp_rev()})
    return _err(rid, 4002, f"unknown config key: {key}")


@method("setup.status")
def _(rid, params: dict) -> dict:
    """Loose provider check; ``profile`` (optional) scopes it to that profile's home."""
    try:
        from hermes_cli.main import _has_any_provider_configured
        return _readiness_check(rid, params, lambda profile, scoped: {
            "provider_configured": bool(_has_any_provider_configured(strict_profile_scope=bool(profile))),
            **scoped})
    except Exception as e:
        return _err(rid, 5016, str(e))


@method("setup.runtime_check")
def _(rid, params: dict) -> dict:
    """Strict provider check via the same resolve_runtime_provider() the agent uses on session
    creation (setup.status is True if ANY provider auth state is discoverable): ok=False + the auth
    error when the model can't be served, so UIs surface onboarding before a doomed prompt.
    ``profile`` answers for THAT profile's pin and ``.env``; unknown -> ``ok=False``."""
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider
        from hermes_cli.auth import has_usable_secret
        from hermes_cli.main import _has_any_provider_configured
        requested = str(params.get("provider") or "").strip() or None

        def probe(profile, scoped):
            runtime = resolve_runtime_provider(requested=requested)
            provider_configured = bool(_has_any_provider_configured(strict_profile_scope=bool(profile)))
            provider = runtime.get("provider") or "provider"
            source = str(runtime.get("source") or "")

            def fail(error, src):
                return {"ok": False, "provider": provider, "model": runtime.get("model"),
                        "source": src, "error": error, **scoped}
            if (not provider_configured and provider == "bedrock"
                    and source in {"iam-role", "aws-sdk-default-chain"}):
                return fail("No Hermes provider is configured.", source)
            api_key = runtime.get("api_key")
            api_key_text = "" if callable(api_key) else str(api_key or "").strip()
            if not (callable(api_key) or api_key_text in {"aws-sdk", "no-key-required"}
                    or has_usable_secret(api_key_text) or bool(runtime.get("command"))):
                return fail(f"No usable credentials found for {provider}.", runtime.get("source"))
            return {"ok": True, "provider": runtime.get("provider"), "model": runtime.get("model"),
                    "source": runtime.get("source"), **scoped}
        return _readiness_check(rid, params, probe)
    except Exception as e:
        return _ok(rid, {"ok": False, "error": str(e)})


def _safe_client_label(label: str) -> str:
    """Alnum/._- () only, ≤64 chars, dot-runs and leading dots collapsed (no traversal shapes)."""
    safe = "".join(ch for ch in label if ch.isalnum() or ch in "._- ()").strip()[:64]
    while ".." in safe:
        safe = safe.replace("..", ".")
    return safe.lstrip(".").strip()


@method("diagnostics.share_nous")
def _(rid, params: dict) -> dict:
    """Upload a redacted debug bundle to Nous-internal diagnostics storage — same collection +
    force-redaction pipeline as ``hermes debug share --nous``; redaction is NOT client-controllable
    and consent lives with the CALLER (privacy notice first). Structured ``ok``/``error`` envelope so
    upload failures render inline. Optional: ``error_context`` (-> ``error-context.txt``),
    ``extra_files`` ({label -> text}), ``log_lines`` (default 200); all force-redacted."""
    try:
        from hermes_cli.debug import _redact_log_text, build_nous_bundle, collect_share_bundle
        from hermes_cli.diagnostics_upload import share_to_nous
        log_lines = params.get("log_lines")
        if not isinstance(log_lines, int) or not (10 <= log_lines <= 2000):
            log_lines = 200
        bundle = collect_share_bundle(log_lines=log_lines, redact=True)
        # Client text goes through the SAME upload-safe redactor as backend logs (force secret
        # redaction + email masking), never the weaker bare secret pass.
        error_context = params.get("error_context")
        if isinstance(error_context, str) and error_context.strip():
            bundle["error-context.txt"] = _redact_log_text(error_context.strip()[:8_000])
        # Bounded: at most 4 files, 512KB each, sanitized labels — not an arbitrary upload surface.
        extra_files = params.get("extra_files")
        for label, text in list(extra_files.items())[:4] if isinstance(extra_files, dict) else ():
            safe_label = _safe_client_label(label) if isinstance(label, str) else ""
            if safe_label and isinstance(text, str) and text.strip():
                bundle[f"client/{safe_label}"] = _redact_log_text(text[:524_288])
        res = share_to_nous(build_nous_bundle(bundle, redact=True))
        view_url = res.get("viewUrl") or res.get("view_url")
        upload_id = res.get("id")
        if not view_url and not upload_id:  # an upload the user can't reference is useless to support
            return _ok(rid, {"ok": False, "error": "upload succeeded but returned no view URL or id"})
        return _ok(rid, {"ok": True, "view_url": view_url, "upload_id": upload_id,
                         "expires_at": res.get("expiresAt") or res.get("expires_at")})
    except Exception as e:
        return _ok(rid, {"ok": False, "error": str(e)})


def register(server) -> None:
    bind_module(globals(), server, skip=("_",))
