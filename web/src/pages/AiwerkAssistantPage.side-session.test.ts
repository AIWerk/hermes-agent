import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it, vi } from "vitest";

import { runSideSessionBackWithParentRestore } from "@/lib/cui-side-session";

describe("side-session active-session persistence", () => {
  it("restores the remembered parent when session.side.back returns no parent", async () => {
    let storedActiveSessionId = "side-session";
    const restore = vi.fn((sessionId: string) => {
      storedActiveSessionId = sessionId;
    });

    await runSideSessionBackWithParentRestore(
      "parent-session",
      async () => ({}),
      restore,
    );

    expect(restore).toHaveBeenCalledExactlyOnceWith("parent-session");
    expect(storedActiveSessionId).toBe("parent-session");
  });

  it("restores the remembered parent when session.side.back rejects", async () => {
    let storedActiveSessionId = "side-session";
    const restore = vi.fn((sessionId: string) => {
      storedActiveSessionId = sessionId;
    });

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
    expect(storedActiveSessionId).toBe("parent-session");
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
