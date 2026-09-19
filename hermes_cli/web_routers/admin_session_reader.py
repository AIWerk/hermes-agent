"""Explicit delegated read routes; all decisions and audit belong to the broker."""
from fastapi import APIRouter, HTTPException, Query, Request
from hermes_cli.dashboard_auth.admin_session_reader import read_sessions, AdminReadUnavailable
from hermes_cli.dashboard_auth.profile_access import ProfileAccessDenied

router = APIRouter(prefix="/api/admin/session-reader")


def _read(request, profile, operation, **kwargs):
    try:
        return read_sessions(getattr(request.state, "session", None), profile, operation, **kwargs)
    except ProfileAccessDenied:
        raise HTTPException(403, "profile access denied") from None
    except AdminReadUnavailable:
        raise HTTPException(503, "session data unavailable") from None


@router.get("/{profile}/sessions")
def sessions(request: Request, profile: str, limit: int = 50, offset: int = 0):
    return _read(request, profile, "list", limit=limit, offset=offset)


@router.get("/{profile}/sessions/search")
def search(request: Request, profile: str, query: str = "", limit: int = 50, offset: int = 0):
    return _read(request, profile, "search", query=query, limit=limit, offset=offset)


@router.get("/{profile}/sessions/{session_id}")
def detail(request: Request, profile: str, session_id: str,
           limit: int = Query(50, ge=1, le=100),
           offset: int = Query(0, ge=0, le=1_000_000)):
    return _read(request, profile, "read", session_id=session_id, limit=limit, offset=offset)
