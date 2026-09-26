import { describe, expect, it } from "vitest";

import {
  activeProfileFromAuth,
  assistantDocumentTitle,
} from "./cui-greeting";

describe("profile-scoped CUI branding", () => {
  it("uses server-resolved active profile before the default profile", () => {
    expect(activeProfileFromAuth({ active_profile: "lumo", default_profile: "default" })).toBe("lumo");
    expect(activeProfileFromAuth({ default_profile: "susanne" })).toBe("susanne");
    expect(activeProfileFromAuth(null)).toBeUndefined();
  });

  it("updates the customer browser title from the resolved profile agent", () => {
    expect(assistantDocumentTitle(" Lumo ")).toBe("Lumo AI Assistant");
    expect(assistantDocumentTitle(" ")).toBe("AI Assistant");
  });
});