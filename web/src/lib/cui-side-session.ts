export interface SideSessionBackResult {
  parent_session_id?: string;
}

export async function runSideSessionBackWithParentRestore(
  rememberedParentSessionId: string | null,
  requestBack: () => Promise<SideSessionBackResult>,
  restoreParentSessionId: (sessionId: string) => void,
): Promise<SideSessionBackResult> {
  let result: SideSessionBackResult | undefined;
  try {
    result = await requestBack();
    return result;
  } finally {
    const parentSessionId = rememberedParentSessionId?.trim() || result?.parent_session_id?.trim();
    if (parentSessionId) restoreParentSessionId(parentSessionId);
  }
}
