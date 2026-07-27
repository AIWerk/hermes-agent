import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";


describe("CUI greeting load order invariant", () => {
  it("uses only the authenticated auth-session greeting in every completion order", () => {
    const page = readFileSync(
      fileURLToPath(new URL("../pages/AiwerkAssistantPage.tsx", import.meta.url)),
      "utf8",
    );

    expect(page).toContain("const authSessionRef = useRef<DashboardAuthSession | null>(null)");
    expect(page).toContain("authSessionRef.current = data");
    expect(page).toContain("welcomeMessageForAuthSession(authSessionRef.current)");
    expect(page).toContain("withAuthenticatedWelcome(");
    expect(page).toContain("() => welcomeMessageForAuthSession(authSessionRef.current)");
    expect(page).not.toContain("welcomeMessageForModelInfo");
    expect(page).not.toContain("modelInfo?.user_display_name");
    expect(page).not.toContain("modelInfo?.greeting_context");
  });
});
