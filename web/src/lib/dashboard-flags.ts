declare global {
  interface Window {
    /**
     * Injected by the server as `true`. The embedded TUI Chat surface
     * (`/chat`, `/api/ws`, `/api/pty`) is always enabled, so this is
     * effectively a constant; kept on `window` for any consumer that reads
     * it directly and for parity with the server's bootstrap script.
     */
    __HERMES_DASHBOARD_EMBEDDED_CHAT__?: boolean;
    /** Server-owned dashboard surface; browser values are not authority. */
    __HERMES_DASHBOARD_MODE__?: "admin" | "assistant";
    /** Hidden customer locale injected by tenant/profile configuration. */
    __AIWERK_CUI_LOCALE__?: string;
    /** Sanitized customer-facing agent label injected for browser-title branding. */
    __HERMES_AGENT_DISPLAY_NAME__?: string | null;
  }
}

/**
 * Whether the dashboard's embedded TUI Chat surface is available.
 *
 * The embedded chat (`/chat` tab, `/api/ws` + `/api/pty` WebSockets) is now
 * an unconditional part of the dashboard — the desktop app and the in-browser
 * Chat tab both depend on it — so this always returns `true`. The function is
 * retained as a stable seam so call sites don't need to change if the surface
 * ever becomes conditional again.
 */
export function isDashboardEmbeddedChatEnabled(): boolean {
  return true;
}

export function isAssistantDashboardMode(): boolean {
  return typeof window !== "undefined" && window.__HERMES_DASHBOARD_MODE__ === "assistant";
}

export function getHermesAgentDisplayName(): string | null {
  if (typeof window === "undefined") return null;
  const value = window.__HERMES_AGENT_DISPLAY_NAME__;
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}
