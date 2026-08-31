export type CuiGreetingContext = "customer" | "admin" | "unknown";

export function resolveGreetingName(
  authenticatedName: string | null | undefined,
  context: CuiGreetingContext,
): string | null {
  if (context !== "unknown" && authenticatedName) return authenticatedName;
  return null;
}

export function buildWelcomeMessage(
  displayName: string | null | undefined,
  context: CuiGreetingContext,
): string {
  const greetingName = displayName ? ` ${displayName}` : "";
  if (context === "admin") {
    return `Hallo${greetingName}. Du bist im Admin-/Support-Kontext. Wobei soll ich helfen?`;
  }
  if (context === "customer") {
    return `Hallo${greetingName}, schön, dass du da bist. Was soll ich heute für dich erledigen?`;
  }
  return "Hallo, schön, dass du da bist. Was soll ich heute für dich erledigen?";
}

export function withAuthenticatedWelcome<T>(
  messages: readonly T[],
  createWelcome: () => T,
): T[] {
  return messages.length > 0 ? [...messages] : [createWelcome()];
}
