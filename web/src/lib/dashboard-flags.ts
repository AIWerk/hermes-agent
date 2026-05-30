declare global {
  interface Window {
    /** Set true by the server only for `hermes dashboard --tui` (or HERMES_DASHBOARD_TUI=1). */
    __HERMES_DASHBOARD_EMBEDDED_CHAT__?: boolean;
    /** Dashboard surface mode injected by the backend. */
    __HERMES_DASHBOARD_MODE__?: "admin" | "assistant" | string;
    /** Sanitized first-name style display label injected by the backend for customer UI personalization. */
    __HERMES_USER_DISPLAY_NAME__?: string | null;
    /** @deprecated Older injected name; treated as on when true. */
    __HERMES_DASHBOARD_TUI__?: boolean;
  }
}

/** True only when the dashboard was started with embedded TUI Chat (`hermes dashboard --tui`). */
export function isDashboardEmbeddedChatEnabled(): boolean {
  if (typeof window === "undefined") return false;
  if (window.__HERMES_DASHBOARD_EMBEDDED_CHAT__ === true) return true;
  return window.__HERMES_DASHBOARD_TUI__ === true;
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
