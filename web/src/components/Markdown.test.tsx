import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { Markdown } from "./Markdown";

function render(content: string, streaming = false): string {
  return renderToStaticMarkup(<Markdown content={content} streaming={streaming} />);
}

describe("Markdown streaming progress", () => {
  it.each(["#", "# ", "##", "##  ", "###", "###\t", "####", "####  "])(
    "renders an incomplete heading line %j once as literal text",
    (marker) => {
      const html = render(marker, true);

      expect(html).toContain(`<p>${marker.trimEnd()}`);
      expect(html.match(/<p>/g)).toHaveLength(1);
      expect(html).not.toMatch(/<h[1-4]/);
      expect(html.match(/#+/g)).toEqual([marker.trim()]);
    },
  );
});

describe("Markdown heading hierarchy", () => {
  it.each([
    [1, "text-lg font-bold leading-snug"],
    [2, "text-base font-bold leading-snug"],
    [3, "text-[15px] font-semibold leading-snug"],
    [4, "text-sm font-medium leading-snug"],
  ])("renders a completed h%i with a distinct compact scale", (level, classes) => {
    const html = render(`${"#".repeat(level)} Heading`);

    expect(html).toContain(`<h${level} class="${classes}">Heading</h${level}>`);
  });
});
