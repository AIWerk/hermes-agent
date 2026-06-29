import { afterEach, describe, expect, it, vi } from "vitest";
import { safeExternalUrl, safeWindowOpen } from "./safe-open";

describe("safeExternalUrl", () => {
  it("allows http(s), mailto and relative URLs", () => {
    expect(safeExternalUrl("https://pass.aiwerk.ch")).toBe("https://pass.aiwerk.ch");
    expect(safeExternalUrl("http://example.com/path?q=1")).toBe("http://example.com/path?q=1");
    expect(safeExternalUrl("HTTPS://Example.com")).toBe("HTTPS://Example.com");
    expect(safeExternalUrl("mailto:hallo@aiwerk.ch")).toBe("mailto:hallo@aiwerk.ch");
    expect(safeExternalUrl("/api/assistant/file/123")).toBe("/api/assistant/file/123");
    expect(safeExternalUrl("./relative/doc.pdf")).toBe("./relative/doc.pdf");
    expect(safeExternalUrl("//cdn.aiwerk.ch/asset.png")).toBe("//cdn.aiwerk.ch/asset.png");
    expect(safeExternalUrl("  https://aiwerk.ch  ")).toBe("https://aiwerk.ch");
  });

  it("blocks javascript: scheme in any casing or spacing", () => {
    expect(safeExternalUrl("javascript:alert(1)")).toBeNull();
    expect(safeExternalUrl("JavaScript:alert(1)")).toBeNull();
    expect(safeExternalUrl("  javascript:alert(document.cookie)")).toBeNull();
    expect(safeExternalUrl("JAVASCRIPT:void(0)")).toBeNull();
  });

  it("blocks data: URLs (incl. HTML payloads)", () => {
    expect(safeExternalUrl("data:text/html,<script>alert(1)</script>")).toBeNull();
    expect(safeExternalUrl("data:text/html;base64,PHNjcmlwdD4=")).toBeNull();
    expect(safeExternalUrl("DATA:image/svg+xml,<svg onload=alert(1)>")).toBeNull();
  });

  it("blocks other dangerous / unknown schemes", () => {
    expect(safeExternalUrl("vbscript:msgbox(1)")).toBeNull();
    expect(safeExternalUrl("file:///etc/passwd")).toBeNull();
    expect(safeExternalUrl("blob:https://evil.example/uuid")).toBeNull();
    expect(safeExternalUrl("ftp://example.com")).toBeNull();
    expect(safeExternalUrl("tel:+41000")).toBeNull();
  });

  it("blocks blob: by default and without an isLocalBlob predicate", () => {
    // A server/agent-supplied blob: must never pass when no local allowlist is
    // provided.
    expect(safeExternalUrl("blob:https://app.example/abc")).toBeNull();
    expect(safeExternalUrl("blob:https://app.example/abc", {})).toBeNull();
  });

  it("only allows blob: URLs the client minted itself", () => {
    const localUrl = "blob:https://app.example/local-1234";
    const serverUrl = "blob:https://app.example/server-evil";
    const minted = new Set([localUrl]);
    const isLocalBlob = (url: string) => minted.has(url);

    // Locally minted object URL -> allowed.
    expect(safeExternalUrl(localUrl, { isLocalBlob })).toBe(localUrl);
    // Server/agent-supplied blob: (not in the local registry) -> rejected even
    // though a predicate is present.
    expect(safeExternalUrl(serverUrl, { isLocalBlob })).toBeNull();
  });

  it("blocks scheme-smuggling via embedded control characters / whitespace", () => {
    expect(safeExternalUrl("java\tscript:alert(1)")).toBeNull();
    expect(safeExternalUrl("java\nscript:alert(1)")).toBeNull();
    expect(safeExternalUrl("jav\x00ascript:alert(1)")).toBeNull();
    expect(safeExternalUrl("javascript\n:alert(1)")).toBeNull();
    expect(safeExternalUrl("https://a b.com")).toBeNull();
  });

  it("rejects empty, nullish, and bare non-scheme strings", () => {
    expect(safeExternalUrl(undefined)).toBeNull();
    expect(safeExternalUrl(null)).toBeNull();
    expect(safeExternalUrl("")).toBeNull();
    expect(safeExternalUrl("   ")).toBeNull();
    expect(safeExternalUrl("not a url")).toBeNull();
    expect(safeExternalUrl("example.com")).toBeNull();
  });

  it("treats bare root and relative-without-leading-slash carefully", () => {
    expect(safeExternalUrl("/")).toBe("/");
    // No scheme and not a recognized relative form -> rejected.
    expect(safeExternalUrl("//")).toBeNull();
    expect(safeExternalUrl("/\\evil")).toBeNull();
  });
});

describe("safeWindowOpen", () => {
  const realWindow = (globalThis as { window?: unknown }).window;

  afterEach(() => {
    if (realWindow === undefined) {
      delete (globalThis as { window?: unknown }).window;
    } else {
      (globalThis as { window?: unknown }).window = realWindow;
    }
    vi.restoreAllMocks();
  });

  it("opens validated URLs with noopener,noreferrer and nulls the opener", () => {
    const opened: { opener: unknown } = { opener: {} };
    const open = vi.fn().mockReturnValue(opened);
    (globalThis as { window?: unknown }).window = { open };

    const result = safeWindowOpen("https://pass.aiwerk.ch");

    expect(open).toHaveBeenCalledWith("https://pass.aiwerk.ch", "_blank", "noopener,noreferrer");
    expect(opened.opener).toBeNull();
    expect(result).toBe(opened);
  });

  it("never calls window.open for blocked schemes", () => {
    const open = vi.fn();
    (globalThis as { window?: unknown }).window = { open };

    expect(safeWindowOpen("javascript:alert(1)")).toBeNull();
    expect(safeWindowOpen("data:text/html,<script>")).toBeNull();
    expect(safeWindowOpen(undefined)).toBeNull();
    expect(open).not.toHaveBeenCalled();
  });

  it("opens a locally-minted blob: URL but never a server-supplied one", () => {
    const opened: { opener: unknown } = { opener: {} };
    const open = vi.fn().mockReturnValue(opened);
    (globalThis as { window?: unknown }).window = { open };

    const localUrl = "blob:https://app.example/local-1234";
    const isLocalBlob = (url: string) => url === localUrl;

    expect(safeWindowOpen(localUrl, { isLocalBlob })).toBe(opened);
    expect(open).toHaveBeenCalledWith(localUrl, "_blank", "noopener,noreferrer");
    expect(opened.opener).toBeNull();

    open.mockClear();
    expect(safeWindowOpen("blob:https://app.example/server-evil", { isLocalBlob })).toBeNull();
    expect(open).not.toHaveBeenCalled();
  });
});
