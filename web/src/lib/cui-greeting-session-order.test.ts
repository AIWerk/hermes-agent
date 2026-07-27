import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";


describe("CUI greeting load order invariant", () => {
  it("uses the latest actor model info for both model-info and empty-session completion orders", () => {
    const page = readFileSync(
      fileURLToPath(new URL("../pages/AiwerkAssistantPage.tsx", import.meta.url)),
      "utf8",
    );

    expect(page).toContain("const modelInfoRef = useRef<ModelInfoResponse | null>(null)");
    expect(page).toContain("modelInfoRef.current = info");
    expect(page).toContain("welcomeMessageForModelInfo(modelInfoRef.current)");
    expect(page).toContain("? [welcomeMessageForModelInfo(info)]");
  });
});
