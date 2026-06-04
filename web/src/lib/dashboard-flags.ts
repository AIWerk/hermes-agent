declare global {
  interface Window {
    /**
     * Injected by the server as `true`. The embedded TUI Chat surface
     * (`/chat`, `/api/ws`, `/api/pty`) is always enabled, so this is
     * effectively a constant; kept on `window` for any consumer that reads
     * it directly and for parity with the server's bootstrap script.
     */
    __HERMES_DASHBOARD_EMBEDDED_CHAT__?: boolean;
    /** Dashboard surface mode injected by the backend. */
    __HERMES_DASHBOARD_MODE__?: "admin" | "assistant" | string;
    /** Sanitized first-name style display label injected by the backend for customer UI personalization. */
    __HERMES_USER_DISPLAY_NAME__?: string | null;
    /** @deprecated Older injected name; treated as on when true. */
    __HERMES_DASHBOARD_TUI__?: boolean;
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

/** True when the backend serves the restricted AIWerk assistant surface. */
export function isAssistantDashboardMode(): boolean {
  if (typeof window === "undefined") return false;
  return window.__HERMES_DASHBOARD_MODE__ === "assistant";
}

export function getHermesUserDisplayName(): string | null {
  if (typeof window === "undefined") return null;
  const value = window.__HERMES_USER_DISPLAY_NAME__;
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}
