# AIWerk Customer UI Backend Integration Plan

Goal: connect the AIWerk Customer UI mockup to the built-in Hermes dashboard backend so it becomes a real customer-facing chat surface for the local or VPS-hosted Hermes agent.

Visible AIWerk Customer UI copy defaults to German for the intended Swiss SME customer users.

## Scope

This plan targets the AIWerk Customer UI direction, not the Claude Code agent-dashboard area.

Use:
- Hermes built-in dashboard backend in `hermes_cli/web_server.py`
- built-in session-token protection
- built-in `/api/ws` structured gateway for chat
- existing React dashboard app under `web/src/`

Do not use:
- retired `nesquena/hermes-webui`
- frontend-only shell-out hacks
- customer exposure of admin/config/env/logs pages

## Implementation sequence

### 1. Freeze the mockup as design input

Files:
- `docs/aiwerk/mockups/hermes-agent-dashboard.html`
- `docs/aiwerk/mockups/README.md`

Action:
- Treat the HTML as visual reference only.
- Do not wire production logic into the standalone HTML.

Verification:
- Mockup remains viewable and unchanged unless explicitly redesigning.

### 2. Add a real AIWerk Customer UI route in the React app

Status: implemented as an assistant-mode top-level surface rather than a normal admin sidebar route.

Files:
- Created `web/src/pages/AiwerkAssistantPage.tsx`
- Modified `web/src/App.tsx`
- Modified `web/src/lib/dashboard-flags.ts`

Action completed:
- Added a simplified AIWerk Customer UI surface.
- Kept it separate from the existing admin ChatPage terminal UI.
- Assistant mode renders this surface directly and avoids mounting the admin sidebar/plugin/config app.

Verification:
- `npm run build` succeeds.
- Page opens through the built-in dashboard server.

### 3. Reuse the existing backend session token

Files:
- Use existing `web/src/lib/api.ts`
- Use injected `window.__HERMES_SESSION_TOKEN__`

Action:
- Do not invent a new auth layer for local MVP.
- Use the same token mechanism already protecting `/api/*` and WebSockets.

Verification:
- Direct file-open of frontend does not work.
- Page works only when served by Hermes dashboard.

### 4. Connect chat through structured `/api/ws`

Files:
- Reuse `web/src/lib/gatewayClient.ts`
- New page: `web/src/pages/AiwerkAssistantPage.tsx`

Action:
- Instantiate `GatewayClient`.
- Connect to `/api/ws?token=...` through the existing helper.
- Create or resume a session through gateway JSON-RPC.
- Submit user prompts through gateway JSON-RPC.
- Render streaming assistant text from `message.delta` and final state from `message.complete`.

Verification:
- A browser message reaches the local Hermes agent.
- Streaming text appears in the custom UI, not as terminal output.
- Session is written to normal Hermes session storage.

### 5. Render operational events as cards

Files:
- New small components under `web/src/components/aiwerk/` if needed

Events:
- `tool.start`
- `tool.progress`
- `tool.complete`
- `approval.request`
- `clarify.request`
- `status.update`

Action:
- Keep customer UI simple.
- Tool events become compact status rows.
- Approval requests become explicit approve/deny cards.
- Clarify requests become question cards.

Verification:
- Tool calls do not dump raw internal noise into the chat stream.
- Approval/clarify requests are visible and actionable.

### 6. Add minimal session history

Files:
- Use existing `api.getSessions()` and `api.getSessionMessages()` from `web/src/lib/api.ts`
- New page state in `AiwerkAssistantPage.tsx`

Action:
- Show recent user-facing sessions only.
- Do not show internal Claude Code or memory-classifier noise.
- Reuse existing session API instead of a new database query.

Verification:
- Recent sessions load.
- Selecting a session loads messages.
- Internal noise is hidden.

### 7. Add customer mode feature gating

Status: first backend-gated assistant mode implemented.

Files:
- `web/src/App.tsx`
- `web/src/lib/dashboard-flags.ts`
- `hermes_cli/main.py`
- `hermes_cli/web_server.py`

Action completed:
- Added `hermes dashboard --assistant`.
- Injects `window.__HERMES_DASHBOARD_MODE__="assistant"` into the frontend.
- Enables the structured chat gateway in assistant mode.
- Blocks non-allowlisted admin HTTP APIs server-side in assistant mode.

Hidden/blocked from assistant mode:
- Config
- Env
- Logs
- Plugins
- Profiles
- raw Skills
- raw Cron
- admin model/provider controls unless explicitly allowlisted later

Verification:
- Customer route has no direct navigation to dangerous admin pages.
- Admin UI remains available for operator mode.

### 8. Build and run locally

Commands:
- `cd $HOME/.hermes/hermes-agent/web && npm run build`
- Admin/operator: `cd $HOME/.hermes/hermes-agent && python -m hermes_cli.main dashboard --port 9119 --no-open --tui`
- AIWerk Customer UI: `cd $HOME/.hermes/hermes-agent && python -m hermes_cli.main dashboard --assistant --port 9120 --no-open --skip-build`

Verification performed:
- Open `http://127.0.0.1:9120`
- `/api/status` returns OK in assistant mode.
- `/api/env`, `/api/config`, and `/api/logs` return 404 in assistant mode.
- Root HTML injects assistant mode and embedded chat mode.
- `/api/ws` accepts the injected token and `session.create` succeeds.

### 9. Add focused tests

Likely tests:
- TypeScript build
- targeted ESLint for changed frontend files
- focused Python dashboard assistant-mode and API-gating tests

Verification performed:
- `npm run build`
- `npx eslint src/App.tsx src/pages/AiwerkAssistantPage.tsx src/lib/dashboard-flags.ts --quiet`
- `.venv/bin/python -m pytest tests/hermes_cli/test_dashboard_lifecycle_flags.py tests/hermes_cli/test_web_server.py::TestWebServerEndpoints::test_assistant_mode_allows_chat_safe_api_and_blocks_admin_api -q -o 'addopts='`

Known note:
- Full `npm run lint` still has unrelated pre-existing frontend lint errors outside the changed files.

### 10. VPS/customer-agent promotion later

Do only after local MVP works.

Rules:
- one isolated Hermes home/profile per customer agent
- dashboard backend behind protected reverse proxy
- AIWerk CUI route only exposed by default
- admin routes AIWerk/operator controlled
- no credentials or customer private data in shared runtime/wiki

Promotion path:
local prototype -> AIWerk/hermes-agent runtime branch -> base-agent template -> staged VPS customer agents.

## First build slice

The first safe implementation slice is:

1. Create `web/src/pages/AiwerkAssistantPage.tsx` with static layout matching the mockup.
2. Add the route in `web/src/App.tsx`.
3. Connect `GatewayClient` and show connection status only.
4. Build and verify.
5. Then add prompt submit and streaming render.

This avoids mixing visual migration, gateway protocol, session history, and approvals in one risky change.
