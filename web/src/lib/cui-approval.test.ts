import { describe, expect, it } from "vitest";

import { buildApprovalResponseParams } from "./cui-approval";


describe("assistant approval response contract", () => {
  it("maps customer confirmation to the gateway once choice", () => {
    expect(buildApprovalResponseParams("sid-1", "req-1", true)).toEqual({
      session_id: "sid-1",
      request_id: "req-1",
      choice: "once",
    });
  });

  it("maps customer rejection to the gateway deny choice", () => {
    expect(buildApprovalResponseParams("sid-1", "req-1", false)).toEqual({
      session_id: "sid-1",
      request_id: "req-1",
      choice: "deny",
    });
  });
});
