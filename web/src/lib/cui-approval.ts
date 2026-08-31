export function buildApprovalResponseParams(
  sessionId: string,
  requestId: string,
  approved: boolean,
) {
  return {
    session_id: sessionId,
    request_id: requestId,
    choice: approved ? "once" : "deny",
  };
}
