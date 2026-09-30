# AIWerk Customer UI integration plan

Goal: turn `hermes-agent-dashboard.html` from a static mockup into a real chat surface for the local or VPS-hosted Hermes agent.

## Existing Hermes surfaces

Hermes already has a dashboard backend in `hermes_cli/web_server.py`.

Important endpoints:

- `GET /api/status` — read-only status.
- `GET /api/sessions` — session list.
- `GET /api/sessions/{id}/messages` — prior messages.
- `WS /api/pty?token=...&channel=...&resume=...` — PTY bridge. Spawns the same `hermes --tui` path and renders it as a terminal stream.
- `WS /api/ws?token=...` — structured JSON-RPC gateway using `tui_gateway.ws.handle_ws`.
- `WS /api/pub` and `WS /api/events` — event sidecar for tool/status/approval events linked to a chat tab channel.

The React client already contains `web/src/lib/gatewayClient.ts`, which can connect to `/api/ws` and submit JSON-RPC requests.

## Best integration path for AIWerk Customer UI

The customer-facing AIWerk CUI must present visible UI copy in German by default for German-speaking Swiss SME users.

Do not build the first real customer chat by shelling out manually from the mockup.
Use the existing Hermes dashboard server and replace or add a simpler customer-facing React route.

Recommended path:

1. Keep the current static HTML as a design reference.
2. Create a new React page under `web/src/pages/` for AIWerk Customer UI chat.
3. Reuse the existing dashboard server, session token injection, auth middleware, and WebSocket endpoints.
4. Use the structured JSON-RPC `GatewayClient` path for native chat UI where possible:
   - connect to `/api/ws`
   - create or resume a session
   - submit prompts
   - render `message.delta`, `message.complete`, `tool.start`, `tool.complete`, `approval.request`, `clarify.request`, and `status.update`
5. Keep `/api/pty` as fallback or advanced/operator mode, not the main customer chat UI.
6. Add a restricted customer/user mode that hides admin surfaces by default.

## Why not PTY first

`/api/pty` works today and is useful because it embeds the real TUI in a browser. But it is terminal-shaped:

- good for operator/debug/power-user use
- weaker for polished customer UI
- harder to style as normal chat bubbles/cards
- harder to restrict controls cleanly

For the AIWerk Customer UI, short AIWerk CUI, structured events are the better base.

## Minimal MVP

First living version should have:

- chat input
- streaming assistant response
- session title and recent session list
- file attachment placeholder, later real upload
- approval card rendering from `approval.request`
- simple status badge
- no Skills, Memory, Logs, Env, Config, Cron, Plugins, Profiles, or raw workspace browser in customer mode

## Current implementation status

Implemented in this repo:

- `hermes dashboard --assistant` starts the restricted AIWerk Customer UI surface.
- `9119` can remain the full built-in admin/operator dashboard.
- `9120` can serve the simplified AIWerk CUI.
- The AIWerk CUI uses the existing injected dashboard session token.
- Chat connects through the structured `/api/ws` gateway, not the PTY terminal bridge.
- Backend assistant mode blocks admin HTTP APIs such as `/api/env`, `/api/config`, and `/api/logs` with 404.

Implemented files:

- `web/src/pages/AiwerkAssistantPage.tsx`
- `web/src/App.tsx`
- `web/src/lib/dashboard-flags.ts`
- `hermes_cli/main.py`
- `hermes_cli/web_server.py`

## Launch command for local development

Admin/operator dashboard:

```bash
cd $HOME/.hermes/hermes-agent
python -m hermes_cli.main dashboard --port 9119 --no-open --tui
```

AIWerk Customer UI:

```bash
cd $HOME/.hermes/hermes-agent
python -m hermes_cli.main dashboard --assistant --port 9120 --no-open --skip-build
```

Frontend rebuild:

```bash
cd $HOME/.hermes/hermes-agent/web
npm run build
```

Open `http://127.0.0.1:9120` for the AIWerk CUI surface.

## Verification performed

- `npm run build` succeeds.
- Focused Python tests for assistant mode and API gating pass.
- Targeted ESLint on changed frontend files passes.
- Smoke server on `127.0.0.1:9120` returns `/api/status`.
- In assistant mode `/api/env`, `/api/config`, and `/api/logs` return 404.
- Root HTML injects assistant mode and embedded chat mode.
- WebSocket `/api/ws` accepts the injected token and `session.create` succeeds.

Known note: full `npm run lint` still reports pre-existing lint errors in unrelated frontend files.

## VPS deployment shape

For each VPS-hosted user/customer agent:

- run one isolated Hermes home/profile per agent
- run the dashboard backend bound to loopback or behind a protected reverse proxy
- expose only the restricted AIWerk CUI route
- keep admin/config/secret/setup routes AIWerk-controlled
- preserve tenant memory/config/secrets/session isolation

Promotion path:

local Hermes prototype → AIWerk/hermes-agent runtime/UI capability → AIWerk/base-agent lock/config/docs/verifier → staged VPS agent rollout.
