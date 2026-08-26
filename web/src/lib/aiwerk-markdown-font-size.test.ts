import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const css = readFileSync(resolve(__dirname, "../index.css"), "utf8");

function ruleContaining(selector: string): string {
  const rule = css
    .match(/[^{}]+\{[^{}]*\}/g)
    ?.find((candidate) => candidate.split("{")[0].includes(selector));

  expect(rule, `missing CSS rule for ${selector}`).toBeDefined();
  return rule!;
}

describe("AIWerk message markdown typography", () => {
  it("scopes paragraph and list inheritance to the chat bubble class", () => {
    const rule = ruleContaining(".aiwerk-message-markdown p");
    const [selectorList, declarations] = rule.split("{");
    const selectors = selectorList
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .split(",")
      .map((selector) => selector.trim());

    expect(selectors).toEqual(
      expect.arrayContaining([
        ".aiwerk-message-markdown > div",
        ".aiwerk-message-markdown p",
        ".aiwerk-message-markdown ul",
        ".aiwerk-message-markdown ol",
        ".aiwerk-message-markdown li",
      ]),
    );
    expect(selectors.every((selector) => selector.startsWith(".aiwerk-message-markdown"))).toBe(true);
    expect(declarations).toMatch(/font-size:\s*inherit\s*!important/);
    expect(declarations).toMatch(/line-height:\s*inherit\s*!important/);
  });

  it("does not add unscoped paragraph or list typography rules", () => {
    const typographyRules = css.match(/[^{}]+\{[^{}]*(?:font-size|line-height):[^{}]*\}/g) ?? [];
    const unscopedListRule = typographyRules.find((rule) => {
      const selectors = rule.split("{")[0].split(",").map((selector) => selector.trim());
      return selectors.some((selector) => /^(?:p|ul|ol|li)$/.test(selector));
    });

    expect(unscopedListRule).toBeUndefined();
  });
});
