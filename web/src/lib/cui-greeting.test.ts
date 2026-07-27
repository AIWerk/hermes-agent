import { describe, expect, it } from "vitest";

import { buildWelcomeMessage, resolveGreetingName } from "./cui-greeting";


describe("buildWelcomeMessage", () => {
  it("greets a customer by authenticated first name", () => {
    expect(buildWelcomeMessage("Example", "customer")).toBe(
      "Hallo Example, schön, dass du da bist. Was soll ich heute für dich erledigen?",
    );
  });

  it("labels the admin support context without using a customer identity", () => {
    expect(buildWelcomeMessage("Operator", "admin")).toBe(
      "Hallo Operator. Du bist im Admin-/Support-Kontext. Wobei soll ich helfen?",
    );
  });

  it("uses a neutral greeting when actor identity is unknown", () => {
    expect(buildWelcomeMessage("Customer", "unknown")).toBe(
      "Hallo, schön, dass du da bist. Was soll ich heute für dich erledigen?",
    );
  });

  it("never falls back to configured identity without an authenticated name", () => {
    expect(resolveGreetingName(null, "admin")).toBeNull();
    expect(resolveGreetingName(null, "unknown")).toBeNull();
    expect(resolveGreetingName(null, "customer")).toBeNull();
  });
});
