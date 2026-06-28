import { describe, expect, it } from "vitest";
import { JSDOM } from "jsdom";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const repoRoot = resolve(__dirname, "../..");

function readRuntimeFile(path: string): string {
  return readFileSync(resolve(repoRoot, path), "utf8");
}

// The chat bubble sets the markdown container font-size (Tailwind text-[15px] /
// compact text-[14px]). The .aiwerk-message-markdown rule must force every
// rendered paragraph/list node back to `inherit !important` so the dashboard's
// global markdown utilities (which otherwise set their own sizes) cannot shrink
// or enlarge assistant text. We assert this against the real index.css via the
// computed style of a rendered node, not a source-string grep.
describe("AIWerk CUI markdown font-size invariant", () => {
  it("forces markdown paragraphs and lists to inherit the chat bubble size at runtime", () => {
    const css = readRuntimeFile("src/index.css");
    // A competing global rule that would shrink markdown text if the !important
    // override were missing. Mirrors how the dashboard markdown utilities apply
    // their own sizing.
    const competingCss = `
      .markdown-global p,
      .markdown-global ul,
      .markdown-global ol,
      .markdown-global li,
      .markdown-global div { font-size: 11px; line-height: 1; }
    `;
    const bubbleSize = "15px";
    const dom = new JSDOM(
      `<!DOCTYPE html><html><head><style>${css}\n${competingCss}</style></head>` +
        `<body><div class="markdown-global aiwerk-message-markdown" style="font-size: ${bubbleSize}; line-height: 1.6;">` +
        `<div><p>paragraph</p><ul><li>item</li></ul><ol><li>num</li></ol></div>` +
        `</div></body></html>`,
    );
    const { window } = dom;
    const container = window.document.querySelector(".aiwerk-message-markdown")!;
    const wrapper = container.querySelector(":scope > div")!;
    const paragraph = container.querySelector("p")!;
    const list = container.querySelector("ul")!;
    const listItem = container.querySelector("li")!;

    // Despite the competing 11px rule, every targeted node resolves to the
    // bubble's inherited size and carries the !important priority.
    for (const node of [wrapper, paragraph, list, listItem]) {
      const computed = window.getComputedStyle(node);
      expect(computed.getPropertyValue("font-size")).toBe(bubbleSize);
      expect(computed.getPropertyPriority("font-size")).toBe("important");
      expect(computed.getPropertyPriority("line-height")).toBe("important");
    }

    window.close();
  });

  it("keeps the inherit !important rule wired to the markdown selectors", () => {
    const css = readRuntimeFile("src/index.css");
    for (const selector of [
      ".aiwerk-message-markdown > div",
      ".aiwerk-message-markdown p",
      ".aiwerk-message-markdown ul",
      ".aiwerk-message-markdown ol",
      ".aiwerk-message-markdown li",
    ]) {
      expect(css).toContain(selector);
    }
    expect(css).toMatch(/font-size:\s*inherit\s*!important/);
    expect(css).toMatch(/line-height:\s*inherit\s*!important/);
    // The container is rendered with the .aiwerk-message-markdown class.
    expect(readRuntimeFile("src/pages/AiwerkAssistantPage.tsx")).toContain("aiwerk-message-markdown");
  });
});
