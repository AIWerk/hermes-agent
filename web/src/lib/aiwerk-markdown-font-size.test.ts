import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const repoRoot = resolve(__dirname, "../..");

function readRuntimeFile(path: string): string {
  return readFileSync(resolve(repoRoot, path), "utf8");
}

describe("AIWerk CUI markdown font-size invariant", () => {
  it("forces markdown paragraphs and lists to inherit the chat bubble size", () => {
    const sources = [
      readRuntimeFile("src/pages/AiwerkAssistantPage.tsx"),
      readRuntimeFile("src/index.css"),
    ].join("\n");

    for (const selector of [
      ".aiwerk-message-markdown > div",
      ".aiwerk-message-markdown p",
      ".aiwerk-message-markdown ul",
      ".aiwerk-message-markdown ol",
      ".aiwerk-message-markdown li",
    ]) {
      expect(sources).toContain(selector);
    }

    expect(sources).toMatch(/font-size:\s*inherit\s*!important/);
    expect(sources).toMatch(/line-height:\s*inherit\s*!important/);
  });
});
