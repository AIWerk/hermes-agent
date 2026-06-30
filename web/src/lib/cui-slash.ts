export const CUI_NATIVE_SLASH_COMMANDS = new Set(["/back", "/help", "/new", "/side", "/status", "/stop", "/usage"]);
export const CUI_EXEC_SLASH_COMMANDS = new Set(["/compress", "/reload-mcp"]);
export const CUI_SUPPORTED_SLASH_COMMANDS = new Set([...CUI_NATIVE_SLASH_COMMANDS, ...CUI_EXEC_SLASH_COMMANDS]);

export function slashBase(command: string): string {
  const trimmed = command.trim();
  if (!trimmed.startsWith("/")) return "";
  return (trimmed.split(/\s+/, 1)[0] || "").toLowerCase();
}

export function isCuiSlashInput(text: string): boolean {
  return Boolean(slashBase(text));
}

function numberField(payload: Record<string, unknown>, key: string): number {
  const value = Number(payload[key]);
  return Number.isFinite(value) ? value : 0;
}

function compactNumber(value: number): string {
  return value.toLocaleString();
}

function keepTogether(text: string): string {
  return text.replace(/ /g, "\u00a0");
}

export function formatCuiUsage(payload: Record<string, unknown>): string {
  const calls = numberField(payload, "calls");
  const input = numberField(payload, "input");
  const output = numberField(payload, "output");
  const reasoning = numberField(payload, "reasoning");
  const total = numberField(payload, "total");
  const contextUsed = numberField(payload, "context_used");
  const contextMax = numberField(payload, "context_max");
  const contextPercent = numberField(payload, "context_percent");
  const percent = `${Math.round(contextPercent)}%`;
  const tokenParts = [
    `Input ${compactNumber(input)}`,
    `Output ${compactNumber(output)}`,
  ];
  if (reasoning) tokenParts.push(`Reasoning ${compactNumber(reasoning)}`);
  tokenParts.push(`Total ${compactNumber(total)}`);
  const creditsLines = Array.isArray(payload.credits_lines)
    ? payload.credits_lines.filter((line): line is string => typeof line === "string" && Boolean(line.trim()))
    : [];
  const lines = [
    "Session usage",
    `API calls: ${compactNumber(calls)}`,
    `Tokens: ${tokenParts.join(" · ")}`,
  ];
  if (contextMax) {
    const contextRange = keepTogether(`${compactNumber(contextUsed)} / ${compactNumber(contextMax)}`);
    lines.push(`Context: ${contextRange} · ${percent}`);
  }
  if (creditsLines.length) lines.push("", "Nous credits", ...creditsLines);
  if (!calls && !creditsLines.length) lines.push("", "No API calls yet.");
  return lines.join("\n");
}
