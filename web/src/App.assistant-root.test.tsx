import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it } from "vitest";

import { DashboardRoot, dashboardDocumentTitle } from "./App";


describe("DashboardRoot", () => {
  const realWindow = (globalThis as { window?: unknown }).window;

  afterEach(() => {
    if (realWindow === undefined) delete (globalThis as { window?: unknown }).window;
    else (globalThis as { window?: unknown }).window = realWindow;
  });

  it("renders only the customer assistant root in assistant mode", () => {
    (globalThis as { window?: unknown }).window = {
      __HERMES_DASHBOARD_MODE__: "assistant",
    };

    const html = renderToStaticMarkup(<DashboardRoot />);

    expect(html).toContain('data-aiwerk-surface="assistant"');
    expect(html).not.toContain("app-sidebar");
  });

  it("uses agent branding only for the assistant surface", () => {
    (globalThis as { window?: unknown }).window = {
      __HERMES_DASHBOARD_MODE__: "assistant",
      __HERMES_AGENT_DISPLAY_NAME__: "  Theo  ",
    };
    expect(dashboardDocumentTitle()).toBe("Theo AI Assistant");

    (globalThis as { window?: unknown }).window = {
      __HERMES_DASHBOARD_MODE__: "admin",
      __HERMES_AGENT_DISPLAY_NAME__: "Theo",
    };
    expect(dashboardDocumentTitle()).toBe("Assistant Dashboard");
  });
});
