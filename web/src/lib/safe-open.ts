// Shared helper for opening external/server-supplied URLs in a new tab.
//
// Several call sites in the customer UI pass URLs sourced from the
// /api/assistant/resources backend (cloud_url, connector.open_url,
// shared-folder open_url) directly to window.open. To keep scheme validation
// consistent and defend against a future change to the resource source turning
// this into a javascript:/data: injection point, every external open must go
// through safeWindowOpen.
//
// Allowed: http:, https:, mailto:, and dashboard-relative paths ("/..." or
// "./..."). Blocked: javascript:, data:, vbscript:, blob:, file:, and anything
// that does not resolve to one of the allowed schemes.

/**
 * Returns the URL when it is safe to pass to window.open, otherwise null.
 * Pure and DOM-free so it can be unit-tested in a node context.
 */
export function safeExternalUrl(raw?: string | null): string | null {
  if (typeof raw !== "string") return null;
  const url = raw.trim();
  if (!url) return null;

  // Reject embedded control characters / whitespace that can be used to smuggle
  // a scheme past naive checks (e.g. "java\tscript:" or "javascript\n:..."). A
  // single bounded character class (U+0000..U+0020) — no nested quantifiers,
  // ReDoS-free.
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u0020]/.test(url)) return null;

  // Relative paths (no scheme) are same-origin and safe.
  if (/^\.?\/[^/\\]/.test(url) || url === "/") return url;
  // Protocol-relative URLs ("//host/...") inherit the page scheme (http/https).
  if (/^\/\/[^/\\]/.test(url)) return url;

  const schemeMatch = /^([a-z][a-z0-9+.-]*):/i.exec(url);
  if (schemeMatch) {
    const scheme = schemeMatch[1].toLowerCase();
    if (scheme === "http" || scheme === "https" || scheme === "mailto") return url;
    return null;
  }

  // No scheme and not a recognized relative form: reject to be safe.
  return null;
}

/**
 * Opens a validated external URL in a new tab with noopener/noreferrer and
 * nulls the opener. Returns the opened window (or null when blocked/rejected).
 */
export function safeWindowOpen(raw?: string | null): Window | null {
  const url = safeExternalUrl(raw);
  if (!url) return null;
  const opened = window.open(url, "_blank", "noopener,noreferrer");
  if (opened) opened.opener = null;
  return opened;
}
