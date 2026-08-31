import { afterEach, describe, expect, it } from "vitest";
import { readConfiguredCuiLocale } from "./aiwerk-cui-i18n";

type Win = { __AIWERK_CUI_LOCALE__?: string };

describe("readConfiguredCuiLocale", () => {
  const realWindow = (globalThis as { window?: unknown }).window;

  afterEach(() => {
    if (realWindow === undefined) {
      delete (globalThis as { window?: unknown }).window;
    } else {
      (globalThis as { window?: unknown }).window = realWindow;
    }
  });

  it("falls back to 'de' in a non-DOM context (SSR/node) without throwing", () => {
    delete (globalThis as { window?: unknown }).window;
    expect(readConfiguredCuiLocale()).toBe("de");
  });

  it("reads and normalizes the configured locale from window", () => {
    const setLocale = (value?: string) => {
      (globalThis as { window?: Win }).window = value === undefined ? {} : { __AIWERK_CUI_LOCALE__: value };
    };

    setLocale("HU");
    expect(readConfiguredCuiLocale()).toBe("hu");

    setLocale("magyar");
    expect(readConfiguredCuiLocale()).toBe("hu");

    setLocale("zh_TW");
    expect(readConfiguredCuiLocale()).toBe("zh-hant");

    setLocale("de-CH.UTF-8");
    expect(readConfiguredCuiLocale()).toBe("de");

    setLocale("klingon");
    expect(readConfiguredCuiLocale()).toBe("de");

    setLocale(undefined);
    expect(readConfiguredCuiLocale()).toBe("de");
  });
});
