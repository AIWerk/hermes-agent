// @vitest-environment jsdom
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { beforeEach, describe, expect, it, vi } from "vitest";

import { runSideSessionBackWithParentRestore } from "@/lib/cui-side-session";

const ACTIVE_SESSION_STORAGE_KEY = "aiwerk-cui.active-session-id";

function persistActiveSession(sessionId: string): void {
  window.localStorage.setItem(ACTIVE_SESSION_STORAGE_KEY, sessionId);
}

describe("side-session active-session persistence", () => {
  beforeEach(() => {
    window.localStorage.clear();
    persistActiveSession("side-session");
  });

  it("restores the remembered parent when session.side.back returns no parent", async () => {
    const restore = vi.fn(persistActiveSession);

    await runSideSessionBackWithParentRestore(
      "parent-session",
      async () => ({}),
      restore,
    );

    expect(restore).toHaveBeenCalledExactlyOnceWith("parent-session");
    expect(window.localStorage.getItem(ACTIVE_SESSION_STORAGE_KEY)).toBe("parent-session");
  });

  it("restores the remembered parent when session.side.back rejects", async () => {
    const restore = vi.fn(persistActiveSession);

    await expect(
      runSideSessionBackWithParentRestore(
        "parent-session",
        async () => {
          throw new Error("side back failed");
        },
        restore,
      ),
    ).rejects.toThrow("side back failed");

    expect(restore).toHaveBeenCalledExactlyOnceWith("parent-session");
    expect(window.localStorage.getItem(ACTIVE_SESSION_STORAGE_KEY)).toBe("parent-session");
  });

  it("wires the close-button toggle through the remembered-parent restore path", () => {
    const page = readFileSync(
      resolve(process.cwd(), "src/pages/AiwerkAssistantPage.tsx"),
      "utf8",
    );
    const start = page.slice(
      page.indexOf("const startSideSession"),
      page.indexOf("const returnFromSideSession"),
    );
    const back = page.slice(
      page.indexOf("const returnFromSideSession"),
      page.indexOf("const sendSlash"),
    );
    const toggle = page.slice(
      page.indexOf("const toggleConversationMode"),
      page.indexOf("const resolveApproval"),
    );

    expect(start).toContain("sideParentSessionIdRef.current = activeSessionKeyRef.current || sessionId");
    expect(back).toContain("runSideSessionBackWithParentRestore(");
    expect(back).toContain("storeActiveSessionId(parentSessionId)");
    expect(toggle).toContain('if (conversationMode === "side")');
    expect(toggle).toContain("void returnFromSideSession()");
    expect(page).toContain("onClick={toggleConversationMode}");
  });
});
