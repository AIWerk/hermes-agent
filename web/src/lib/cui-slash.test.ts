import { describe, expect, it } from "vitest";

import { CUI_SUPPORTED_SLASH_COMMANDS, formatCuiUsage, isCuiSlashInput, slashBase } from "./cui-slash";

describe("CUI slash helpers", () => {
  it("recognizes slash input before it can be sent as an agent prompt", () => {
    expect(isCuiSlashInput("/usage")).toBe(true);
    expect(isCuiSlashInput("  /STATUS now ")).toBe(true);
    expect(isCuiSlashInput("please run /usage")).toBe(false);
  });

  it("normalizes the slash base command", () => {
    expect(slashBase(" /Usage detailed ")).toBe("/usage");
    expect(slashBase("hello")).toBe("");
  });

  it("keeps the CUI autocomplete limited to commands with safe native handlers", () => {
    expect(CUI_SUPPORTED_SLASH_COMMANDS.has("/usage")).toBe(true);
    expect(CUI_SUPPORTED_SLASH_COMMANDS.has("/help")).toBe(true);
    expect(CUI_SUPPORTED_SLASH_COMMANDS.has("/stop")).toBe(true);
    expect(CUI_SUPPORTED_SLASH_COMMANDS.has("/model")).toBe(false);
    expect(CUI_SUPPORTED_SLASH_COMMANDS.has("/tools")).toBe(false);
  });

  it("formats session usage without needing the agent to interpret /usage", () => {
    const rendered = formatCuiUsage({
      calls: 2,
      input: 1000,
      output: 250,
      total: 1250,
      context_used: 4000,
      context_max: 10000,
      context_percent: 40,
      credits_lines: ["Grant: ok"]
    });
    expect(rendered).toContain("API calls: 2");
    expect(rendered).toMatch(/Input tokens: 1[,’']000/);
    expect(rendered).toMatch(/Context: 4[,’']000 \/ 10[,’']000 \(40%\)/);
    expect(rendered).toContain("Grant: ok");
  });
});
