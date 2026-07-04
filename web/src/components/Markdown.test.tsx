import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { Markdown } from "./Markdown";

describe("Markdown", () => {
  it("renders an incomplete streaming heading marker without hanging", () => {
    const html = renderToStaticMarkup(<Markdown content={"Antwort\n\n### "} streaming />);
    expect(html).toContain("###");
  });
});
