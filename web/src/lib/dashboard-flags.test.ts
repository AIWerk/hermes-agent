import { afterEach, describe, expect, it } from "vitest";

import { getHermesUserDisplayName, isAssistantDashboardMode } from "./dashboard-flags";


describe("assistant dashboard mode", () => {
  const realWindow = (globalThis as { window?: unknown }).window;

  afterEach(() => {
    if (realWindow === undefined) delete (globalThis as { window?: unknown }).window;
    else (globalThis as { window?: unknown }).window = realWindow;
  });

  it("selects the customer root only for the exact server-injected assistant mode", () => {
    (globalThis as { window?: unknown }).window = { __HERMES_DASHBOARD_MODE__: "assistant" };
    expect(isAssistantDashboardMode()).toBe(true);

    (globalThis as { window?: unknown }).window = { __HERMES_DASHBOARD_MODE__: "admin" };
    expect(isAssistantDashboardMode()).toBe(false);

    (globalThis as { window?: unknown }).window = { __HERMES_DASHBOARD_MODE__: "spoofed" };
    expect(isAssistantDashboardMode()).toBe(false);
  });

  it("returns only a trimmed injected user display name", () => {
    (globalThis as { window?: unknown }).window = { __HERMES_USER_DISPLAY_NAME__: "  Kata  " };
    expect(getHermesUserDisplayName()).toBe("Kata");

    (globalThis as { window?: unknown }).window = { __HERMES_USER_DISPLAY_NAME__: "   " };
    expect(getHermesUserDisplayName()).toBeNull();
  });
});
