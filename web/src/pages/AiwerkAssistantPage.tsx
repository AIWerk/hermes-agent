import { CalendarDays, ChevronRight, ExternalLink, FileText, FolderOpen, Image as ImageIcon, KeyRound, LifeBuoy, ListChecks, Mail, Mic, Paperclip, Pencil, Phone, PlugZap, Plus, RefreshCw, Search, Send, Square, UserRound, Volume2, VolumeX, X } from "lucide-react";
import { Fragment, type CSSProperties, type PointerEvent as ReactPointerEvent, type ReactNode, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { Markdown } from "@/components/Markdown";
import { getHermesUserDisplayName } from "@/lib/dashboard-flags";
import { GatewayClient, type GatewayEvent } from "@/lib/gatewayClient";
import { HERMES_BASE_PATH, api, type AssistantConnectorSummary, type AssistantContactItem, type AssistantResourceEventItem, type AssistantResourcesResponse, type AssistantResourceMailItem, type AssistantResourceStatus, type AssistantSharedFolderItem, type AssistantSupportRequest, type AssistantTodoItem, type AssistantUploadedAttachment, type ModelInfoResponse } from "@/lib/api";

type ChatRole = "user" | "agent" | "system" | "tool";
type ConnectionState = "idle" | "connecting" | "open" | "closed" | "error";
type VoiceInputState = "idle" | "recording" | "transcribing";

interface AttachmentPreview {
  id: string;
  name: string;
  type: string;
  size: number;
  previewUrl?: string;
  file?: File;
  uploaded?: AssistantUploadedAttachment;
}

interface ChatMessage {
  id: string;
  role: ChatRole;
  text: string;
  status?: "streaming" | "complete" | "error";
  attachments?: AttachmentPreview[];
}

interface ToolCallSummary {
  id: string;
  toolId: string;
  anchorMessageId?: string;
  name: string;
  context?: string;
  summary?: string;
  details?: string;
  status: "running" | "done" | "error";
}

interface SessionTitleBubble {
  text: string;
  left: number;
  top: number;
  width: number;
  placement: "above" | "below";
}

interface ApprovalCard {
  id: string;
  detail: string;
}

interface RecentSession {
  id: string;
  title?: string | null;
  preview?: string | null;
}

function recentSessionDisplayTitle(session: RecentSession): string {
  const title = session.title?.trim();
  if (title) return title;
  const preview = session.preview?.trim();
  if (preview) return `Unbenannte Sitzung · ${preview}`;
  return "Unbenannte Sitzung";
}

type ResourcePanelKey = "email" | "calendar" | "shared_folder" | "vault" | "todos" | "contacts" | "connectors";
type ResourcePanelId = ResourcePanelKey | "shared";

type ResourceCardAction = { icon: ReactNode; label: string; onClick: () => void; disabled?: boolean };

function mergeAssistantResources(
  current: AssistantResourcesResponse | null,
  incoming: AssistantResourcesResponse,
  resource?: ResourcePanelKey,
): AssistantResourcesResponse {
  if (!current || !resource) return incoming;
  return {
    ...current,
    checked_at: incoming.checked_at,
    warnings: incoming.warnings,
    [resource]: incoming[resource],
    cache: incoming.cache
      ? {
          cached: incoming.cache.cached,
          resources: {
            ...(current.cache?.resources ?? {}),
            ...incoming.cache.resources,
          },
        }
      : current.cache,
  };
}

function normalizeContactSearch(value?: string | null): string {
  return (value ?? "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
}

function contactIdentityKeys(contact: Partial<Pick<AssistantContactItem, "id" | "email" | "phone" | "display_name">>): string[] {
  return [
    contact.id ? `id:${contact.id.toLowerCase()}` : "",
    contact.email ? `email:${contact.email.toLowerCase()}` : "",
    contact.phone ? `phone:${contact.phone.toLowerCase()}` : "",
    contact.display_name ? `name:${normalizeContactSearch(contact.display_name)}` : "",
  ].filter(Boolean);
}

function contactPendingKey(contact: AssistantContactItem): string {
  return contactIdentityKeys(contact)[0] ?? contact.display_name.toLowerCase();
}

function contactMatchesKeys(contact: Partial<Pick<AssistantContactItem, "id" | "email" | "phone" | "display_name">>, keys: Set<string>): boolean {
  return contactIdentityKeys(contact).some((key) => keys.has(key));
}

function dedupeContactList(contacts: AssistantContactItem[]): AssistantContactItem[] {
  const seen = new Set<string>();
  const deduped: AssistantContactItem[] = [];
  contacts.forEach((contact) => {
    const key = contact.email ? `email:${contact.email.toLowerCase()}` : contact.id ? `id:${contact.id}` : contact.display_name.toLowerCase();
    if (!key || seen.has(key)) return;
    seen.add(key);
    deduped.push(contact);
  });
  return deduped;
}

const RIGHT_RAIL_ROW_ACTION_CLASS = "grid h-auto w-[34px] shrink-0 cursor-pointer place-items-center rounded-[10px] border border-[#dfd4c4] bg-[#f8f0e3] text-[#6d5f4d] transition hover:bg-[#efe4d4]";
const RIGHT_RAIL_ROW_ACTION_DISABLED_CLASS = "disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-[#f8f0e3]";

const RESOURCE_STATUS_COPY: Record<AssistantResourceStatus, { label: string; dot: string }> = {
  connected: { label: "Verbunden", dot: "#7bcf91" },
  limited: { label: "Eingeschränkt", dot: "#d7b98e" },
  auth_required: { label: "Anmeldung nötig", dot: "#d7b98e" },
  not_configured: { label: "Nicht eingerichtet", dot: "#b7ad9c" },
  error: { label: "Fehler", dot: "#c98b7a" },
};

function resourceStatusCopy(status?: AssistantResourceStatus): { label: string; dot: string } {
  return RESOURCE_STATUS_COPY[status ?? "not_configured"] ?? RESOURCE_STATUS_COPY.not_configured;
}

function formatResourceTime(value?: string | null, options?: { year?: boolean }): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString("de-CH", {
    day: "2-digit",
    month: "2-digit",
    ...(options?.year ? { year: "numeric" as const } : {}),
    hour: "2-digit",
    minute: "2-digit",
  });
}

function openSharedFolderCloudUrl(url?: string | null): void {
  if (!url) return;
  const opened = window.open(url, "_blank", "noopener,noreferrer");
  if (opened) opened.opener = null;
}

type DocumentTabKind = "email" | "calendar" | "contact";

type PanelTabId = "chat" | string;

interface DocumentTab {
  id: string;
  kind: DocumentTabKind;
  title: string;
  subtitle?: string;
  openUrl: string;
  status: "loading" | "ready" | "error";
  html?: string;
  contact?: AssistantContactItem;
  error?: string;
}

async function fetchTokenProtectedHtml(openUrl: string): Promise<string> {
  const headers = new Headers();
  const token = window.__HERMES_SESSION_TOKEN__;
  if (token) headers.set("X-Hermes-Session-Token", token);
  const response = await fetch(`${HERMES_BASE_PATH}${openUrl}`, { headers, credentials: "include" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.text();
}

function openSharedFolderFile(item: AssistantSharedFolderItem): void {
  if (!item.open_url) return;
  const opened = window.open("about:blank", "_blank");
  if (!opened) return;
  opened.opener = null;
  opened.document.title = item.name;
  opened.document.body.textContent = "Datei wird geöffnet…";

  const headers = new Headers();
  const token = window.__HERMES_SESSION_TOKEN__;
  if (token) headers.set("X-Hermes-Session-Token", token);

  fetch(`${HERMES_BASE_PATH}${item.open_url}`, { headers, credentials: "include" })
    .then((response) => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.blob();
    })
    .then((blob) => {
      const objectUrl = URL.createObjectURL(blob);
      opened.location.href = objectUrl;
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
    })
    .catch(() => {
      opened.document.body.textContent = "Datei konnte nicht geöffnet werden.";
    });
}

interface SessionNotesSnapshot {
  title?: string | null;
  scratchpad?: {
    current_goal?: string | null;
    decisions?: unknown[];
    artifacts?: unknown[];
    open_items?: unknown[];
    candidates?: unknown[];
  } | null;
  events?: Array<{
    event_type?: string;
    content?: Record<string, unknown>;
    turn_index?: number | null;
  }>;
  summary?: { short_summary?: string | null } | null;
  event_count?: number;
}

interface RuntimeStatus {
  busyMode?: string;
  reasoningEffort?: string;
  reasoningDisplay?: string;
  fastMode?: string;
  yoloMode?: string;
}

interface ContextUsage {
  used?: number;
  max?: number;
  percent?: number;
  compressions?: number;
}

type RuntimeBadgeId = "busy" | "reasoning" | "fast" | "approvals";
type ConversationMode = "main" | "side";
const ACTIVE_SESSION_STORAGE_KEY = "aiwerk-cui.active-session-id";
const READ_ALOUD_STORAGE_KEY = "aiwerk-cui.read-aloud-enabled";
const RIGHT_RAIL_WIDTH_STORAGE_KEY = "aiwerk-cui.right-rail-width";
const RIGHT_RAIL_DEFAULT_WIDTH = 340;
const RIGHT_RAIL_MIN_WIDTH = 280;
const RIGHT_RAIL_MAX_WIDTH = 560;
const COMPRESS_CONTEXT_PERCENT_THRESHOLD = 45;

interface RuntimeBadge {
  id: RuntimeBadgeId;
  label: string;
  help: string;
}

interface GatewayHistoryMessage {
  role?: string;
  text?: string;
  content?: string | null;
  name?: string;
  tool_name?: string;
  context?: string;
}

function toolCallsFromGateway(history?: GatewayHistoryMessage[]): ToolCallSummary[] {
  return (history ?? [])
    .filter((message) => message.role === "tool")
    .map((message, index) => {
      const name = message.name?.trim() || message.tool_name?.trim() || "tool";
      const context = message.context?.trim() || message.text?.trim() || message.content?.trim() || "";
      return {
        id: newId("history-tool"),
        toolId: `history-${index}-${name}`,
        name,
        context,
        summary: "Ausgeführt",
        status: "done" as const,
      };
    });
}

interface SessionInflightTurn {
  assistant?: string;
  streaming?: boolean;
  user?: string;
}

interface SessionOpenResult {
  session_id: string;
  session_key?: string;
  resumed?: string;
  messages?: GatewayHistoryMessage[];
  info?: { session_id?: string };
  running?: boolean;
  status?: string;
  inflight?: SessionInflightTurn | null;
}

function persistentSessionIdFromOpenResult(result: SessionOpenResult): string {
  return result.resumed || result.session_key || result.info?.session_id || result.session_id || "";
}

function readStoredSessionId(): string {
  try {
    return window.localStorage.getItem(ACTIVE_SESSION_STORAGE_KEY)?.trim() || "";
  } catch {
    return "";
  }
}

function storeActiveSessionId(sessionId?: string | null): void {
  const value = sessionId?.trim();
  try {
    if (value) window.localStorage.setItem(ACTIVE_SESSION_STORAGE_KEY, value);
    else window.localStorage.removeItem(ACTIVE_SESSION_STORAGE_KEY);
  } catch {
    // Ignore storage failures; the UI still works, only reload-resume is lost.
  }
}

function readStoredReadAloudEnabled(): boolean {
  try {
    return window.localStorage.getItem(READ_ALOUD_STORAGE_KEY) === "true";
  } catch {
    return false;
  }
}

function clampRightRailWidth(value: number): number {
  return Math.max(RIGHT_RAIL_MIN_WIDTH, Math.min(RIGHT_RAIL_MAX_WIDTH, Math.round(value)));
}

function readStoredRightRailWidth(): number {
  try {
    const raw = window.localStorage.getItem(RIGHT_RAIL_WIDTH_STORAGE_KEY);
    if (!raw) return RIGHT_RAIL_DEFAULT_WIDTH;
    const stored = Number(raw);
    return Number.isFinite(stored) ? clampRightRailWidth(stored) : RIGHT_RAIL_DEFAULT_WIDTH;
  } catch {
    return RIGHT_RAIL_DEFAULT_WIDTH;
  }
}

function storeRightRailWidth(width: number): void {
  try {
    window.localStorage.setItem(RIGHT_RAIL_WIDTH_STORAGE_KEY, String(clampRightRailWidth(width)));
  } catch {
    // Ignore storage failures; resizing still works for the current page.
  }
}

function storeReadAloudEnabled(enabled: boolean): void {
  try {
    window.localStorage.setItem(READ_ALOUD_STORAGE_KEY, enabled ? "true" : "false");
  } catch {
    // Ignore storage failures; the toggle still works for the current page.
  }
}

function speechTextFromMarkdown(text: string): string {
  return text
    .replace(/```[\s\S]*?```/g, "")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/!\[[^\]]*]\([^)]*\)/g, "")
    .replace(/\[([^\]]+)]\([^)]*\)/g, "$1")
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")
    .replace(/^\s{0,3}[-*+]\s+/gm, "")
    .replace(/^\s{0,3}>\s?/gm, "")
    .replace(/[*_~]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function welcomeMessage(displayName = getHermesUserDisplayName()): ChatMessage {
  const greetingName = displayName ? ` ${displayName}` : "";
  return {
    id: "welcome",
    role: "agent",
    text: `Hallo${greetingName}, schön, dass du da bist. Was soll ich heute für dich erledigen?`,
    status: "complete",
  };
}

function messagesFromGateway(history?: GatewayHistoryMessage[]): ChatMessage[] {
  const mapped: ChatMessage[] = [];
  for (const message of history ?? []) {
    const text = message.text?.trim() || message.context?.trim() || message.content?.trim() || "";
    if (!text) continue;
    if (message.role === "user") {
      mapped.push({ id: newId("resume-user"), role: "user", text, status: "complete" });
    } else if (message.role === "assistant") {
      mapped.push({ id: newId("resume-agent"), role: "agent", text, status: "complete" });
    } else if (message.role === "tool") {
      continue;
    } else {
      mapped.push({ id: newId("resume-system"), role: "system", text, status: "complete" });
    }
  }
  return mapped.length > 0 ? mapped : [welcomeMessage()];
}

function messagesWithInflight(history?: GatewayHistoryMessage[], inflight?: SessionInflightTurn | null): ChatMessage[] {
  const base = messagesFromGateway(history);
  if (!inflight || !inflight.streaming) return base;
  const user = inflight.user?.trim() || "";
  const assistant = inflight.assistant || "";
  const next = base[0]?.id === "welcome" && user ? [] : [...base];
  if (user && next[next.length - 1]?.text !== user) {
    next.push({ id: newId("resume-inflight-user"), role: "user", text: user, status: "complete" });
  }
  if (assistant) {
    next.push({ id: newId("resume-inflight-agent"), role: "agent", text: assistant, status: "streaming" });
  }
  return next.length > 0 ? next : base;
}

function toolDisclosureInsertIndex(messages: ChatMessage[], tools: ToolCallSummary[]): number | null {
  if (tools.length === 0) return null;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.role === "agent" && message.id !== "welcome") return index;
  }
  return messages.length;
}

function anchoredToolsForMessage(tools: ToolCallSummary[], messageId: string): ToolCallSummary[] {
  return tools.filter((tool) => tool.anchorMessageId === messageId);
}

function unanchoredTools(tools: ToolCallSummary[]): ToolCallSummary[] {
  return tools.filter((tool) => !tool.anchorMessageId);
}

function contextUsageFromPayload(payload: unknown): ContextUsage | null {
  if (!payload || typeof payload !== "object") return null;
  const data = payload as Record<string, unknown>;
  const usage = data.usage && typeof data.usage === "object" ? data.usage as Record<string, unknown> : data;
  const used = Number(usage.context_used);
  const max = Number(usage.context_max);
  const rawPercent = Number(usage.context_percent);
  const percent = Number.isFinite(rawPercent) ? Math.max(0, Math.min(100, Math.round(rawPercent))) : (Number.isFinite(used) && Number.isFinite(max) && max > 0 ? Math.round((used / max) * 100) : undefined);
  const compressions = Number(usage.compressions);
  if (!Number.isFinite(used) && !Number.isFinite(max) && percent === undefined) return null;
  return {
    used: Number.isFinite(used) ? used : undefined,
    max: Number.isFinite(max) ? max : undefined,
    percent,
    compressions: Number.isFinite(compressions) ? compressions : undefined,
  };
}

function compactNumber(value?: number): string {
  if (!Number.isFinite(value)) return "–";
  if ((value ?? 0) >= 1_000_000) return `${Math.round((value ?? 0) / 100_000) / 10}M`;
  if ((value ?? 0) >= 1_000) return `${Math.round((value ?? 0) / 100) / 10}k`;
  return String(Math.round(value ?? 0));
}

function contextTone(percent?: number): { label: string; bar: string; detail: string } {
  if (percent === undefined) return { label: "Kontext", bar: "#8b724e", detail: "Noch keine Messung" };
  if (percent >= 85) return { label: "Kontext: voll", bar: "#c98b7a", detail: "Bald komprimieren" };
  if (percent >= 65) return { label: "Kontext: mittel", bar: "#d7b98e", detail: "Noch genug Platz" };
  return { label: "Kontext: frei", bar: "#7bcf91", detail: "Ausreichend Platz" };
}

function textFromPayload(payload: unknown): string {
  if (typeof payload === "string") return payload;
  if (!payload || typeof payload !== "object") return "";
  const data = payload as Record<string, unknown>;
  const direct = data.text ?? data.message ?? data.question ?? data.name ?? data.command;
  if (typeof direct === "string") return direct;
  try {
    return JSON.stringify(data);
  } catch {
    return "";
  }
}

function newId(prefix: string): string {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function formatFileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  if (bytes < 1024) return `${bytes} B`;
  const kb = bytes / 1024;
  if (kb < 1024) return `${kb.toFixed(kb >= 10 ? 0 : 1)} KB`;
  const mb = kb / 1024;
  return `${mb.toFixed(mb >= 10 ? 0 : 1)} MB`;
}

function translateKnownNoteText(text: string): string {
  const normalized = text.trim();
  if (normalized === "Continue the current user task") return "Aktuelle Nutzeraufgabe fortsetzen";
  if (normalized.startsWith("User correction: ")) {
    return `Korrektur des Nutzers: ${normalized.slice("User correction: ".length)}`;
  }
  if (normalized.startsWith("Checkpoint: ")) {
    return `Zwischenstand: ${normalized.slice("Checkpoint: ".length)}`;
  }
  return normalized;
}

function itemText(item: unknown): string {
  if (!item) return "";
  if (typeof item === "string") return translateKnownNoteText(item);
  if (typeof item !== "object") return translateKnownNoteText(String(item));
  const data = item as Record<string, unknown>;
  const direct = data.summary ?? data.path ?? data.type ?? data.route;
  return typeof direct === "string" ? translateKnownNoteText(direct) : "";
}

function formatLiveNotes(snapshot: SessionNotesSnapshot | null): string {
  const scratchpad = snapshot?.scratchpad;
  const finalSummary = snapshot?.summary?.short_summary?.trim();
  const parts: string[] = [];
  const goal = scratchpad?.current_goal?.trim();
  if (goal) parts.push(`Aktuelles Ziel: ${translateKnownNoteText(goal)}`);
  const latestDecision = scratchpad?.decisions?.map(itemText).filter(Boolean).slice(-1)[0];
  if (latestDecision) parts.push(`Letzte Entscheidung: ${latestDecision}`);
  const latestArtifact = scratchpad?.artifacts?.map(itemText).filter(Boolean).slice(-1)[0];
  if (latestArtifact) parts.push(`Aktuelles Artefakt: ${latestArtifact}`);
  const latestOpen = scratchpad?.open_items?.map(itemText).filter(Boolean).slice(-1)[0];
  if (latestOpen) parts.push(`Offen: ${latestOpen}`);
  if (parts.length > 0) return parts.join(" · ");
  if (finalSummary) return finalSummary;
  return "Live-Notizen werden während der Sitzung automatisch aufgebaut.";
}

function busyModeLabel(mode?: string): string {
  if (mode === "queue") return "Warteschlange";
  if (mode === "steer") return "Lenken";
  return "Unterbrechen";
}

function reasoningEffortLabel(effort?: string): string {
  if (effort === "none") return "aus";
  if (effort === "minimal") return "minimal";
  if (effort === "low") return "niedrig";
  if (effort === "medium") return "mittel";
  if (effort === "high") return "hoch";
  if (effort === "xhigh") return "sehr hoch";
  return effort?.trim() || "standard";
}

function reasoningLabel(status: RuntimeStatus): string {
  const effort = reasoningEffortLabel(status.reasoningEffort);
  const display = status.reasoningDisplay === "show" ? "sichtbar" : "verdeckt";
  return `${effort} · ${display}`;
}

function fastModeLabel(mode?: string): string {
  return mode === "fast" ? "Schnell" : "Normal";
}

function statusBadges(status: RuntimeStatus, approvalCount: number): RuntimeBadge[] {
  return [
    {
      id: "busy",
      label: `Eingabe: ${busyModeLabel(status.busyMode)}`,
      help: "Legt fest, was passiert, wenn Sie schreiben, während der Assistent arbeitet.",
    },
    {
      id: "reasoning",
      label: `Denken: ${reasoningLabel(status)}`,
      help: "Steuert Denkaufwand und ob Zwischenüberlegungen sichtbar werden.",
    },
    {
      id: "fast",
      label: `Tempo: ${fastModeLabel(status.fastMode)}`,
      help: "Schaltet zwischen normaler und schnellerer Verarbeitung um, falls das Modell es unterstützt.",
    },
    {
      id: "approvals",
      label: status.yoloMode === "on" || status.yoloMode === "1"
        ? "Aktionen: direkt"
        : approvalCount > 0
          ? `Aktionen: ${approvalCount} offen`
          : "Aktionen: mit Rückfrage",
      help: "Wählt, ob riskante Aktionen vorher bestätigt werden müssen oder direkt laufen.",
    },
  ];
}


function cleanAssistantName(value?: string | null): string {
  const clean = (value ?? "").replace(/\s+/g, " ").trim();
  return clean || "Agent";
}

function assistantSubjectName(name: string): string {
  return name === "Agent" ? "der Agent" : name;
}

function assistantDativeName(name: string): string {
  return name === "Agent" ? "dem Agent" : name;
}

function chatHeaderCopy(
  busy: boolean,
  approvalCount: number,
  connection: ConnectionState,
  statusLabel: string,
  assistantName: string,
): { title: string; subtitle: string } {
  if (approvalCount > 0) {
    return {
      title: "Freigabe nötig",
      subtitle: approvalCount === 1
        ? "Bitte prüfen Sie die wartende Aktion"
        : `${approvalCount} Aktionen warten auf Ihre Entscheidung`,
    };
  }
  if (busy) {
    return {
      title: "Aktuelle Unterhaltung",
      subtitle: "Antwort wird vorbereitet · Sie können weiterschreiben",
    };
  }
  if (connection !== "open") {
    return {
      title: "Aktuelle Unterhaltung",
      subtitle: statusLabel,
    };
  }
  return {
    title: "Aktuelle Unterhaltung",
    subtitle: `Schreiben Sie, was ${assistantSubjectName(assistantName)} erledigen soll.`,
  };
}

function upsertAssistantDelta(messages: ChatMessage[], text: string): ChatMessage[] {
  const next = [...messages];
  const last = next[next.length - 1];
  if (last?.role === "agent" && last.status === "streaming") {
    next[next.length - 1] = { ...last, text: last.text + text };
    return next;
  }
  next.push({ id: newId("agent"), role: "agent", text, status: "streaming" });
  return next;
}

function completeAssistant(
  messages: ChatMessage[],
  text: string,
  status: ChatMessage["status"],
): ChatMessage[] {
  const next = [...messages];
  const lastStreamingIndex = next.findLastIndex((message) => message.role === "agent" && message.status === "streaming");
  if (lastStreamingIndex >= 0) {
    const current = next[lastStreamingIndex];
    next[lastStreamingIndex] = { ...current, text: text || current.text, status };
    return next;
  }
  next.push({ id: newId("agent"), role: "agent", text, status });
  return next;
}

function insertUserGuidance(messages: ChatMessage[], message: ChatMessage): ChatMessage[] {
  const next = [...messages];
  const last = next[next.length - 1];
  if (last?.role === "agent" && last.status === "streaming") {
    next.splice(next.length - 1, 0, message);
    return next;
  }
  next.push(message);
  return next;
}

function textField(payload: Record<string, unknown>, key: string): string | undefined {
  const value = payload[key];
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function upsertToolCall(
  tools: ToolCallSummary[],
  payload: Record<string, unknown>,
  status: ToolCallSummary["status"],
  anchorMessageId?: string,
): ToolCallSummary[] {
  const toolId = textField(payload, "tool_id") || textField(payload, "id") || textField(payload, "name") || newId("tool");
  const next = [...tools];
  const existingIndex = next.findIndex((tool) =>
    tool.toolId === toolId && (anchorMessageId ? tool.anchorMessageId === anchorMessageId : !tool.anchorMessageId),
  );
  const previous = existingIndex >= 0 ? next[existingIndex] : null;
  const entry: ToolCallSummary = {
    id: previous?.id || newId("tool"),
    toolId,
    anchorMessageId: previous?.anchorMessageId || anchorMessageId,
    name: textField(payload, "name") || previous?.name || "tool",
    context: textField(payload, "context") || previous?.context,
    summary: textField(payload, "summary") || previous?.summary,
    details: textField(payload, "result_text") || textField(payload, "args_text") || textField(payload, "inline_diff") || previous?.details,
    status,
  };
  if (existingIndex >= 0) next[existingIndex] = entry;
  else next.push(entry);
  return next;
}

export default function AiwerkAssistantPage() {
  const gatewayRef = useRef<GatewayClient | null>(null);
  const messagesScrollRef = useRef<HTMLDivElement | null>(null);
  const contentGridRef = useRef<HTMLElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const mainInputRef = useRef<HTMLTextAreaElement | null>(null);
  const sideInputRef = useRef<HTMLTextAreaElement | null>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const voiceStreamRef = useRef<MediaStream | null>(null);
  const voiceChunksRef = useRef<Blob[]>([]);
  const voiceTimerRef = useRef<number | null>(null);
  const voiceCancelledRef = useRef(false);
  const attachmentUrlsRef = useRef<Set<string>>(new Set());
  const readAloudEnabledRef = useRef(false);
  const readAloudAudioRef = useRef<HTMLAudioElement | null>(null);
  const readAloudUrlRef = useRef<string | null>(null);
  const readAloudRequestRef = useRef(0);
  const messagesRef = useRef<ChatMessage[]>([]);
  const sideMessagesRef = useRef<ChatMessage[]>([]);

  const [connection, setConnection] = useState<ConnectionState>("idle");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const [activeSessionKey, setActiveSessionKey] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [sideInput, setSideInput] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>(() => [welcomeMessage()]);
  const [sideMessages, setSideMessages] = useState<ChatMessage[]>([]);
  const [toolCalls, setToolCalls] = useState<ToolCallSummary[]>([]);
  const [sideToolCalls, setSideToolCalls] = useState<ToolCallSummary[]>([]);
  const [approvals, setApprovals] = useState<ApprovalCard[]>([]);
  const [recentSessions, setRecentSessions] = useState<RecentSession[]>([]);
  const [sessionTitle, setSessionTitle] = useState("Neue Unterhaltung");
  const [liveNotes, setLiveNotes] = useState<SessionNotesSnapshot | null>(null);
  const [runtimeStatus, setRuntimeStatus] = useState<RuntimeStatus>({});
  const [modelInfo, setModelInfo] = useState<ModelInfoResponse | null>(null);
  const [resourceSummary, setResourceSummary] = useState<AssistantResourcesResponse | null>(null);
  const [resourcesLoading, setResourcesLoading] = useState(false);
  const [resourceRefreshing, setResourceRefreshing] = useState<Partial<Record<ResourcePanelKey, boolean>>>({});
  const [resourcesError, setResourcesError] = useState<string | null>(null);
  const [contextUsage, setContextUsage] = useState<ContextUsage | null>(null);
  const [activeStatusModal, setActiveStatusModal] = useState<RuntimeBadgeId | null>(null);
  const [conversationMode, setConversationMode] = useState<ConversationMode>("main");
  const [busy, setBusy] = useState(false);
  const [isCompressing, setIsCompressing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [attachedFiles, setAttachedFiles] = useState<AttachmentPreview[]>([]);
  const [draggingAttachment, setDraggingAttachment] = useState(false);
  const [voiceState, setVoiceState] = useState<VoiceInputState>("idle");
  const [voiceSeconds, setVoiceSeconds] = useState(0);
  const [readAloudEnabled, setReadAloudEnabled] = useState(() => readStoredReadAloudEnabled());
  const [rightRailWidth, setRightRailWidth] = useState(() => readStoredRightRailWidth());
  const [isResizingRightRail, setIsResizingRightRail] = useState(false);
  const [readAloudBusy, setReadAloudBusy] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [sessionTitleBubble, setSessionTitleBubble] = useState<SessionTitleBubble | null>(null);
  const [activeTurnMode, setActiveTurnMode] = useState<ConversationMode>("main");
  const [documentTabs, setDocumentTabs] = useState<DocumentTab[]>([]);
  const [activePanelTab, setActivePanelTab] = useState<PanelTabId>("chat");
  const activeTurnModeRef = useRef<ConversationMode>("main");
  const activeToolAnchorRef = useRef<string | undefined>(undefined);
  const activeSideToolAnchorRef = useRef<string | undefined>(undefined);
  const conversationModeRef = useRef<ConversationMode>("main");

  const showToast = useCallback((text: string) => {
    setToast(text);
    window.setTimeout(() => setToast(null), 1800);
  }, []);

  const activeDocumentTab = useMemo(
    () => documentTabs.find((tab) => tab.id === activePanelTab) ?? null,
    [activePanelTab, documentTabs],
  );
  const isChatPanelActive = activePanelTab === "chat";

  const closeDocumentTab = useCallback((tabId: string) => {
    setDocumentTabs((current) => {
      const index = current.findIndex((tab) => tab.id === tabId);
      const next = current.filter((tab) => tab.id !== tabId);
      setActivePanelTab((active) => {
        if (active !== tabId) return active;
        return next[index - 1]?.id ?? next[index]?.id ?? "chat";
      });
      return next;
    });
  }, []);

  const openDocumentTab = useCallback(async (kind: DocumentTabKind, openUrl: string | null | undefined, title: string, subtitle?: string) => {
    if (!openUrl) return;
    if (/^https?:\/\//i.test(openUrl)) {
      const opened = window.open(openUrl, "_blank", "noopener,noreferrer");
      if (opened) opened.opener = null;
      return;
    }
    const id = `${kind}:${openUrl}`;
    setDocumentTabs((current) => {
      const existing = current.find((tab) => tab.id === id);
      if (existing) return current.map((tab) => tab.id === id ? { ...tab, title, subtitle, status: "loading", error: undefined } : tab);
      return [...current, { id, kind, title, subtitle, openUrl, status: "loading" }];
    });
    setActivePanelTab(id);
    try {
      const html = await fetchTokenProtectedHtml(openUrl);
      setDocumentTabs((current) => current.map((tab) => tab.id === id ? { ...tab, html, status: "ready", error: undefined } : tab));
    } catch (error) {
      setDocumentTabs((current) => current.map((tab) => tab.id === id ? {
        ...tab,
        status: "error",
        error: error instanceof Error ? error.message : String(error),
      } : tab));
      showToast(kind === "email" ? "E-Mail konnte nicht geöffnet werden." : "Termin konnte nicht geöffnet werden.");
    }
  }, [showToast]);

  const openEmailInPanel = useCallback((item: AssistantResourceMailItem) => {
    const subtitle = [item.sender, item.received_at ? formatResourceTime(item.received_at) : undefined].filter(Boolean).join(" · ");
    void openDocumentTab("email", item.open_url, item.subject || "E-Mail", subtitle || item.account_address || item.account_label);
  }, [openDocumentTab]);

  const openCalendarInPanel = useCallback((item: AssistantResourceEventItem) => {
    const subtitle = [formatResourceTime(item.starts_at), item.location_hint].filter(Boolean).join(" · ");
    void openDocumentTab("calendar", item.open_url, item.title || "Termin", subtitle || item.account_address || item.account_label);
  }, [openDocumentTab]);

  const openContactInPanel = useCallback((contact: AssistantContactItem) => {
    const title = contact.display_name || contact.email || contact.phone || "Kontakt";
    const subtitle = [contact.role, contact.organization].filter(Boolean).join(" · ") || contact.email || contact.phone;
    const id = `contact:${contact.id || title}`;
    setDocumentTabs((current) => {
      const existing = current.find((tab) => tab.id === id);
      if (existing) return current.map((tab) => tab.id === id ? { ...tab, title, subtitle, contact, status: "ready", error: undefined } : tab);
      return [...current, { id, kind: "contact", title, subtitle, openUrl: id, contact, status: "ready" }];
    });
    setActivePanelTab(id);
  }, []);

  const startRightRailResize = useCallback((event: ReactPointerEvent<HTMLButtonElement>) => {
    if (!contentGridRef.current) return;
    event.preventDefault();
    const pointerId = event.pointerId;
    event.currentTarget.setPointerCapture(pointerId);
    setIsResizingRightRail(true);

    const updateWidth = (clientX: number) => {
      const rect = contentGridRef.current?.getBoundingClientRect();
      if (!rect) return;
      const nextWidth = clampRightRailWidth(rect.right - clientX - 12);
      setRightRailWidth(nextWidth);
      storeRightRailWidth(nextWidth);
    };

    updateWidth(event.clientX);

    const handleMove = (moveEvent: PointerEvent) => updateWidth(moveEvent.clientX);
    const handleUp = () => {
      setIsResizingRightRail(false);
      window.removeEventListener("pointermove", handleMove);
      window.removeEventListener("pointerup", handleUp);
      window.removeEventListener("pointercancel", handleUp);
    };

    window.addEventListener("pointermove", handleMove);
    window.addEventListener("pointerup", handleUp, { once: true });
    window.addEventListener("pointercancel", handleUp, { once: true });
  }, []);

  const resetRightRailWidth = useCallback(() => {
    setRightRailWidth(RIGHT_RAIL_DEFAULT_WIDTH);
    storeRightRailWidth(RIGHT_RAIL_DEFAULT_WIDTH);
  }, []);

  const resizeComposerTextarea = useCallback((element: HTMLTextAreaElement | null) => {
    if (!element) return;
    const maxHeight = 160;
    element.style.height = "auto";
    const nextHeight = Math.min(element.scrollHeight, maxHeight);
    element.style.height = `${nextHeight}px`;
    element.style.overflowY = element.scrollHeight > maxHeight ? "auto" : "hidden";
  }, []);

  useLayoutEffect(() => {
    resizeComposerTextarea(mainInputRef.current);
  }, [input, resizeComposerTextarea]);

  useLayoutEffect(() => {
    resizeComposerTextarea(sideInputRef.current);
  }, [sideInput, resizeComposerTextarea]);

  const stopReadAloud = useCallback(() => {
    readAloudRequestRef.current += 1;
    const audio = readAloudAudioRef.current;
    if (audio) {
      audio.pause();
      audio.removeAttribute("src");
      audio.load();
    }
    readAloudAudioRef.current = null;
    if (readAloudUrlRef.current) {
      URL.revokeObjectURL(readAloudUrlRef.current);
      readAloudUrlRef.current = null;
    }
    setReadAloudBusy(false);
  }, []);

  const speakAssistantText = useCallback((text: string) => {
    if (!readAloudEnabledRef.current) return;
    const speechText = speechTextFromMarkdown(text);
    if (!speechText) return;
    const requestId = readAloudRequestRef.current + 1;
    readAloudRequestRef.current = requestId;
    setReadAloudBusy(true);
    void api.synthesizeAssistantSpeech(speechText, sessionIdRef.current ?? undefined)
      .then((blob) => {
        if (!readAloudEnabledRef.current || readAloudRequestRef.current !== requestId) return;
        if (readAloudUrlRef.current) URL.revokeObjectURL(readAloudUrlRef.current);
        const url = URL.createObjectURL(blob);
        readAloudUrlRef.current = url;
        const audio = new Audio(url);
        readAloudAudioRef.current = audio;
        audio.onended = () => {
          if (readAloudRequestRef.current === requestId) setReadAloudBusy(false);
        };
        audio.onerror = () => {
          if (readAloudRequestRef.current === requestId) {
            setReadAloudBusy(false);
            showToast("Vorlesen konnte nicht abgespielt werden.");
          }
        };
        return audio.play();
      })
      .catch(() => {
        if (readAloudRequestRef.current === requestId) {
          setReadAloudBusy(false);
          showToast("ElevenLabs-Vorlesen fehlgeschlagen.");
        }
      });
  }, [showToast]);

  const toggleReadAloud = useCallback(() => {
    setReadAloudEnabled((current) => {
      const next = !current;
      readAloudEnabledRef.current = next;
      storeReadAloudEnabled(next);
      if (!next) stopReadAloud();
      showToast(next ? "Vorlesen eingeschaltet" : "Vorlesen ausgeschaltet");
      return next;
    });
  }, [showToast, stopReadAloud]);

  const showTruncatedSessionTitle = useCallback((button: HTMLButtonElement, text: string) => {
    const label = button.querySelector("strong");
    if (!label || label.scrollWidth <= label.clientWidth + 1) {
      setSessionTitleBubble(null);
      return;
    }
    const rect = button.getBoundingClientRect();
    const width = Math.min(320, Math.max(220, rect.width + 28));
    const desiredLeft = rect.left + 72;
    const left = Math.min(Math.max(desiredLeft, 16), window.innerWidth - width - 16);
    const placement: SessionTitleBubble["placement"] = rect.top > 82 ? "above" : "below";
    setSessionTitleBubble({
      text,
      left,
      top: placement === "above" ? rect.top - 2 : rect.bottom + 10,
      width,
      placement,
    });
  }, []);

  const hideSessionTitleBubble = useCallback(() => {
    setSessionTitleBubble(null);
  }, []);

  const revokeAttachmentUrl = useCallback((url?: string) => {
    if (!url || !attachmentUrlsRef.current.has(url)) return;
    URL.revokeObjectURL(url);
    attachmentUrlsRef.current.delete(url);
  }, []);

  const removeAttachedFile = useCallback((id: string) => {
    setAttachedFiles((current) => {
      const target = current.find((file) => file.id === id);
      revokeAttachmentUrl(target?.previewUrl);
      return current.filter((file) => file.id !== id);
    });
    if (fileInputRef.current) fileInputRef.current.value = "";
  }, [revokeAttachmentUrl]);

  const clearAttachedFiles = useCallback(() => {
    setAttachedFiles((current) => {
      current.forEach((file) => revokeAttachmentUrl(file.previewUrl));
      return [];
    });
    if (fileInputRef.current) fileInputRef.current.value = "";
  }, [revokeAttachmentUrl]);

  const addAttachedFiles = useCallback((files: File[], source: "file" | "paste" | "drop" = "file") => {
    if (files.length === 0) return;
    const previews = files.map((file) => {
      const isImage = file.type.startsWith("image/");
      const previewUrl = isImage ? URL.createObjectURL(file) : undefined;
      if (previewUrl) attachmentUrlsRef.current.add(previewUrl);
      return {
        id: newId("attachment"),
        name: file.name || (isImage ? "Eingefügtes Bild" : "Anhang"),
        type: file.type || "application/octet-stream",
        size: file.size,
        previewUrl,
        file,
      };
    });
    setAttachedFiles((current) => [...current, ...previews]);
    const label = source === "drop"
      ? (files.length === 1 ? "Datei abgelegt" : `${files.length} Dateien abgelegt`)
      : (files.length === 1 ? "Datei angehängt" : `${files.length} Dateien angehängt`);
    showToast(source === "paste" ? "Bild aus der Zwischenablage angehängt" : label);
  }, [showToast]);

  const attachResourceToSession = useCallback(async (
    kind: "email" | "calendar_event" | "shared_file" | "contact",
    item: Record<string, unknown>,
    label: string,
  ) => {
    if (!sessionId) {
      showToast("Sitzung noch nicht bereit.");
      return;
    }
    try {
      const result = await api.attachAssistantResource({ kind, item, session_id: sessionId });
      const previews = result.attachments.map((uploaded) => ({
        id: newId("attachment"),
        name: uploaded.name || label,
        type: uploaded.type || "application/octet-stream",
        size: uploaded.size || 0,
        uploaded,
      }));
      setAttachedFiles((current) => [...current, ...previews]);
      showToast(`${label} angehängt`);
    } catch {
      showToast(`${label} konnte nicht angehängt werden.`);
    }
  }, [sessionId, showToast]);

  const attachTodoToComposer = useCallback((item: AssistantTodoItem) => {
    const taskText = item.text.trim();
    if (!taskText) return;
    const marker = `Aufgabe-ID: ${item.id}`;
    const prompt = [
      "Hilf mir, diese Aufgabe zu lösen.",
      "",
      "Aufgabe:",
      taskText,
      "",
      "Status:",
      item.done ? "erledigt" : "offen",
      "",
      item.line ? `Quelle: TODO.md, Zeile ${item.line}` : "Quelle: TODO.md",
      marker,
      "",
      "Bitte:",
      "- zerlege die Aufgabe in konkrete Schritte,",
      "- nutze den aktuellen Gesprächskontext, wenn er relevant ist,",
      "- frage gezielt nach, falls etwas unklar ist,",
      "- schlage den nächsten sinnvollen Schritt vor.",
    ].join("\n");

    setActivePanelTab("chat");
    setActiveTurnMode("main");
    conversationModeRef.current = "main";
    setInput((current) => {
      if (current.includes(marker)) return current;
      const trimmed = current.trim();
      return trimmed ? `${trimmed}\n\n${prompt}` : prompt;
    });
    window.setTimeout(() => mainInputRef.current?.focus(), 0);
    showToast("Aufgabe in den Chat übernommen");
  }, [showToast]);

  const stopVoiceTracks = useCallback(() => {
    if (voiceTimerRef.current !== null) {
      window.clearInterval(voiceTimerRef.current);
      voiceTimerRef.current = null;
    }
    voiceStreamRef.current?.getTracks().forEach((track) => track.stop());
    voiceStreamRef.current = null;
  }, []);

  const stopVoiceInput = useCallback(() => {
    const recorder = mediaRecorderRef.current;
    if (recorder && recorder.state !== "inactive") {
      recorder.stop();
    }
  }, []);

  const cancelVoiceInput = useCallback(() => {
    voiceCancelledRef.current = true;
    stopVoiceInput();
    stopVoiceTracks();
    mediaRecorderRef.current = null;
    voiceChunksRef.current = [];
    setVoiceState("idle");
    setVoiceSeconds(0);
  }, [stopVoiceInput, stopVoiceTracks]);

  const startVoiceInput = useCallback(async () => {
    if (voiceState === "recording") {
      stopVoiceInput();
      return;
    }
    if (voiceState !== "idle") return;
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      showToast("Spracheingabe wird von diesem Browser nicht unterstützt.");
      return;
    }
    try {
      voiceCancelledRef.current = false;
      voiceChunksRef.current = [];
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      voiceStreamRef.current = stream;
      const preferredTypes = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/ogg"];
      const mimeType = preferredTypes.find((type) => MediaRecorder.isTypeSupported(type)) || "";
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
      mediaRecorderRef.current = recorder;
      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) voiceChunksRef.current.push(event.data);
      };
      recorder.onerror = () => {
        stopVoiceTracks();
        setVoiceState("idle");
        showToast("Sprache konnte nicht aufgenommen werden.");
      };
      recorder.onstop = () => {
        const chunks = [...voiceChunksRef.current];
        const cancelled = voiceCancelledRef.current;
        const type = recorder.mimeType || mimeType || "audio/webm";
        stopVoiceTracks();
        mediaRecorderRef.current = null;
        voiceChunksRef.current = [];
        setVoiceSeconds(0);
        if (cancelled || chunks.length === 0) {
          setVoiceState("idle");
          return;
        }
        setVoiceState("transcribing");
        const extension = type.includes("ogg") ? "ogg" : "webm";
        const file = new File(chunks, `sprache.${extension}`, { type });
        api.transcribeAssistantAudio(file, sessionId ?? undefined)
          .then((result) => {
            const text = result.text.trim();
            if (!text) {
              showToast("Sprache konnte nicht erkannt werden.");
              return;
            }
            setInput((current) => current.trim() ? `${current.trim()} ${text}` : text);
            showToast("Sprache erkannt");
          })
          .catch(() => showToast("Sprache konnte nicht erkannt werden."))
          .finally(() => setVoiceState("idle"));
      };
      recorder.start();
      setVoiceSeconds(0);
      setVoiceState("recording");
      voiceTimerRef.current = window.setInterval(() => setVoiceSeconds((seconds) => seconds + 1), 1000);
    } catch (error) {
      stopVoiceTracks();
      setVoiceState("idle");
      const errorName = error instanceof DOMException ? error.name : "";
      if (errorName === "NotAllowedError" || errorName === "SecurityError") {
        showToast("Mikrofonzugriff im Browser erlauben.");
      } else if (errorName === "NotFoundError" || errorName === "DevicesNotFoundError") {
        const devices = await navigator.mediaDevices?.enumerateDevices?.().catch(() => []);
        const hasAudioInput = Array.isArray(devices) && devices.some((device) => device.kind === "audioinput");
        showToast(hasAudioInput ? "Mikrofon in den Browser-Einstellungen wählen." : "Kein Mikrofon im Browser gefunden.");
      } else {
        showToast("Mikrofon konnte nicht geöffnet werden.");
      }
    }
  }, [sessionId, showToast, stopVoiceInput, stopVoiceTracks, voiceState]);

  const statusInfo = useMemo(() => {
    if (connection === "open") {
      return {
        label: "Online",
        detail: "Verbindung aktiv",
        dot: "#7bcf91",
        glow: "0 0 0 4px rgba(123,207,145,.14)",
      };
    }
    if (connection === "connecting") {
      return {
        label: "Verbindet…",
        detail: "Verbindung wird hergestellt",
        dot: "#d7b98e",
        glow: "0 0 0 4px rgba(215,185,142,.14)",
      };
    }
    if (connection === "error") {
      return {
        label: "Fehler",
        detail: "Verbindung fehlgeschlagen",
        dot: "#c98b7a",
        glow: "0 0 0 4px rgba(201,139,122,.14)",
      };
    }
    return {
      label: "Offline",
      detail: "Verbindung unterbrochen",
      dot: "#c98b7a",
      glow: "0 0 0 4px rgba(201,139,122,.14)",
    };
  }, [connection]);
  const statusLabel = statusInfo.label;
  const contextInfo = useMemo(() => contextTone(contextUsage?.percent), [contextUsage?.percent]);
  const sessionMessageCount = useMemo(() => {
    const currentMessages = conversationMode === "side" ? sideMessages : messages;
    return currentMessages.filter((message) => message.role !== "system").length;
  }, [conversationMode, messages, sideMessages]);
  const currentModelLabel = useMemo(() => {
    if (!modelInfo?.model) return "Wird geladen…";
    return modelInfo.provider ? `${modelInfo.provider}/${modelInfo.model}` : modelInfo.model;
  }, [modelInfo]);
  const assistantName = useMemo(() => cleanAssistantName(modelInfo?.agent_name), [modelInfo?.agent_name]);
  const assistantInitial = assistantName.charAt(0).toUpperCase();

  const liveNotesText = useMemo(() => formatLiveNotes(liveNotes), [liveNotes]);
  const headerBadges = useMemo(() => statusBadges(runtimeStatus, approvals.length), [runtimeStatus, approvals.length]);
  const chatHeader = useMemo(
    () => chatHeaderCopy(busy, approvals.length, connection, statusLabel, assistantName),
    [busy, approvals.length, connection, statusLabel, assistantName],
  );
  const foldedTabHelp = conversationMode === "side"
    ? "Nebenfrage schliessen"
    : "Nebenfrage starten";
  const canCompress = Boolean(
    sessionId
    && connection === "open"
    && conversationMode === "main"
    && !busy
    && !isCompressing
    && (contextUsage?.percent ?? 0) >= COMPRESS_CONTEXT_PERCENT_THRESHOLD,
  );
  const showDashboardLoader = connection !== "open" || !sessionId || !modelInfo || (!resourceSummary && !resourcesError);
  const dashboardLoaderStep = connection !== "open"
    ? "Verbindung zum Agenten wird hergestellt"
    : !sessionId
      ? "Sitzung wird geöffnet"
      : !modelInfo
        ? "Agentenprofil wird geladen"
        : !resourceSummary && !resourcesError
          ? "Ressourcen werden synchronisiert"
          : "Dashboard ist bereit";
  const compressHelp = canCompress
    ? "Kontext jetzt komprimieren"
    : `Komprimieren wird aktiv ab ca. ${COMPRESS_CONTEXT_PERCENT_THRESHOLD}% Kontextbelegung; automatisch komprimiert Hermes erst später.`;

  const refreshRuntimeStatus = useCallback(async (gateway: GatewayClient, sid: string) => {
    const [busyResult, reasoningResult, fastResult, yoloResult] = await Promise.allSettled([
      gateway.request<{ value?: string }>("config.get", { key: "busy", session_id: sid }, 15_000),
      gateway.request<{ value?: string; display?: string }>("config.get", { key: "reasoning", session_id: sid }, 15_000),
      gateway.request<{ value?: string }>("config.get", { key: "fast", session_id: sid }, 15_000),
      gateway.request<{ value?: string }>("config.get", { key: "yolo", session_id: sid }, 15_000),
    ]);
    setRuntimeStatus((current) => ({
      ...current,
      busyMode: busyResult.status === "fulfilled" ? busyResult.value.value : current.busyMode,
      reasoningEffort: reasoningResult.status === "fulfilled" ? reasoningResult.value.value : current.reasoningEffort,
      reasoningDisplay: reasoningResult.status === "fulfilled" ? reasoningResult.value.display : current.reasoningDisplay,
      fastMode: fastResult.status === "fulfilled" ? fastResult.value.value : current.fastMode,
      yoloMode: yoloResult.status === "fulfilled" ? yoloResult.value.value : current.yoloMode,
    }));
  }, []);

  const refreshContextUsage = useCallback(async (gateway: GatewayClient, sid: string) => {
    try {
      const usage = await gateway.request<Record<string, unknown>>("session.usage", { session_id: sid }, 15_000);
      setContextUsage(contextUsageFromPayload(usage));
    } catch {
      setContextUsage(null);
    }
  }, []);

  const refreshSessionMeta = useCallback(async (gateway: GatewayClient, sid: string) => {
    const [titleResult, notesResult] = await Promise.allSettled([
      gateway.request<{ title?: string }>("session.title", { session_id: sid }, 15_000),
      gateway.request<SessionNotesSnapshot>("session.notes", { session_id: sid, limit: 12 }, 15_000),
    ]);
    if (titleResult.status === "fulfilled" && titleResult.value.title?.trim()) {
      setSessionTitle(titleResult.value.title.trim());
    }
    if (notesResult.status === "fulfilled") {
      setLiveNotes(notesResult.value);
      if (notesResult.value.title?.trim()) setSessionTitle(notesResult.value.title.trim());
    }
  }, []);

  const refreshRecentSessions = useCallback(async (pinnedSession?: RecentSession) => {
    const pinSession = (sessions: RecentSession[]) => {
      if (!pinnedSession?.id) return sessions.slice(0, 10);
      const withoutDuplicate = sessions.filter((session) => session.id !== pinnedSession.id);
      return [pinnedSession, ...withoutDuplicate].slice(0, 10);
    };
    try {
      const res = await api.getSessions(10, 0, { excludeSources: ["cron"] });
      setRecentSessions(pinSession(res.sessions ?? []));
    } catch {
      if (pinnedSession?.id) setRecentSessions((current) => pinSession(current));
    }
  }, []);

  const refreshResources = useCallback(async (options?: { force?: boolean; resource?: ResourcePanelKey }) => {
    if (options?.resource) {
      setResourceRefreshing((current) => ({ ...current, [options.resource as ResourcePanelKey]: true }));
    } else {
      setResourcesLoading(true);
    }
    try {
      const resources = await api.getAssistantResources({ refresh: options?.force, resource: options?.resource });
      setResourceSummary((current) => mergeAssistantResources(current, resources, options?.resource));
      setResourcesError(null);
    } catch (e) {
      setResourcesError(e instanceof Error ? e.message : "Ressourcen konnten nicht geladen werden.");
    } finally {
      if (options?.resource) {
        setResourceRefreshing((current) => ({ ...current, [options.resource as ResourcePanelKey]: false }));
      } else {
        setResourcesLoading(false);
      }
    }
  }, []);

  const addTodo = useCallback(async (text: string) => {
    try {
      const result = await api.addAssistantTodo(text);
      setResourceSummary((current) => current ? { ...current, todos: result.todos } : current);
      setResourcesError(null);
    } catch (e) {
      setResourcesError(e instanceof Error ? e.message : "Aufgabe konnte nicht hinzugefügt werden.");
      throw e;
    }
  }, []);

  const updateTodoDone = useCallback(async (id: string, done: boolean) => {
    let previous: AssistantResourcesResponse | null = null;
    setResourceSummary((current) => {
      previous = current;
      if (!current) return current;
      const before = current.todos.items.find((item) => item.id === id);
      if (!before || !!before.done === done) return current;
      const items = current.todos.items.map((item) => item.id === id ? { ...item, done } : item);
      const open_count = items.filter((item) => !item.done).length;
      const done_count = items.filter((item) => item.done).length;
      return {
        ...current,
        todos: {
          ...current.todos,
          items,
          open_count,
          done_count,
          total_count: items.length,
          summary: open_count ? `${open_count} offene Aufgaben` : "Keine offenen Aufgaben",
        },
      };
    });
    try {
      const result = await api.updateAssistantTodo(id, done);
      setResourceSummary((current) => current ? { ...current, todos: result.todos } : current);
      setResourcesError(null);
    } catch (e) {
      setResourceSummary(previous);
      setResourcesError(e instanceof Error ? e.message : "Aufgabe konnte nicht aktualisiert werden.");
      throw e;
    }
  }, []);

  const submitSupportMessage = useCallback(async (payload: AssistantSupportRequest) => {
    const result = await api.sendAssistantSupport(payload);
    if (result.delivered) {
      showToast(`Nachricht an AIWerk gesendet · ${result.support_id}`);
    } else {
      showToast(`Nachricht gespeichert · ${result.support_id}`);
    }
    return result;
  }, [showToast]);

  const reloadMcpServers = useCallback(async () => {
    const gateway = gatewayRef.current;
    if (!gateway || !sessionId) {
      showToast("MCP-Reload nicht verbunden");
      return;
    }
    showToast("MCP-Server werden neu geladen…");
    try {
      await gateway.request("slash.exec", { session_id: sessionId, command: "reload-mcp" }, 120_000);
      window.setTimeout(() => void refreshResources({ force: true }), 400);
      window.setTimeout(() => void refreshResources({ force: true }), 1800);
      showToast("MCP-Server neu geladen");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      showToast("MCP-Reload fehlgeschlagen");
      throw e;
    }
  }, [refreshResources, sessionId, showToast]);

  useEffect(() => {
    const initial = window.setTimeout(() => void refreshResources(), 0);
    const timer = window.setInterval(() => void refreshResources(), 30 * 60 * 1000);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(timer);
    };
  }, [refreshResources]);

  const editSessionTitle = useCallback(async () => {
    const gateway = gatewayRef.current;
    if (!gateway || !sessionId) return;
    const next = window.prompt("Sitzungstitel", sessionTitle)?.trim();
    if (!next || next === sessionTitle) return;
    try {
      const result = await gateway.request<{ title?: string }>("session.title", { session_id: sessionId, title: next });
      setSessionTitle(result.title?.trim() || next);
      showToast("Titel gespeichert");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [sessionId, sessionTitle, showToast]);

  useEffect(() => {
    sessionIdRef.current = sessionId;
  }, [sessionId]);

  useEffect(() => {
    readAloudEnabledRef.current = readAloudEnabled;
    storeReadAloudEnabled(readAloudEnabled);
  }, [readAloudEnabled]);

  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);

  useEffect(() => {
    sideMessagesRef.current = sideMessages;
  }, [sideMessages]);

  useEffect(() => {
    return () => stopReadAloud();
  }, [stopReadAloud]);

  useEffect(() => {
    conversationModeRef.current = conversationMode;
  }, [conversationMode]);

  useEffect(() => {
    const el = messagesScrollRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    const urls = attachmentUrlsRef.current;
    return () => {
      urls.forEach((url) => URL.revokeObjectURL(url));
      urls.clear();
    };
  }, []);

  useEffect(() => {
    return () => {
      if (voiceTimerRef.current !== null) window.clearInterval(voiceTimerRef.current);
      voiceStreamRef.current?.getTracks().forEach((track) => track.stop());
      const recorder = mediaRecorderRef.current;
      if (recorder && recorder.state !== "inactive") recorder.stop();
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const gateway = new GatewayClient();
    gatewayRef.current = gateway;

    const offState = gateway.onState((state) => {
      if (!cancelled) setConnection(state as ConnectionState);
    });
    const offDelta = gateway.on("message.delta", (ev) => {
      const delta = textFromPayload(ev.payload);
      if (!delta) return;
      const target = activeTurnModeRef.current || conversationModeRef.current;
      if (target === "side") {
        setSideMessages((prev) => upsertAssistantDelta(prev, delta));
      } else {
        setMessages((prev) => upsertAssistantDelta(prev, delta));
      }
    });
    const offComplete = gateway.on("message.complete", (ev) => {
      const payload = (ev.payload ?? {}) as Record<string, unknown>;
      const text = textFromPayload(payload);
      const status = payload.status === "error" ? "error" : "complete";
      const target = activeTurnModeRef.current || conversationModeRef.current;
      const currentMessages = target === "side" ? sideMessagesRef.current : messagesRef.current;
      const latestAssistantText = currentMessages[currentMessages.length - 1]?.role === "agent"
        ? currentMessages[currentMessages.length - 1].text
        : "";
      const completedText = text || latestAssistantText;
      if (target === "side") {
        setSideMessages((prev) => completeAssistant(prev, text, status));
      } else {
        setMessages((prev) => completeAssistant(prev, text, status));
      }
      if (status === "complete") speakAssistantText(completedText);
      setBusy(false);
      const sid = ev.session_id;
      if (sid) {
        const usage = contextUsageFromPayload(payload);
        if (usage) setContextUsage(usage);
        window.setTimeout(() => void refreshSessionMeta(gateway, sid), 400);
        window.setTimeout(() => void refreshContextUsage(gateway, sid), 500);
        window.setTimeout(() => void refreshSessionMeta(gateway, sid), 2400);
      }
    });
    const offError = gateway.on("error", (ev) => {
      const message = textFromPayload(ev.payload) || "Hermes-Gateway-Fehler";
      setError(message);
      setBusy(false);
    });
    const updateTools = (payload: unknown, status: ToolCallSummary["status"]) => {
      if (!payload || typeof payload !== "object") return;
      const target = activeTurnModeRef.current || conversationModeRef.current;
      const data = payload as Record<string, unknown>;
      if (target === "side") setSideToolCalls((prev) => upsertToolCall(prev, data, status, activeSideToolAnchorRef.current));
      else setToolCalls((prev) => upsertToolCall(prev, data, status, activeToolAnchorRef.current));
    };
    const offToolStart = gateway.on("tool.start", (ev) => updateTools(ev.payload, "running"));
    const offToolComplete = gateway.on("tool.complete", (ev) => updateTools(ev.payload, "done"));
    const pushApproval = (ev: GatewayEvent) => {
      const detail = textFromPayload(ev.payload) || "Eine Aktion wartet auf Freigabe.";
      setApprovals((prev) => [{ id: newId("approval"), detail }, ...prev].slice(0, 4));
      setActiveStatusModal("approvals");
    };
    const offApproval = gateway.on("approval.request", pushApproval);
    const offClarify = gateway.on("clarify.request", pushApproval);
    const offTitle = gateway.on<{ title?: string }>("session.title", (ev) => {
      const title = ev.payload?.title?.trim();
      if (title) setSessionTitle(title);
    });
    const offSessionInfo = gateway.on("session.info", (ev) => {
      const payload = (ev.payload ?? {}) as Record<string, unknown>;
      const usage = contextUsageFromPayload(payload);
      if (usage) setContextUsage(usage);
      const persistentSessionId = typeof payload.session_id === "string" ? payload.session_id.trim() : "";
      if (persistentSessionId) {
        setActiveSessionKey(persistentSessionId);
        storeActiveSessionId(persistentSessionId);
      }
      const sid = ev.session_id;
      if (sid) {
        void refreshSessionMeta(gateway, sid);
        void refreshRuntimeStatus(gateway, sid);
        void refreshContextUsage(gateway, sid);
      }
    });

    async function connect() {
      try {
        setConnection("connecting");
        await gateway.connect();
        const storedSessionId = readStoredSessionId();
        let result: SessionOpenResult;
        if (storedSessionId) {
          try {
            result = await gateway.request<SessionOpenResult>("session.resume", { session_id: storedSessionId, cols: 100 }, 30_000);
          } catch {
            storeActiveSessionId(null);
            result = await gateway.request<SessionOpenResult>("session.create", { cols: 100 });
          }
        } else {
          result = await gateway.request<SessionOpenResult>("session.create", { cols: 100 });
        }
        if (!cancelled) {
          const persistentSessionId = persistentSessionIdFromOpenResult(result);
          setSessionId(result.session_id);
          setActiveSessionKey(persistentSessionId || result.session_id);
          storeActiveSessionId(persistentSessionId);
          setSessionTitle("Neue Unterhaltung");
          setLiveNotes(null);
          setConversationMode("main");
          conversationModeRef.current = "main";
          activeTurnModeRef.current = "main";
          setActiveTurnMode("main");
          setSideMessages([]);
          setSideToolCalls([]);
          setToolCalls(result.resumed ? toolCallsFromGateway(result.messages) : []);
          setMessages(result.resumed ? messagesWithInflight(result.messages, result.inflight) : [welcomeMessage()]);
          setBusy(Boolean(result.running || result.status === "working" || result.status === "waiting" || result.inflight?.streaming));
          if (result.inflight?.streaming) activeTurnModeRef.current = "main";
          void refreshSessionMeta(gateway, result.session_id);
          void refreshRuntimeStatus(gateway, result.session_id);
          void refreshContextUsage(gateway, result.session_id);
        }
      } catch (e) {
        if (!cancelled) {
          setConnection("error");
          setError(e instanceof Error ? e.message : String(e));
        }
      }
    }

    void connect();
    api
      .getSessions(10, 0, { excludeSources: ["cron"] })
      .then((res) => {
        if (!cancelled) setRecentSessions((res.sessions ?? []).slice(0, 10));
      })
      .catch(() => undefined);
    api
      .getModelInfo()
      .then((info) => {
        if (!cancelled) setModelInfo(info);
      })
      .catch(() => {
        if (!cancelled) setModelInfo(null);
      });

    return () => {
      cancelled = true;
      offState();
      offDelta();
      offComplete();
      offError();
      offToolStart();
      offToolComplete();
      offApproval();
      offClarify();
      offTitle();
      offSessionInfo();
      gateway.close();
    };
  }, [refreshContextUsage, refreshSessionMeta, refreshRuntimeStatus, speakAssistantText]);

  const loadRecentSession = useCallback(async (session: RecentSession) => {
    const gateway = gatewayRef.current;
    const selectedSessionId = session.id;
    const title = recentSessionDisplayTitle(session);
    setError(null);
    setBusy(false);
    setIsCompressing(false);
    stopReadAloud();
    try {
      let runtimeSessionId = selectedSessionId;
      let history: GatewayHistoryMessage[] | undefined;
      let inflight: SessionInflightTurn | null | undefined;
      let resumedRunning = false;
      if (gateway) {
        const result = await gateway.request<SessionOpenResult>(
          "session.resume",
          { session_id: selectedSessionId, cols: 100 },
          30_000,
        );
        runtimeSessionId = result.session_id || selectedSessionId;
        history = result.messages;
        inflight = result.inflight;
        resumedRunning = Boolean(result.running || result.status === "working" || result.status === "waiting" || result.inflight?.streaming);
      }
      if (!history) {
        const result = await api.getSessionMessages(selectedSessionId);
        runtimeSessionId = result.session_id || runtimeSessionId;
        history = result.messages.map((message) => ({
          role: message.role,
          content: message.content,
          name: message.tool_name,
          tool_name: message.tool_name,
          context: message.content ?? undefined,
        }));
      }

      setSessionId(runtimeSessionId);
      sessionIdRef.current = runtimeSessionId;
      setActiveSessionKey(selectedSessionId);
      storeActiveSessionId(selectedSessionId);
      setSessionTitle(title);
      setLiveNotes(null);
      setConversationMode("main");
      conversationModeRef.current = "main";
      activeTurnModeRef.current = "main";
      activeToolAnchorRef.current = undefined;
      activeSideToolAnchorRef.current = undefined;
      setActiveTurnMode("main");
      setSideMessages([]);
      setSideToolCalls([]);
      setToolCalls(toolCallsFromGateway(history));
      setMessages(messagesWithInflight(history, inflight));
      setBusy(resumedRunning);
      if (inflight?.streaming) activeTurnModeRef.current = "main";
      if (gateway) {
        void refreshSessionMeta(gateway, runtimeSessionId);
        void refreshRuntimeStatus(gateway, runtimeSessionId);
        void refreshContextUsage(gateway, runtimeSessionId);
      }
      showToast("Session geladen");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      showToast("Session konnte nicht geladen werden");
    }
  }, [refreshContextUsage, refreshRuntimeStatus, refreshSessionMeta, showToast, stopReadAloud]);

  const startNewSession = useCallback(async () => {
    const gateway = gatewayRef.current;
    if (!gateway) return;
    setError(null);
    setBusy(false);
    setIsCompressing(false);
    stopReadAloud();
    try {
      const result = await gateway.request<SessionOpenResult>("session.create", { cols: 100 }, 30_000);
      const persistentSessionId = persistentSessionIdFromOpenResult(result);
      const nextSessionId = result.session_id;
      const activeId = persistentSessionId || nextSessionId;
      setSessionId(nextSessionId);
      sessionIdRef.current = nextSessionId;
      setActiveSessionKey(activeId);
      storeActiveSessionId(activeId);
      setMessages([welcomeMessage()]);
      setSessionTitle("Neue Unterhaltung");
      setLiveNotes(null);
      setContextUsage(null);
      setConversationMode("main");
      conversationModeRef.current = "main";
      activeTurnModeRef.current = "main";
      activeToolAnchorRef.current = undefined;
      activeSideToolAnchorRef.current = undefined;
      setActiveTurnMode("main");
      setActivePanelTab("chat");
      setDocumentTabs([]);
      setInput("");
      setSideInput("");
      setSideMessages([]);
      setSideToolCalls([]);
      setToolCalls([]);
      setApprovals([]);
      setActiveStatusModal(null);
      setAttachedFiles([]);
      if (fileInputRef.current) fileInputRef.current.value = "";
      setRecentSessions((current) => {
        if (!activeId) return current;
        const withoutDuplicate = current.filter((session) => session.id !== activeId);
        return [{ id: activeId, title: "Neue Unterhaltung" }, ...withoutDuplicate].slice(0, 10);
      });
      void refreshRuntimeStatus(gateway, nextSessionId);
      void refreshContextUsage(gateway, nextSessionId);
      void refreshRecentSessions(activeId ? { id: activeId, title: "Neue Unterhaltung" } : undefined);
      showToast("Neue Unterhaltung gestartet");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      showToast("Neue Unterhaltung konnte nicht gestartet werden");
    }
  }, [refreshContextUsage, refreshRecentSessions, refreshRuntimeStatus, showToast, stopReadAloud]);

  const submit = async (target: ConversationMode = conversationModeRef.current) => {
    const text = (target === "side" ? sideInput : input).trim();
    const gateway = gatewayRef.current;
    const attachments = target === "main" ? attachedFiles : [];
    if ((!text && attachments.length === 0) || !gateway || !sessionId) return;
    const attachmentNames = attachments.map((file) => file.name).join(", ");
    const submitText = text || `Anhänge: ${attachmentNames || "Dateien"}`;
    const gatewayText = text && attachmentNames ? `${text}\n\nAnhänge: ${attachmentNames}` : submitText;

    if (busy) {
      if (!text) return;
      const steerText = text;
      try {
        const result = await gateway.request<{ status?: string }>("session.steer", { session_id: sessionId, text: steerText }, 15_000);
        if (result.status !== "queued") {
          setError("Lenkung wurde nicht übernommen.");
          return;
        }
        const userMessageId = newId("user");
        const userMessage: ChatMessage = { id: userMessageId, role: "user", text: steerText, status: "complete" };
        if (target === "side") {
          setSideInput("");
          setSideMessages((prev) => insertUserGuidance(prev, userMessage));
        } else {
          setInput("");
          setMessages((prev) => insertUserGuidance(prev, userMessage));
        }
        setError(null);
        showToast(
          attachments.length > 0
            ? "Lenkung gesendet · Anhänge bleiben für die nächste Nachricht bereit"
            : "Lenkung an die laufende Antwort gesendet",
        );
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
      return;
    }

    activeTurnModeRef.current = target;
    setActiveTurnMode(target);
    setBusy(true);
    setError(null);
    try {
      const uploadFiles = attachments.map((attachment) => attachment.file).filter((file): file is File => Boolean(file));
      const uploaded = uploadFiles.length > 0
        ? (await api.uploadAssistantAttachments(uploadFiles, sessionId)).attachments
        : attachments.map((attachment) => attachment.uploaded).filter((attachment): attachment is AssistantUploadedAttachment => Boolean(attachment));
      if (target === "side") {
        const userMessageId = newId("user");
        activeSideToolAnchorRef.current = userMessageId;
        setSideInput("");
        setSideMessages((prev) => [...prev, { id: userMessageId, role: "user", text: submitText, status: "complete" }]);
      } else {
        const userMessageId = newId("user");
        activeToolAnchorRef.current = userMessageId;
        setInput("");
        setAttachedFiles([]);
        if (fileInputRef.current) fileInputRef.current.value = "";
        setMessages((prev) => [...prev, { id: userMessageId, role: "user", text: submitText, status: "complete", attachments }]);
      }
      await gateway.request("prompt.submit", { session_id: sessionId, text: gatewayText, attachments: uploaded });
      window.setTimeout(() => void refreshSessionMeta(gateway, sessionId), 800);
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      setError(message);
      setBusy(false);
    }
  };

  const setRuntimeConfig = async (key: "busy" | "reasoning" | "fast" | "yolo", value: string, label: string) => {
    const gateway = gatewayRef.current;
    showToast(label);
    if (!gateway || !sessionId) return;
    try {
      const result = await gateway.request<{ value?: string; display?: string }>(
        "config.set",
        { session_id: sessionId, key, value },
      );
      if (key === "busy") {
        setRuntimeStatus((current) => ({ ...current, busyMode: result.value || value }));
      } else if (key === "fast") {
        setRuntimeStatus((current) => ({ ...current, fastMode: result.value || value }));
      } else if (key === "yolo") {
        setRuntimeStatus((current) => ({ ...current, yoloMode: result.value || value }));
      } else if (key === "reasoning") {
        if (value === "show" || value === "hide") {
          setRuntimeStatus((current) => ({ ...current, reasoningDisplay: result.value || value }));
        } else {
          setRuntimeStatus((current) => ({ ...current, reasoningEffort: result.value || value }));
        }
      }
      window.setTimeout(() => void refreshRuntimeStatus(gateway, sessionId), 600);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const startSideSession = async () => {
    const gateway = gatewayRef.current;
    setConversationMode("side");
    conversationModeRef.current = "side";
    activeTurnModeRef.current = "side";
    activeSideToolAnchorRef.current = undefined;
    setActiveTurnMode("side");
    setSideMessages([]);
    setSideToolCalls([]);
    setSideInput("");
    showToast("Nebenunterhaltung starten");
    if (!gateway || !sessionId) return;
    try {
      const result = await gateway.request<{ side_session_id?: string }>("session.side.start", { session_id: sessionId });
      if (result.side_session_id) {
        setActiveSessionKey(result.side_session_id);
        storeActiveSessionId(result.side_session_id);
      }
      void refreshSessionMeta(gateway, sessionId);
      void refreshRuntimeStatus(gateway, sessionId);
    } catch (e) {
      setConversationMode("main");
      conversationModeRef.current = "main";
      activeTurnModeRef.current = "main";
      activeToolAnchorRef.current = undefined;
      activeSideToolAnchorRef.current = undefined;
      setActiveTurnMode("main");
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const returnFromSideSession = async () => {
    const gateway = gatewayRef.current;
    setConversationMode("main");
    conversationModeRef.current = "main";
    activeTurnModeRef.current = "main";
    setActiveTurnMode("main");
    showToast("Zurück zur Hauptsitzung");
    if (!gateway || !sessionId) return;
    try {
      const result = await gateway.request<{ parent_session_id?: string }>("session.side.back", { session_id: sessionId });
      if (result.parent_session_id) {
        setActiveSessionKey(result.parent_session_id);
        storeActiveSessionId(result.parent_session_id);
      }
      void refreshSessionMeta(gateway, sessionId);
      void refreshRuntimeStatus(gateway, sessionId);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const sendSlash = async (command: string, label: string) => {
    const gateway = gatewayRef.current;
    if (command === "/side") {
      showToast(label);
      void startSideSession();
      return;
    }
    if (command === "/back") {
      showToast(label);
      void returnFromSideSession();
      return;
    }
    if (command === "/new") {
      void startNewSession();
      return;
    }
    if (command === "/compress") {
      if (!gateway || !sessionId) return;
      if (!canCompress) {
        showToast("Kontext noch zu klein zum Komprimieren");
        return;
      }
      setIsCompressing(true);
      showToast(label);
      try {
        await gateway.request("slash.exec", { session_id: sessionId, command: "compress" }, 60_000);
        window.setTimeout(() => void refreshContextUsage(gateway, sessionId), 300);
        window.setTimeout(() => void refreshRuntimeStatus(gateway, sessionId), 600);
        window.setTimeout(() => void refreshSessionMeta(gateway, sessionId), 600);
        window.setTimeout(() => void refreshContextUsage(gateway, sessionId), 1600);
        window.setTimeout(() => void refreshContextUsage(gateway, sessionId), 3200);
        showToast("Kontext komprimiert");
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
        showToast("Komprimierung fehlgeschlagen");
      } finally {
        setIsCompressing(false);
      }
      return;
    }
    showToast(label);
    if (!gateway || !sessionId) return;
    try {
      await gateway.request("slash.exec", { session_id: sessionId, command: command.replace(/^\//, "") });
      window.setTimeout(() => void refreshRuntimeStatus(gateway, sessionId), 900);
      window.setTimeout(() => void refreshSessionMeta(gateway, sessionId), 900);
    } catch {
      try {
        await gateway.request("prompt.submit", { session_id: sessionId, text: command });
      } catch {
        /* surfaced via gateway error handler */
      }
    }
  };

  const toggleConversationMode = () => {
    if (conversationMode === "side") {
      void returnFromSideSession();
    } else {
      void startSideSession();
    }
  };

  const resolveApproval = async (id: string, approve: boolean) => {
    const gateway = gatewayRef.current;
    const remaining = approvals.filter((a) => a.id !== id);
    setApprovals(remaining);
    if (remaining.length === 0) setActiveStatusModal(null);
    showToast(approve ? "Freigegeben" : "Abgelehnt");
    if (!gateway || !sessionId) return;
    try {
      await gateway.request("approval.respond", {
        session_id: sessionId,
        choice: approve ? "once" : "deny",
      });
    } catch {
      /* surfaced via gateway error handler */
    }
  };

  const showMainThinking = busy && activeTurnMode !== "side" && messages[messages.length - 1]?.status !== "streaming";
  const showSideThinking = busy && activeTurnMode === "side" && sideMessages[sideMessages.length - 1]?.status !== "streaming";
  const mainHasAnchoredToolCalls = toolCalls.some((tool) => tool.anchorMessageId);
  const sideHasAnchoredToolCalls = sideToolCalls.some((tool) => tool.anchorMessageId);
  const mainUnanchoredToolCalls = mainHasAnchoredToolCalls ? [] : unanchoredTools(toolCalls);
  const sideUnanchoredToolCalls = sideHasAnchoredToolCalls ? [] : unanchoredTools(sideToolCalls);
  const mainToolDisclosureIndex = toolDisclosureInsertIndex(messages, mainUnanchoredToolCalls);
  const sideToolDisclosureIndex = toolDisclosureInsertIndex(sideMessages, sideUnanchoredToolCalls);

  const fontStack =
    'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';

  return (
    <div className="min-h-dvh bg-[#f4f1ec] text-[#292720]" style={{ fontFamily: fontStack }}>
      <style>{`
        .aiwerk-assistant { font-size: 16px; line-height: normal; }
        .aiwerk-assistant, .aiwerk-assistant * { font-family: ${fontStack} !important; }
        .aiwerk-assistant ::selection {
          background: rgba(139, 114, 78, .34);
          color: #241f18;
        }
        .aiwerk-assistant aside ::selection,
        .aiwerk-assistant [data-selection-surface="dark"] ::selection {
          background: rgba(215, 185, 142, .48);
          color: #fffaf2;
        }
        .aiwerk-assistant button:not(:disabled):not([data-aiwerk-resize-handle="true"]),
        .aiwerk-assistant [role="button"]:not([aria-disabled="true"]):not([data-aiwerk-resize-handle="true"]) {
          cursor: pointer;
        }
        .aiwerk-assistant button:disabled,
        .aiwerk-assistant [aria-disabled="true"] {
          cursor: not-allowed;
        }
        @keyframes aiwerk-thinking-pulse {
          0%, 80%, 100% { opacity: .32; transform: translateY(0); }
          40% { opacity: 1; transform: translateY(-3px); }
        }
        .aiwerk-thinking-dot { animation: aiwerk-thinking-pulse 1.2s ease-in-out infinite; }
        .aiwerk-thinking-dot:nth-child(2) { animation-delay: .15s; }
        .aiwerk-thinking-dot:nth-child(3) { animation-delay: .3s; }
        @keyframes aiwerk-loader-orbit {
          to { transform: rotate(360deg); }
        }
        @keyframes aiwerk-loader-breathe {
          0%, 100% { opacity: .48; transform: scale(.96); }
          50% { opacity: 1; transform: scale(1); }
        }
        .aiwerk-dashboard-loader-ring { animation: aiwerk-loader-orbit 1.05s linear infinite; }
        .aiwerk-dashboard-loader-dot { animation: aiwerk-loader-breathe 1.4s ease-in-out infinite; }
        .aiwerk-scrollbar { scrollbar-width: thin; scrollbar-color: rgba(139, 114, 78, .42) transparent; }
        .aiwerk-scrollbar::-webkit-scrollbar { width: 7px; height: 7px; }
        .aiwerk-scrollbar::-webkit-scrollbar-track { background: transparent; }
        .aiwerk-scrollbar::-webkit-scrollbar-thumb {
          background: rgba(139, 114, 78, .34);
          border: 2px solid transparent;
          border-radius: 999px;
          background-clip: padding-box;
        }
        .aiwerk-scrollbar::-webkit-scrollbar-thumb:hover { background: rgba(139, 114, 78, .52); background-clip: padding-box; }
        .aiwerk-chat-panel {
          position: relative;
          isolation: isolate;
        }
        .aiwerk-side-drawer {
          position: absolute;
          inset: 0 0 0 auto;
          z-index: 5;
          width: min(520px, 68%);
          display: grid;
          grid-template-rows: auto minmax(0, 1fr) auto;
          border-left: 1px solid #d8cdbc;
          background: rgba(255,250,242,.98);
          box-shadow: -22px 0 48px rgba(56,42,20,.18);
          transform: translateX(104%);
          visibility: hidden;
          transition: transform .22s ease, visibility 0s linear .22s;
        }
        .aiwerk-side-drawer[data-open="true"] {
          transform: translateX(0);
          visibility: visible;
          transition-delay: 0s;
        }
        .aiwerk-resource-stack > * + * {
          position: relative;
        }
        .aiwerk-resource-stack > * + *::before {
          content: "";
          position: absolute;
          left: 18px;
          right: 18px;
          top: -8px;
          height: 1px;
          border-radius: 999px;
          background: linear-gradient(90deg, transparent, rgba(139, 114, 78, .28), transparent);
          pointer-events: none;
        }
        .aiwerk-resource-stack > * + *::after {
          content: "";
          position: absolute;
          left: 50%;
          top: -10px;
          width: 22px;
          height: 3px;
          border-radius: 999px;
          background: rgba(139, 114, 78, .18);
          transform: translateX(-50%);
          pointer-events: none;
        }
        .aiwerk-side-scrim {
          position: absolute;
          inset: 0;
          z-index: 4;
          background: rgba(41,39,32,.10);
          opacity: 0;
          pointer-events: none;
          transition: opacity .18s ease;
        }
        .aiwerk-side-scrim[data-open="true"] {
          opacity: 1;
          pointer-events: auto;
        }
        @media (max-width: 900px) {
          .aiwerk-side-drawer { width: min(460px, 92%); }
        }
      `}</style>
      {showDashboardLoader && (
        <div className="fixed inset-0 z-[80] grid place-items-center bg-[#f4f1ec]/88 px-[22px] backdrop-blur-[3px]" role="status" aria-live="polite" aria-label="Dashboard wird synchronisiert">
          <div className="w-full max-w-[360px] rounded-[26px] border border-[#dccfbd] bg-[#fffaf2] px-[24px] py-[22px] text-center shadow-[0_28px_90px_rgba(48,38,22,.20)]">
            <div className="relative mx-auto grid h-[58px] w-[58px] place-items-center rounded-[20px] bg-[#f5eadb] text-[#705334]">
              <div className="aiwerk-dashboard-loader-ring absolute inset-[7px] rounded-full border-2 border-[#d8c4a8] border-t-[#8a6842]" />
              <span className="aiwerk-dashboard-loader-dot h-[10px] w-[10px] rounded-full bg-[#8a6842] shadow-[0_0_0_7px_rgba(138,104,66,.12)]" />
            </div>
            <h2 className="m-0 mt-[16px] text-[18px] font-bold tracking-[-0.02em] text-[#302b24]">Dashboard wird synchronisiert</h2>
            <p className="m-0 mt-[7px] text-[13px] leading-[1.45] text-[#756a5b]">{dashboardLoaderStep}…</p>
            <div className="mt-[15px] h-[7px] overflow-hidden rounded-full bg-[#eadfce]">
              <div className="h-full w-2/3 rounded-full bg-[#8a6842] transition-all" />
            </div>
          </div>
        </div>
      )}
      <div className="aiwerk-assistant grid h-dvh min-h-0 grid-cols-1 overflow-hidden lg:grid-cols-[380px_1fr]">
        {/* Sidebar */}
        <aside className="hidden h-dvh min-h-0 flex-col gap-[20px] overflow-hidden bg-[#292720] p-[24px] text-[#f8f4ed] lg:flex">
          <div className="flex items-center gap-[12px]">
            <div className="grid h-[48px] w-[48px] place-items-center rounded-[15px] bg-[#d7b98e] text-[20px] font-extrabold text-[#292720]">
              {assistantInitial}
            </div>
            <div>
              <strong className="text-[20px]">{assistantName}</strong>
              <br />
              <small className="text-[14px] text-[#bfb7aa]">AIWerk Persönlicher KI-Assistent</small>
            </div>
          </div>

          <section className="grid gap-[10px]">
            <div className="text-xs font-bold uppercase tracking-[0.12em] text-[#bfb7aa]">STATUS</div>
            <div className="rounded-[18px] border border-white/10 bg-white/[0.08] p-[16px]">
              <span className="mt-[4px] inline-flex items-center gap-[8px] text-[17px] font-bold text-[#f8f4ed]">
                <span
                  className="h-2 w-2 rounded-full"
                  style={{ background: statusInfo.dot, boxShadow: statusInfo.glow }}
                />
                {statusLabel}
              </span>
              <p className="mt-[10px] text-[13px] text-[#cfc6b8]">
                {statusInfo.detail}
              </p>
            </div>
          </section>

          <section className="grid gap-[10px]">
            <div className="text-xs font-bold uppercase tracking-[0.12em] text-[#bfb7aa]">KONTEXT</div>
            <div className="rounded-[18px] border border-white/10 bg-white/[0.08] p-[16px]">
              <div className="flex items-baseline justify-between gap-[12px]">
                <strong className="text-[17px] text-[#f8f4ed]">{contextUsage?.percent ?? 0}% belegt</strong>
                <span className="text-[12px] font-bold uppercase tracking-[.1em] text-[#cfc6b8]">{contextInfo.label.replace("Kontext: ", "")}</span>
              </div>
              <div className="mt-[11px] h-[7px] overflow-hidden rounded-full bg-white/[0.12]">
                <div
                  className="h-full rounded-full transition-all"
                  style={{ width: `${contextUsage?.percent ?? 0}%`, background: contextInfo.bar }}
                />
              </div>
              <p className="mt-[10px] text-[13px] text-[#cfc6b8]">
                {contextInfo.detail} · {compactNumber(contextUsage?.used)} / {compactNumber(contextUsage?.max)} Tokens
              </p>
            </div>
          </section>

          <section className="grid gap-[10px]">
            <div className="text-xs font-bold uppercase tracking-[0.12em] text-[#bfb7aa]">AKTUELLE SITZUNG</div>
            <div className="rounded-[18px] border border-white/10 bg-white/[0.08] p-[16px]">
              <div className="grid gap-[10px]">
                <div className="flex justify-between gap-[14px] border-b border-white/[0.08] pb-[10px] text-[14px] text-[#dcd4c8]">
                  <span>Sitzungs-ID</span>
                  <strong className="break-all text-right font-mono text-[12px] text-[#f8f4ed]">
                    {activeSessionKey ?? sessionId ?? "Startet…"}
                  </strong>
                </div>
                <div className="flex justify-between gap-[14px] text-[14px] text-[#dcd4c8]">
                  <span>Modell</span>
                  <strong className="max-w-[150px] truncate text-right text-[12px] text-[#f8f4ed]" title={currentModelLabel}>{currentModelLabel}</strong>
                </div>
                <div className="flex justify-between gap-[14px] border-t border-white/[0.08] pt-[10px] text-[14px] text-[#dcd4c8]">
                  <span>Nachrichten</span>
                  <strong className="text-right text-[#f8f4ed]">{sessionMessageCount}</strong>
                </div>
                <div className="flex justify-between gap-[14px] border-t border-white/[0.08] pt-[10px] text-[14px] text-[#dcd4c8]">
                  <span>Kompressionen</span>
                  <strong className="text-right text-[#f8f4ed]">{contextUsage?.compressions ?? 0}</strong>
                </div>
              </div>
            </div>
          </section>

          <section className="grid min-w-0 min-h-0 flex-1 grid-rows-[auto_minmax(0,1fr)] gap-[10px]">
            <div className="text-xs font-bold uppercase tracking-[0.12em] text-[#bfb7aa]">LETZTE SITZUNGEN</div>
            <div className="aiwerk-scrollbar min-w-0 min-h-0 overflow-x-hidden overflow-y-auto overscroll-contain pb-[8px] pr-[18px]">
              <div className="grid min-w-0 content-start gap-[10px] overflow-hidden rounded-[18px] border border-white/10 bg-white/[0.065] p-[14px]">
              {recentSessions.length === 0 ? (
                <p className="text-[13px] text-[#bfb7aa]">Noch keine Historie geladen.</p>
              ) : (
                recentSessions.map((session) => {
                  const title = recentSessionDisplayTitle(session);
                  return (
                  <button
                    key={session.id}
                    type="button"
                    onClick={() => void loadRecentSession(session)}
                    onFocus={(event) => showTruncatedSessionTitle(event.currentTarget, title)}
                    onBlur={hideSessionTitleBubble}
                    onMouseEnter={(event) => showTruncatedSessionTitle(event.currentTarget, title)}
                    onMouseLeave={hideSessionTitleBubble}
                    className="box-border w-full min-w-0 max-w-full cursor-pointer rounded-[13px] border border-white/[0.075] bg-white/[0.045] px-[12px] py-[11px] text-left text-[#f8f4ed] transition hover:bg-white/[0.09]"
                  >
                    <strong className="block min-w-0 truncate text-[14px] leading-[1.25]">{title}</strong>
                  </button>
                  );
                })
              )}
              </div>
            </div>
          </section>
        </aside>

        {/* Main */}
        <main className="grid h-dvh min-h-0 grid-rows-[auto_minmax(0,1fr)] gap-[22px] overflow-hidden p-[26px]">
          <header className="flex flex-col items-start gap-[16px] sm:flex-row sm:items-center sm:justify-between">
            <div>
              <div className="flex items-center gap-[8px]">
                <h1 className="m-0 text-[26px] tracking-[-0.03em]">{sessionTitle}</h1>
                <button
                  type="button"
                  onClick={() => void editSessionTitle()}
                  title="Titel bearbeiten"
                  className="inline-grid h-[32px] w-[32px] cursor-pointer place-items-center rounded-[8px] border border-[#d9d0c1] bg-[#fffaf2] text-[#5d503f] hover:bg-[#f2eadf]"
                >
                  <Pencil className="h-[14px] w-[14px]" />
                </button>
              </div>
              <div className="mt-[10px] max-w-[760px] text-[15px] leading-[1.5] text-[#625a4c]">
                {liveNotesText}
              </div>
              <div className="mt-[12px] flex flex-wrap gap-[8px]">
                {headerBadges.map((badge, index) => (
                  <button
                    key={badge.id}
                    type="button"
                    onClick={() => setActiveStatusModal(badge.id)}
                    aria-label={`${badge.label}. ${badge.help}`}
                    className="group relative cursor-pointer rounded-[8px] border border-[#dbcfbe] bg-[#e9ddcb] px-[10px] py-[6px] text-[12px] font-bold text-[#695a43] transition hover:border-[#b89d72] hover:bg-[#dfcfb7] focus:outline-none focus:ring-2 focus:ring-[#b89d72]/40"
                  >
                    {badge.label}
                    <span
                      className={
                        "pointer-events-none absolute top-[calc(100%+8px)] z-30 hidden w-[230px] rounded-[12px] border border-[#d9d0c1] bg-[#292720] px-[12px] py-[9px] text-left text-[12px] font-medium leading-[1.35] text-[#f8f4ed] shadow-[0_14px_34px_rgba(41,39,32,.22)] group-hover:block group-focus-visible:block " +
                        (index === 0 ? "left-0" : "left-1/2 -translate-x-1/2")
                      }
                    >
                      {badge.help}
                    </span>
                  </button>
                ))}
              </div>
            </div>
            <div className="flex gap-[10px]">
              <button
                type="button"
                onClick={() => void sendSlash("/compress", "Kontext komprimieren")}
                disabled={!canCompress}
                title={compressHelp}
                aria-label={compressHelp}
                className={
                  "rounded-[12px] border px-[14px] py-[10px] font-semibold transition " +
                  (canCompress
                    ? "cursor-pointer border-[#d9d0c1] bg-[#fffaf2] text-[#3a362d] hover:bg-[#f2eadf]"
                    : "cursor-not-allowed border-[#e4dacd] bg-[#f4eee5] text-[#9f9383] opacity-60")
                }
              >
                {isCompressing ? "Komprimiert…" : "Komprimieren"}
              </button>
              <button
                type="button"
                onClick={() => void sendSlash("/new", "Neue Unterhaltung gestartet")}
                className="cursor-pointer rounded-[12px] border border-[#9a7b51] bg-[#8b724e] px-[14px] py-[10px] font-semibold text-white hover:bg-[#7a6342]"
              >
                Neue Unterhaltung
              </button>
            </div>
          </header>

          <section
            ref={contentGridRef}
            className={
              "grid h-full min-h-0 overflow-hidden grid-cols-1 gap-[12px] xl:grid-cols-[minmax(0,1fr)_10px_var(--right-rail-width)] " +
              (isResizingRightRail ? "cursor-col-resize select-none" : "")
            }
            style={{ "--right-rail-width": `${rightRailWidth}px` } as CSSProperties}
          >
            {/* Chat panel */}
            <div
              className={
                "aiwerk-chat-panel relative grid h-full min-h-0 overflow-hidden rounded-[24px] border border-[#ded4c4] bg-[rgba(255,250,242,.86)] shadow-[0_18px_50px_rgba(56,42,20,.08)] " +
                (documentTabs.length > 0 ? "grid-rows-[auto_auto_minmax(0,1fr)_auto]" : "grid-rows-[auto_minmax(0,1fr)_auto]")
              }
              onDragEnter={(e) => {
                if (Array.from(e.dataTransfer.types).includes("Files")) {
                  e.preventDefault();
                  setDraggingAttachment(true);
                }
              }}
              onDragOver={(e) => {
                if (Array.from(e.dataTransfer.types).includes("Files")) {
                  e.preventDefault();
                  setDraggingAttachment(true);
                }
              }}
              onDragLeave={(e) => {
                const nextTarget = e.relatedTarget;
                if (nextTarget instanceof Node && e.currentTarget.contains(nextTarget)) return;
                setDraggingAttachment(false);
              }}
              onDrop={(e) => {
                const files = Array.from(e.dataTransfer.files ?? []);
                if (files.length === 0) return;
                e.preventDefault();
                setDraggingAttachment(false);
                addAttachedFiles(files, "drop");
              }}
            >
              {draggingAttachment && (
                <div className="pointer-events-none absolute inset-0 z-30 grid place-items-center rounded-[24px] border-2 border-dashed border-[#9a7b51] bg-[rgba(251,245,235,.86)] text-center shadow-[inset_0_0_0_1px_rgba(154,123,81,.18)] backdrop-blur-[2px]">
                  <div className="rounded-[18px] border border-[#d8c8b1] bg-[#fffaf2] px-[22px] py-[16px] text-[#4b4235] shadow-[0_18px_45px_rgba(56,42,20,.13)]">
                    <strong className="block text-[15px]">Bild oder Datei hier ablegen</strong>
                    <span className="mt-[5px] block text-[13px] text-[#746855]">Wird als Anhang zur Nachricht hinzugefügt.</span>
                  </div>
                </div>
              )}
              <div className="flex items-center justify-between border-b border-[#e3d9c9] px-[20px] py-[18px]">
                <div>
                  <strong>{chatHeader.title}</strong>
                  <br />
                  <span className="text-[#777063]">{chatHeader.subtitle}</span>
                </div>
                <div className="flex items-center gap-[8px]">
                  <button
                    type="button"
                    onClick={toggleReadAloud}
                    title={readAloudBusy ? "ElevenLabs-Vorlesen wird vorbereitet" : readAloudEnabled ? "Vorlesen ausschalten" : "Antworten vorlesen"}
                    aria-label={readAloudBusy ? "ElevenLabs-Vorlesen wird vorbereitet" : readAloudEnabled ? "Vorlesen ausschalten" : "Antworten vorlesen"}
                    aria-pressed={readAloudEnabled}
                    className={
                      "inline-flex cursor-pointer items-center gap-[9px] rounded-full border px-[11px] py-[7px] text-[12px] font-bold transition disabled:cursor-not-allowed disabled:opacity-45 " +
                      (readAloudEnabled
                        ? "border-[#9a7b51] bg-[#e4d6c0] text-[#3a362d] shadow-[0_10px_22px_rgba(91,70,39,.12)]"
                        : "border-[#d9d0c1] bg-[#f6eee3] text-[#695a43] hover:bg-[#efe6d6]")
                    }
                  >
                    <span className="sr-only">Vorlesen</span>
                    {readAloudEnabled ? <Volume2 className="h-[14px] w-[14px]" /> : <VolumeX className="h-[14px] w-[14px]" />}
                    <span
                      aria-hidden="true"
                      className={
                        "relative h-[20px] w-[38px] rounded-full border transition-colors " +
                        (readAloudEnabled
                          ? "border-[#8b724e] bg-[#8b724e]"
                          : "border-[#cfc3b2] bg-[#ddd2c0]")
                      }
                    >
                      <span
                        className={
                          "absolute top-[1px] h-[16px] w-[16px] rounded-full bg-[#fffaf2] shadow-[0_2px_7px_rgba(41,39,32,.26)] transition-[left] duration-200 ease-out " +
                          (readAloudEnabled ? "left-[19px]" : "left-[1px]")
                        }
                      />
                    </span>
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      stopReadAloud();
                      void sendSlash("/stop", "Laufende Antwort stoppen");
                    }}
                    title="Laufende Antwort stoppen"
                    aria-label="Laufende Antwort stoppen"
                    disabled={!busy || !sessionId || connection !== "open"}
                    className={
                      "cursor-pointer rounded-[10px] border px-[13px] py-[8px] text-[12px] font-bold transition disabled:cursor-not-allowed disabled:opacity-45 " +
                      (busy
                        ? "border-[#9a6a51] bg-[#8b5d4e] text-white shadow-[0_10px_22px_rgba(91,55,39,.18)]"
                        : "border-[#d9d0c1] bg-[#f6eee3] text-[#7a594d] hover:bg-[#efe2d5]")
                    }
                  >
                    Stop
                  </button>
                  <button
                    type="button"
                    onClick={toggleConversationMode}
                    title={foldedTabHelp}
                    aria-label={`${conversationMode === "side" ? "Schliessen" : "Nebenfrage"}. ${foldedTabHelp}`}
                    className={
                      "cursor-pointer rounded-[10px] border px-[13px] py-[8px] text-[12px] font-bold transition " +
                      (conversationMode === "side"
                        ? "border-[#9a7b51] bg-[#8b724e] text-white shadow-[0_10px_22px_rgba(91,70,39,.18)]"
                        : "border-[#d9d0c1] bg-[#efe6d6] text-[#695a43] hover:bg-[#e6dac8]")
                    }
                  >
                    {conversationMode === "side" ? "Schliessen" : "Nebenfrage"}
                  </button>
                </div>
              </div>

              {documentTabs.length > 0 && (
                <div
                  role="tablist"
                  aria-label="Geöffnete Ansichten"
                  className="flex min-w-0 items-end gap-[2px] overflow-hidden border-b border-[#d8cbb9] bg-[#f1e8da] px-[14px] pt-[8px]"
                >
                  <button
                    type="button"
                    role="tab"
                    onClick={() => setActivePanelTab("chat")}
                    aria-selected={activePanelTab === "chat"}
                    className={
                      "-mb-px flex h-[36px] shrink-0 items-center rounded-t-[9px] border px-[14px] text-[12px] font-bold transition " +
                      (activePanelTab === "chat"
                        ? "border-[#d8cbb9] border-b-[#fffaf2] bg-[#fffaf2] text-[#3f382f] shadow-[0_-1px_0_rgba(255,255,255,.55)_inset]"
                        : "border-transparent bg-transparent text-[#746956] hover:border-[#e2d7c8] hover:bg-[#f8f0e5] hover:text-[#4f4639]")
                    }
                  >
                    Chat
                  </button>
                  {documentTabs.map((tab) => {
                    const isActive = activePanelTab === tab.id;
                    const Icon = tab.kind === "email" ? Mail : tab.kind === "calendar" ? CalendarDays : UserRound;
                    const tabPrefix = tab.kind === "email" ? "Mail" : tab.kind === "calendar" ? "Termin" : "Kontakt";
                    return (
                      <div
                        key={tab.id}
                        role="tab"
                        aria-selected={isActive}
                        tabIndex={0}
                        onClick={() => setActivePanelTab(tab.id)}
                        onKeyDown={(event) => {
                          if (event.key === "Enter" || event.key === " ") {
                            event.preventDefault();
                            setActivePanelTab(tab.id);
                          }
                        }}
                        className={
                          "-mb-px flex h-[36px] min-w-[24px] max-w-[260px] flex-[1_1_170px] cursor-pointer items-center rounded-t-[9px] border pl-[5px] pr-[1px] text-[12px] transition focus:outline-none focus:ring-2 focus:ring-[#b9a98f] " +
                          (isActive
                            ? "border-[#d8cbb9] border-b-[#fffaf2] bg-[#fffaf2] text-[#3f382f] shadow-[0_-1px_0_rgba(255,255,255,.55)_inset]"
                            : "border-transparent bg-transparent text-[#746956] hover:border-[#e2d7c8] hover:bg-[#f8f0e5] hover:text-[#4f4639]")
                        }
                      >
                        <button
                          type="button"
                          onClick={() => setActivePanelTab(tab.id)}
                          className="flex min-w-0 flex-1 items-center gap-[7px] overflow-hidden truncate text-left font-bold"
                          title={tab.title}
                        >
                          <Icon className="h-[13px] w-[13px] shrink-0 opacity-75" />
                          <span className="min-w-0 truncate">{tabPrefix}: {tab.title}</span>
                        </button>
                        <button
                          type="button"
                          onClick={(event) => {
                            event.stopPropagation();
                            closeDocumentTab(tab.id);
                          }}
                          className={
                            "ml-[1px] grid h-[16px] w-[16px] shrink-0 place-items-center rounded-[5px] opacity-70 transition hover:opacity-100 " +
                            (isActive ? "hover:bg-[#eee5d7]" : "hover:bg-[#e6dac8]")
                          }
                          aria-label={`${tab.title} schließen`}
                          title="Tab schließen"
                        >
                          <X size={10} />
                        </button>
                      </div>
                    );
                  })}
                </div>
              )}

              {isChatPanelActive ? (
                <div ref={messagesScrollRef} className="aiwerk-scrollbar min-h-0 overflow-y-auto overscroll-contain p-[24px]">
                  <div className="flex min-h-full flex-col gap-[16px]">
                    {messages.map((msg, index) => (
                      <Fragment key={msg.id}>
                        {mainToolDisclosureIndex === index && <ToolCallsDisclosure tools={mainUnanchoredToolCalls} />}
                        {msg.role === "system" ? (
                          <div className="mx-auto max-w-[74%] rounded-[18px] bg-[#f1eadf] px-4 py-3.5 text-center text-[14px] text-[#695a43]">
                            {msg.text}
                          </div>
                        ) : (
                          <div
                            className={
                              msg.role === "user"
                                ? "max-w-[74%] self-end rounded-[18px] rounded-tr-[6px] bg-[#7f6b4d] px-4 py-3.5 text-[15px] leading-[1.45] text-white"
                                : "max-w-[74%] rounded-[18px] rounded-tl-[6px] bg-[#eee5d7] px-4 py-3.5 text-[15px] leading-[1.45]"
                            }
                          >
                            {msg.attachments && msg.attachments.length > 0 && (
                              <AttachmentPreviewGrid attachments={msg.attachments} compact tone={msg.role === "user" ? "dark" : "light"} />
                            )}
                            <MessageText message={msg} className={msg.attachments?.length ? "mt-[10px]" : undefined} />
                          </div>
                        )}
                        {msg.role === "user" && <ToolCallsDisclosure tools={anchoredToolsForMessage(toolCalls, msg.id)} />}
                      </Fragment>
                    ))}
                    {mainToolDisclosureIndex === messages.length && <ToolCallsDisclosure tools={mainUnanchoredToolCalls} />}
                    {showMainThinking && <ThinkingIndicator />}
                    {error && (
                      <div className="max-w-[74%] rounded-[18px] border border-[#c98b7a] bg-[#f6e3dd] px-4 py-3 text-[14px] text-[#7b3b2f]">
                        {error}
                      </div>
                    )}
                  </div>
                </div>
              ) : (
                <DocumentTabPanel tab={activeDocumentTab} />
              )}

              {isChatPanelActive && (
                <div>
                <div className="flex items-end gap-[10px] border-t border-[#e3d9c9] bg-[#fbf5eb] p-[16px]">
                  <button
                    type="button"
                    onClick={() => void startVoiceInput()}
                    disabled={voiceState === "transcribing" || !sessionId || busy || connection !== "open"}
                    title={voiceState === "recording" ? "Aufnahme stoppen" : "Spracheingabe"}
                    aria-label={voiceState === "recording" ? "Aufnahme stoppen" : "Spracheingabe"}
                    className={
                      "grid h-[46px] w-[46px] shrink-0 cursor-pointer place-items-center rounded-[14px] border transition hover:-translate-y-[1px] disabled:cursor-not-allowed disabled:opacity-50 " +
                      (voiceState === "recording"
                        ? "border-[#9a6a51] bg-[#8b5d4e] text-white shadow-[0_10px_22px_rgba(91,55,39,.18)]"
                        : "border-[#d9d0c1] bg-[#fffaf2] text-[#4b4235] hover:bg-[#f2eadf]")
                    }
                  >
                    {voiceState === "recording" ? <Square className="h-[17px] w-[17px]" /> : <Mic className="h-[20px] w-[20px]" />}
                  </button>
                  <button
                    type="button"
                    onClick={() => fileInputRef.current?.click()}
                    title="Datei oder Bild anhängen"
                    className="grid h-[46px] w-[46px] shrink-0 cursor-pointer place-items-center rounded-[14px] border border-[#d9d0c1] bg-[#fffaf2] text-[#4b4235] hover:bg-[#f2eadf]"
                  >
                    <Paperclip className="h-[20px] w-[20px]" />
                  </button>
                  <input
                    ref={fileInputRef}
                    type="file"
                    multiple
                    accept="image/*,.pdf,.doc,.docx,.txt"
                    className="hidden"
                    onChange={(e) => {
                      const files = Array.from(e.target.files ?? []);
                      addAttachedFiles(files, "file");
                      e.currentTarget.value = "";
                    }}
                  />
                  <textarea
                    ref={mainInputRef}
                    rows={1}
                    value={input}
                    onChange={(e) => {
                      setInput(e.target.value);
                      resizeComposerTextarea(e.currentTarget);
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && !e.shiftKey) {
                        e.preventDefault();
                        void submit("main");
                      }
                    }}
                    onPaste={(e) => {
                      const imageFiles = Array.from(e.clipboardData.items)
                        .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
                        .map((item) => item.getAsFile())
                        .filter((file): file is File => Boolean(file));
                      if (imageFiles.length === 0) return;
                      e.preventDefault();
                      addAttachedFiles(imageFiles, "paste");
                    }}
                    placeholder="Nachricht an den Assistenten schreiben…"
                    className="aiwerk-scrollbar min-h-[46px] max-h-[160px] min-w-0 flex-1 resize-none overflow-y-hidden rounded-[14px] border border-[#d9d0c1] bg-white px-[14px] py-[12px] text-[15px] leading-[1.45] outline-none"
                  />
                  <button
                    type="button"
                    onClick={() => void submit("main")}
                    disabled={(!input.trim() && attachedFiles.length === 0) || !sessionId || connection !== "open" || (busy && !input.trim())}
                    className="h-[46px] shrink-0 cursor-pointer rounded-[12px] border border-[#9a7b51] bg-[#8b724e] px-[14px] py-[10px] font-semibold text-white hover:bg-[#7a6342] disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    Senden
                  </button>
                </div>
                {voiceState !== "idle" && (
                  <div className="flex items-center justify-between border-t border-[#eee3d4] bg-[#fbf5eb] px-[18px] py-[10px] text-[13px] text-[#746855]">
                    <span>
                      {voiceState === "recording"
                        ? `Höre zu… ${Math.floor(voiceSeconds / 60)}:${String(voiceSeconds % 60).padStart(2, "0")}`
                        : "Sprache wird erkannt…"}
                    </span>
                    {voiceState === "recording" && (
                      <button
                        type="button"
                        onClick={cancelVoiceInput}
                        className="cursor-pointer rounded-[8px] px-[8px] py-[4px] font-semibold text-[#7a594d] hover:bg-[#efe6d6]"
                      >
                        Abbrechen
                      </button>
                    )}
                  </div>
                )}
                {attachedFiles.length > 0 && (
                  <div className="aiwerk-scrollbar max-h-[190px] overflow-y-auto border-t border-[#eee3d4] bg-[#fbf5eb] px-[18px] py-[12px] overscroll-contain">
                    <div className="mb-[8px] flex items-center justify-between text-[12px] font-semibold text-[#746855]">
                      <span>{attachedFiles.length === 1 ? "1 Anhang" : `${attachedFiles.length} Anhänge`}</span>
                      <button
                        type="button"
                        onClick={clearAttachedFiles}
                        className="cursor-pointer rounded-[8px] px-[8px] py-[4px] text-[#5c5142] hover:bg-[#efe6d6]"
                      >
                        Alle entfernen
                      </button>
                    </div>
                    <AttachmentPreviewGrid attachments={attachedFiles} onRemove={removeAttachedFile} />
                  </div>
                )}
                </div>
              )}

              <button
                type="button"
                data-open={conversationMode === "side"}
                className="aiwerk-side-scrim"
                aria-label="Nebenunterhaltung schließen"
                aria-hidden={conversationMode !== "side"}
                tabIndex={conversationMode === "side" ? 0 : -1}
                onClick={toggleConversationMode}
              />
              <aside
                className="aiwerk-side-drawer"
                data-open={conversationMode === "side"}
                aria-label="Nebenunterhaltung"
                aria-hidden={conversationMode !== "side"}
              >
                <header className="flex items-start justify-between gap-[14px] border-b border-[#e3d9c9] p-[18px]">
                  <div>
                    <strong>Nebenunterhaltung</strong>
                    <p className="m-0 mt-[6px] text-[13px] leading-[1.35] text-[#777063]">
                      Temporärer Kontext · Schließen kehrt zur Hauptsitzung zurück
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={toggleConversationMode}
                    className="cursor-pointer rounded-[10px] border border-[#d9d0c1] bg-[#fffaf2] px-[10px] py-[7px] text-[12px] font-bold text-[#695a43] hover:bg-[#f2eadf]"
                  >
                    Schliessen
                  </button>
                </header>
                <div className="aiwerk-scrollbar min-h-0 overflow-y-auto overscroll-contain p-[18px]">
                  <div className="flex min-h-full flex-col gap-[14px]">
                    {sideMessages.length === 0 && (
                      <div className="mx-auto max-w-[86%] rounded-[16px] bg-[#f1eadf] px-3 py-2.5 text-center text-[13px] text-[#695a43]">
                        Neue Nebenunterhaltung. Die Hauptsitzung bleibt geparkt.
                      </div>
                    )}
                    {sideMessages.map((msg, index) => (
                      <Fragment key={`side-${msg.id}`}>
                        {sideToolDisclosureIndex === index && <ToolCallsDisclosure tools={sideUnanchoredToolCalls} compact />}
                        {msg.role === "system" ? (
                          <div className="mx-auto max-w-[86%] rounded-[16px] bg-[#f1eadf] px-3 py-2.5 text-center text-[13px] text-[#695a43]">
                            {msg.text}
                          </div>
                        ) : (
                          <div
                            className={
                              msg.role === "user"
                                ? "max-w-[86%] self-end rounded-[16px] rounded-tr-[6px] bg-[#7f6b4d] px-3 py-2.5 text-[14px] leading-[1.45] text-white"
                                : "max-w-[86%] rounded-[16px] rounded-tl-[6px] bg-[#eee5d7] px-3 py-2.5 text-[14px] leading-[1.45]"
                            }
                          >
                            {msg.attachments && msg.attachments.length > 0 && (
                              <AttachmentPreviewGrid attachments={msg.attachments} compact tone={msg.role === "user" ? "dark" : "light"} />
                            )}
                            <MessageText message={msg} className={msg.attachments?.length ? "mt-[8px]" : undefined} compact />
                          </div>
                        )}
                        {msg.role === "user" && <ToolCallsDisclosure tools={anchoredToolsForMessage(sideToolCalls, msg.id)} compact />}
                      </Fragment>
                    ))}
                    {sideToolDisclosureIndex === sideMessages.length && <ToolCallsDisclosure tools={sideUnanchoredToolCalls} compact />}
                    {showSideThinking && <ThinkingIndicator />}
                  </div>
                </div>
                <div className="border-t border-[#e3d9c9] bg-[#fbf5eb] p-[14px]">
                  <div className="flex items-end gap-[10px]">
                    <textarea
                      ref={sideInputRef}
                      rows={1}
                      value={sideInput}
                      onChange={(e) => {
                        setSideInput(e.target.value);
                        resizeComposerTextarea(e.currentTarget);
                      }}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && !e.shiftKey) {
                          e.preventDefault();
                          void submit("side");
                        }
                      }}
                      placeholder="Nachricht in der Nebenunterhaltung…"
                      className="aiwerk-scrollbar min-h-[44px] max-h-[160px] min-w-0 flex-1 resize-none overflow-y-hidden rounded-[14px] border border-[#d9d0c1] bg-white px-[13px] py-[11px] text-[14px] leading-[1.45] outline-none"
                    />
                    <button
                      type="button"
                      onClick={() => void submit("side")}
                      disabled={!sideInput.trim() || !sessionId || connection !== "open"}
                      className="h-[44px] shrink-0 cursor-pointer rounded-[12px] border border-[#9a7b51] bg-[#8b724e] px-[13px] py-[9px] font-semibold text-white hover:bg-[#7a6342] disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      Senden
                    </button>
                  </div>
                </div>
              </aside>
            </div>

            {/* Right rail resize handle */}
            <button
              type="button"
              aria-label="Ressourcenbereich breiter oder schmaler ziehen"
              title="Ressourcenbereich ziehen · Doppelklick setzt zurück"
              onPointerDown={startRightRailResize}
              onDoubleClick={resetRightRailWidth}
              data-aiwerk-resize-handle="true"
              className="group hidden h-full cursor-col-resize items-center justify-center rounded-full focus:outline-none focus:ring-2 focus:ring-[#b89d72]/40 xl:flex"
            >
              <span className="h-[72px] w-[3px] rounded-full bg-[#d8cdbd] transition group-hover:bg-[#b89d72] group-focus-visible:bg-[#b89d72]" />
            </button>

            {/* Right side panels */}
            <ResourcesRail
              resources={resourceSummary}
              loading={resourcesLoading}
              refreshing={resourceRefreshing}
              error={resourcesError}
              activeDocumentTab={activeDocumentTab}
              assistantName={assistantName}
              sessionId={activeSessionKey ?? sessionId ?? undefined}
              sessionTitle={sessionTitle}
              connection={connection}
              onSendSupport={submitSupportMessage}
              onRefresh={(resource) => void refreshResources({ force: true, resource })}
              onReloadMcp={reloadMcpServers}
              onAddTodo={addTodo}
              onUpdateTodoDone={updateTodoDone}
              onAttachResource={attachResourceToSession}
              onAttachTodo={attachTodoToComposer}
              onOpenEmail={openEmailInPanel}
              onOpenCalendar={openCalendarInPanel}
              onOpenContact={openContactInPanel}
            />
          </section>
        </main>
      </div>

      {activeStatusModal && (
        <RuntimeStatusModal
          active={activeStatusModal}
          status={runtimeStatus}
          approvals={approvals}
          onClose={() => {
            if (activeStatusModal === "approvals" && approvals.length > 0) {
              void resolveApproval(approvals[0].id, false);
              return;
            }
            setActiveStatusModal(null);
          }}
          onSetConfig={(key, value, label) => void setRuntimeConfig(key, value, label)}
          onResolveApproval={(id, approve) => void resolveApproval(id, approve)}
        />
      )}

      {sessionTitleBubble && (
        <div
          className="pointer-events-none fixed z-50 rounded-[12px] border border-[#d9d0c1] bg-[#292720] px-[12px] py-[9px] text-[12px] font-medium leading-[1.35] text-[#f8f4ed] shadow-[0_14px_34px_rgba(41,39,32,.25)]"
          style={{
            left: sessionTitleBubble.left,
            top: sessionTitleBubble.top,
            width: sessionTitleBubble.width,
            transform: sessionTitleBubble.placement === "above" ? "translateY(-100%)" : undefined,
          }}
          role="tooltip"
        >
          {sessionTitleBubble.text}
        </div>
      )}

      {toast && (
        <div className="fixed bottom-[24px] right-[24px] z-50 rounded-[14px] bg-[#292720] px-[16px] py-[13px] text-white shadow-[0_16px_40px_rgba(0,0,0,.18)]">
          {toast}
        </div>
      )}
    </div>
  );
}

function MessageText({
  message,
  className = "",
  compact = false,
}: {
  message: ChatMessage;
  className?: string;
  compact?: boolean;
}) {
  const text = message.text || (message.status === "streaming" ? "…" : "");
  const spacing = className ? `${className} block` : "block";

  if (message.role !== "agent") {
    return <span className={`${spacing} whitespace-pre-wrap`}>{text}</span>;
  }

  return (
    <div className={`${spacing} aiwerk-message-markdown ${compact ? "text-[14px]" : "text-[15px]"}`}>
      <Markdown content={text} streaming={message.status === "streaming"} />
    </div>
  );
}

function ToolCallsDisclosure({ tools, compact = false }: { tools: ToolCallSummary[]; compact?: boolean }) {
  const [open, setOpen] = useState(false);
  if (tools.length === 0) return null;
  const running = tools.some((tool) => tool.status === "running");
  const errored = tools.some((tool) => tool.status === "error");
  const noun = tools.length === 1 ? "Schritt" : "Schritte";
  const label = running
    ? `${tools.length} ${noun} läuft`
    : errored
      ? `${tools.length} ${noun} mit Fehler`
      : `${tools.length} ${noun} ausgeführt`;
  const panelId = `tool-calls-${compact ? "side" : "main"}`;

  return (
    <div className={compact ? "max-w-[86%]" : "max-w-[74%]"}>
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((current) => !current)}
        className="inline-flex cursor-pointer items-center justify-center gap-[7px] rounded-[7px] border border-[#d8d4ca] bg-[#f7f5ef] px-[14px] py-[8px] text-[13px] font-normal leading-[1.35] text-[#77736a] transition hover:border-[#ccc7bb] hover:bg-[#f1efe8] hover:text-[#5f5b53] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#b8aa8f]"
      >
        <span className="inline-block w-[10px] text-center">{open ? "⌄" : "›"}</span>
        <span>{label}</span>
        {running && <span className="ml-[2px] h-[7px] w-[7px] rounded-full bg-[#8b724e] animate-pulse" aria-label="läuft" />}
        {errored && <span className="ml-[2px] h-[7px] w-[7px] rounded-full bg-[#c98b7a]" aria-label="Fehler" />}
      </button>
      {open && (
        <div
          id={panelId}
          className="mt-[10px] grid gap-[8px] rounded-[12px] border border-[#ddd4c8] bg-[#fbf7ef] p-[10px] text-[12px] text-[#5f574b] shadow-[0_10px_26px_rgba(56,42,20,.06)]"
        >
          {tools.map((tool, index) => (
            <div key={tool.id} className="rounded-[9px] border border-[#e5dccf] bg-white/60 px-[10px] py-[8px]">
              <div className="flex items-center justify-between gap-[10px]">
                <strong className="min-w-0 truncate font-mono text-[12px] text-[#3f372e]">
                  {index + 1}. {tool.name}
                </strong>
                <span className="shrink-0 text-[11px] text-[#8a8174]">
                  {tool.status === "running" ? "läuft" : tool.status === "error" ? "Fehler" : "fertig"}
                </span>
              </div>
              {tool.context && <p className="m-0 mt-[5px] truncate font-mono text-[11px] text-[#7d7468]">{tool.context}</p>}
              {(tool.summary || tool.details) && (
                <p className="m-0 mt-[6px] max-h-[90px] overflow-y-auto whitespace-pre-wrap rounded-[7px] bg-[#f3eee5] px-[8px] py-[6px] font-mono text-[11px] leading-[1.35] text-[#5f574b]">
                  {tool.summary || tool.details}
                </p>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
function DocumentTabPanel({ tab }: { tab: DocumentTab | null }) {
  if (!tab) {
    return (
      <div className="grid min-h-0 place-items-center bg-[#fffaf2] p-[24px] text-center text-[13px] text-[#776d5f]">
        Kein Tab ausgewählt.
      </div>
    );
  }
  if (tab.status === "loading") {
    return (
      <div className="grid min-h-0 place-items-center bg-[#fffaf2] p-[24px] text-center text-[#6f614e]">
        <div className="rounded-[18px] border border-[#e1d5c4] bg-[#f8f0e5] px-[18px] py-[14px] shadow-[0_12px_30px_rgba(56,42,20,.08)]">
          <strong className="block text-[14px]">{tab.kind === "email" ? "E-Mail wird geöffnet…" : "Termin wird geöffnet…"}</strong>
          {tab.subtitle && <span className="mt-[4px] block text-[12px] text-[#827765]">{tab.subtitle}</span>}
        </div>
      </div>
    );
  }
  if (tab.status === "error") {
    return (
      <div className="grid min-h-0 place-items-center bg-[#fffaf2] p-[24px] text-center text-[#7b3b2f]">
        <div className="rounded-[18px] border border-[#d9aaa0] bg-[#f6e3dd] px-[18px] py-[14px]">
          <strong className="block text-[14px]">{tab.kind === "email" ? "E-Mail konnte nicht geöffnet werden." : tab.kind === "calendar" ? "Termin konnte nicht geöffnet werden." : "Kontakt konnte nicht geöffnet werden."}</strong>
          {tab.error && <span className="mt-[4px] block text-[12px]">{tab.error}</span>}
        </div>
      </div>
    );
  }
  if (tab.kind === "contact") {
    const contact = tab.contact;
    const sourceBadges = (contact?.source_badges ?? []).filter((badge, index, badges) =>
      Boolean(badge) && badges.findIndex((candidate) => candidate.toLowerCase() === badge.toLowerCase()) === index
    );
    const rows = [
      ["Name", contact?.display_name || tab.title],
      ["Organisation", contact?.organization],
      ["Rolle", contact?.role],
      ["E-Mail", contact?.email],
      ["Telefon", contact?.phone],
      ["Quelle", sourceBadges.join(" · ")],
    ].filter((row): row is [string, string] => Boolean(row[1]));
    return (
      <div className="aiwerk-scrollbar min-h-0 overflow-y-auto bg-[#fffaf2] p-[18px]">
        <div className="mx-auto max-w-[720px] overflow-hidden rounded-[22px] border border-[#dfd4c4] bg-[#fffdf8] shadow-[0_16px_42px_rgba(56,42,20,.08)]">
          <div className="flex items-start gap-[14px] border-b border-[#eadfce] bg-[#f8f0e5] px-[18px] py-[16px]">
            <div className="grid h-[42px] w-[42px] shrink-0 place-items-center rounded-[14px] border border-[#d8cbb9] bg-[#fffaf2] text-[#6d5f4d]">
              <UserRound size={20} />
            </div>
            <div className="min-w-0 flex-1">
              <h2 className="m-0 truncate text-[20px] font-bold tracking-[-0.02em] text-[#302b24]">{contact?.display_name || tab.title}</h2>
              {tab.subtitle && <p className="m-0 mt-[4px] truncate text-[13px] text-[#776d5f]">{tab.subtitle}</p>}
            </div>
          </div>
          <div className="grid gap-[8px] p-[16px]">
            {rows.map(([label, value]) => (
              <div key={label} className="grid grid-cols-[120px_minmax(0,1fr)] gap-[12px] rounded-[12px] border border-[#eadfce] bg-[#fffaf2] px-[12px] py-[10px] text-[13px]">
                <span className="font-bold uppercase tracking-[.12em] text-[10px] text-[#9a8b73]">{label}</span>
                <span className="min-w-0 break-words text-[#3f382f]">{value}</span>
              </div>
            ))}
            {!rows.length && <p className="m-0 text-[13px] text-[#776d5f]">Keine Kontaktdetails verfügbar.</p>}
          </div>
        </div>
      </div>
    );
  }
  return (
    <div className="min-h-0 bg-[#fffaf2] p-[12px]">
      <iframe
        title={tab.title}
        srcDoc={tab.html || ""}
        sandbox=""
        className="h-full min-h-0 w-full rounded-[18px] border border-[#dfd4c4] bg-white shadow-[0_12px_30px_rgba(56,42,20,.08)]"
      />
    </div>
  );
}


function ResourcesRail({
  resources,
  loading,
  refreshing,
  error,
  activeDocumentTab,
  assistantName,
  sessionId,
  sessionTitle,
  connection,
  onSendSupport,
  onRefresh,
  onReloadMcp,
  onAddTodo,
  onUpdateTodoDone,
  onAttachResource,
  onAttachTodo,
  onOpenEmail,
  onOpenCalendar,
  onOpenContact,
}: {
  resources: AssistantResourcesResponse | null;
  loading: boolean;
  refreshing: Partial<Record<ResourcePanelKey, boolean>>;
  error: string | null;
  activeDocumentTab: DocumentTab | null;
  assistantName: string;
  sessionId?: string;
  sessionTitle: string;
  connection: ConnectionState;
  onSendSupport: (payload: AssistantSupportRequest) => Promise<{ ok: boolean; support_id: string; delivered: boolean; queued?: boolean }>;
  onRefresh: (resource?: ResourcePanelKey) => void;
  onReloadMcp: () => Promise<void>;
  onAddTodo: (text: string) => Promise<void>;
  onUpdateTodoDone: (id: string, done: boolean) => Promise<void>;
  onAttachResource: (kind: "email" | "calendar_event" | "shared_file" | "contact", item: Record<string, unknown>, label: string) => Promise<void>;
  onAttachTodo: (item: AssistantTodoItem) => void;
  onOpenEmail: (item: AssistantResourceMailItem) => void;
  onOpenCalendar: (item: AssistantResourceEventItem) => void;
  onOpenContact: (item: AssistantContactItem) => void;
}) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({
    email: false,
    calendar: false,
    shared: false,
    vault: false,
    todos: false,
    contacts: false,
    connectors: false,
  });
  const [focusedResourcePanel, setFocusedResourcePanel] = useState<ResourcePanelId | null>(null);
  const [expandedSharedItems, setExpandedSharedItems] = useState<Record<string, boolean>>({});
  const [expandedConnectorItems, setExpandedConnectorItems] = useState<Record<string, boolean>>({});
  const [expandedEmailAccounts, setExpandedEmailAccounts] = useState<Record<string, boolean>>({});
  const [expandedCalendarAccounts, setExpandedCalendarAccounts] = useState<Record<string, boolean>>({});
  const [openingSharedFolder, setOpeningSharedFolder] = useState(false);
  const [reloadingMcp, setReloadingMcp] = useState(false);
  const [updatingTodoId, setUpdatingTodoId] = useState<string | null>(null);
  const [todoModalOpen, setTodoModalOpen] = useState(false);
  const [newTodoText, setNewTodoText] = useState("");
  const [addingTodo, setAddingTodo] = useState(false);
  const [contactSearch, setContactSearch] = useState("");
  const [contactSearchResults, setContactSearchResults] = useState<AssistantContactItem[] | null>(null);
  const [contactSearchLoading, setContactSearchLoading] = useState(false);
  const [contactModalOpen, setContactModalOpen] = useState(false);
  const [contactForm, setContactForm] = useState({ name: "", organization: "", role: "", email: "", phone: "", note: "", link_current_context: false });
  const [optimisticContacts, setOptimisticContacts] = useState<AssistantContactItem[]>([]);
  const [locallyHiddenContactKeys, setLocallyHiddenContactKeys] = useState<Set<string>>(() => new Set());
  const [savingContact, setSavingContact] = useState(false);
  const [hidingContactKeys, setHidingContactKeys] = useState<Set<string>>(() => new Set());
  const [contactError, setContactError] = useState<string | null>(null);
  const [supportModalOpen, setSupportModalOpen] = useState(false);
  const [supportCategory, setSupportCategory] = useState("Agent antwortet falsch");
  const [supportMessage, setSupportMessage] = useState("");
  const [supportDiagnostics, setSupportDiagnostics] = useState(true);
  const [sendingSupport, setSendingSupport] = useState(false);
  const [supportError, setSupportError] = useState<string | null>(null);
  const resourceCardRefs = useRef(new Map<string, HTMLDivElement>());
  const previousResourceRectsRef = useRef<Map<string, DOMRect> | null>(null);
  const setResourceCardRef = useCallback((id: string) => (node: HTMLDivElement | null) => {
    if (node) resourceCardRefs.current.set(id, node);
    else resourceCardRefs.current.delete(id);
  }, []);
  const captureResourcePanelRects = () => {
    previousResourceRectsRef.current = new Map(
      Array.from(resourceCardRefs.current.entries()).map(([id, node]) => [id, node.getBoundingClientRect()]),
    );
  };
  useLayoutEffect(() => {
    const previousRects = previousResourceRectsRef.current;
    if (!previousRects) return;
    previousResourceRectsRef.current = null;
    resourceCardRefs.current.forEach((node, id) => {
      const previous = previousRects.get(id);
      if (!previous) return;
      const next = node.getBoundingClientRect();
      const deltaY = previous.top - next.top;
      if (Math.abs(deltaY) < 1) return;
      node.animate(
        [
          { transform: `translateY(${deltaY}px)` },
          { transform: "translateY(0)" },
        ],
        { duration: 380, easing: "cubic-bezier(.2,.72,.18,1)", fill: "both" },
      );
    });
  }, [focusedResourcePanel, expanded]);
  const toggle = (id: string) => {
    captureResourcePanelRects();
    const panelId = id as ResourcePanelId;
    setFocusedResourcePanel((current) => current === panelId ? null : panelId);
    setExpanded((current) => {
      if (focusedResourcePanel === panelId || current[id]) {
        return { ...current, [id]: false };
      }
      return { ...current, [id]: true };
    });
  };
  const clearFocusedResourcePanel = () => {
    captureResourcePanelRects();
    setFocusedResourcePanel(null);
  };
  const toggleSharedItem = (id: string) => setExpandedSharedItems((current) => ({ ...current, [id]: !current[id] }));
  const toggleConnectorItem = (id: string) => setExpandedConnectorItems((current) => ({ ...current, [id]: !current[id] }));
  const toggleEmailAccount = (id: string) => setExpandedEmailAccounts((current) => ({ ...current, [id]: !current[id] }));
  const toggleCalendarAccount = (id: string) => setExpandedCalendarAccounts((current) => ({ ...current, [id]: !current[id] }));
  const reloadMcpConnectors = async () => {
    if (reloadingMcp) return;
    setReloadingMcp(true);
    try {
      await onReloadMcp();
    } finally {
      setReloadingMcp(false);
    }
  };
  useEffect(() => {
    const query = contactSearch.trim();
    let cancelled = false;
    const timer = window.setTimeout(() => {
      if (!query) {
        if (!cancelled) {
          setContactSearchResults(null);
          setContactSearchLoading(false);
        }
        return;
      }
      setContactSearchLoading(true);
      api.searchCuiContacts(query)
        .then((result) => {
          if (!cancelled) setContactSearchResults(result.items);
        })
        .catch(() => {
          if (!cancelled) setContactSearchResults([]);
        })
        .finally(() => {
          if (!cancelled) setContactSearchLoading(false);
        });
    }, query ? 180 : 0);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [contactSearch]);
  const submitContact = async () => {
    if (savingContact) return;
    if (!contactForm.name.trim() && !contactForm.email.trim() && !contactForm.phone.trim()) {
      setContactError("Name, E-Mail oder Telefon ist nötig.");
      return;
    }
    setSavingContact(true);
    setContactError(null);
    try {
      const result = await api.createCuiContact(contactForm);
      setOptimisticContacts((current) => dedupeContactList([result.contact, ...current]));
      setContactForm({ name: "", organization: "", role: "", email: "", phone: "", note: "", link_current_context: false });
      setContactModalOpen(false);
      setExpanded((current) => ({ ...current, contacts: true }));
      onRefresh("contacts");
    } catch {
      setContactError("Kontakt konnte nicht gespeichert werden.");
    } finally {
      setSavingContact(false);
    }
  };
  const hideContact = async (contact: AssistantContactItem) => {
    const pendingKey = contactPendingKey(contact);
    if (hidingContactKeys.has(pendingKey)) return;
    const hideKeys = contactIdentityKeys(contact);
    if (!hideKeys.length) {
      setContactError("Kontakt konnte nicht eindeutig erkannt werden.");
      return;
    }
    setHidingContactKeys((current) => new Set([...Array.from(current), pendingKey]));
    setContactError(null);
    setLocallyHiddenContactKeys((current) => new Set([...Array.from(current), ...hideKeys]));
    try {
      await api.hideCuiContact(contact);
      onRefresh("contacts");
    } catch {
      setLocallyHiddenContactKeys((current) => {
        const next = new Set(current);
        hideKeys.forEach((key) => next.delete(key));
        return next;
      });
      setContactError("Kontakt konnte nicht ausgeblendet werden.");
    } finally {
      setHidingContactKeys((current) => {
        const next = new Set(current);
        next.delete(pendingKey);
        return next;
      });
    }
  };
  const updateTodoDone = async (id: string, done: boolean) => {
    if (updatingTodoId) return;
    setUpdatingTodoId(id);
    try {
      await onUpdateTodoDone(id, done);
    } catch {
      // Parent stores the customer-facing error message.
    } finally {
      setUpdatingTodoId(null);
    }
  };
  const submitNewTodo = async () => {
    const text = newTodoText.trim();
    if (!text || addingTodo) return;
    setAddingTodo(true);
    try {
      await onAddTodo(text);
      setNewTodoText("");
      setTodoModalOpen(false);
      setExpanded((current) => ({ ...current, todos: true }));
    } catch {
      // Parent stores the customer-facing error message.
    } finally {
      setAddingTodo(false);
    }
  };

  const submitSupport = async () => {
    const message = supportMessage.trim();
    if (!message || sendingSupport) return;
    setSendingSupport(true);
    setSupportError(null);
    const diagnostics = {
      connection,
      email: resources?.email ? { status: resources.email.status, summary: resources.email.summary, unread_count: resources.email.unread_count } : undefined,
      calendar: resources?.calendar ? { status: resources.calendar.status, summary: resources.calendar.summary } : undefined,
      shared_folder: resources?.shared_folder ? { status: resources.shared_folder.status, summary: resources.shared_folder.summary, source: resources.shared_folder.source } : undefined,
      vault: resources?.vault ? { status: resources.vault.status, summary: resources.vault.summary, source: resources.vault.source } : undefined,
      todos: resources?.todos ? { status: resources.todos.status, summary: resources.todos.summary, open_count: resources.todos.open_count } : undefined,
      contacts: resources?.contacts ? { status: resources.contacts.status, summary: resources.contacts.summary, total_count: resources.contacts.total_count } : undefined,
      connectors: { count: resources?.connectors.length ?? 0 },
    };
    try {
      await onSendSupport({
        category: supportCategory,
        message,
        agent_name: assistantName,
        include_diagnostics: supportDiagnostics,
        session_id: sessionId,
        session_title: sessionTitle,
        connection,
        page_url: window.location.pathname + window.location.search,
        user_agent: navigator.userAgent,
        diagnostics,
      });
      setSupportMessage("");
      setSupportModalOpen(false);
    } catch {
      setSupportError("Nachricht konnte nicht gesendet werden. Bitte später erneut versuchen.");
    } finally {
      setSendingSupport(false);
    }
  };

  const openSharedFolder = async () => {
    if (openingSharedFolder) return;
    if (!resources?.shared_folder.can_open_folder) {
      openSharedFolderCloudUrl(resources?.shared_folder.cloud_url);
      return;
    }
    setOpeningSharedFolder(true);
    try {
      await api.openAssistantSharedFolder();
    } catch {
      openSharedFolderCloudUrl(resources?.shared_folder.cloud_url);
    } finally {
      setOpeningSharedFolder(false);
    }
  };
  const emailStatus = resourceStatusCopy(resources?.email.status);
  const calendarStatus = resourceStatusCopy(resources?.calendar.status);
  const sharedStatus = resourceStatusCopy(resources?.shared_folder.status);
  const vaultStatus = resourceStatusCopy(resources?.vault.status);
  const todoStatus = resourceStatusCopy(resources?.todos.status);
  const contactStatus = resourceStatusCopy(resources?.contacts.status);
  const vaultHintCount = (resources?.vault.weak_count ?? 0) + (resources?.vault.reused_count ?? 0) + (resources?.vault.compromised_count ?? 0);
  const connectorCount = resources?.connectors.length ?? 0;
  const connectorSummary = resources
    ? connectorCount
      ? `${connectorCount} MCP-Server verfügbar`
      : "Keine MCP-Server verfügbar"
    : "Wird geprüft…";
  const emailAccountSections = useMemo(() => {
    if (!resources) return [];
    const allItems = resources.email.items ?? [];
    const accounts = resources.email.accounts?.length
      ? resources.email.accounts
      : [{ label: "Mailbox", address: "Mailbox", source: "", status: resources.email.status, unread_count: resources.email.unread_count, summary: resources.email.summary }];
    return accounts.map((account) => {
      const accountId = account.address ?? account.label;
      const accountItems = account.items?.length
        ? account.items
        : allItems.filter((item) => (item.account_address ?? item.account_label ?? "Mailbox") === accountId);
      return { ...account, id: accountId, items: accountItems as AssistantResourceMailItem[] };
    });
  }, [resources]);
  const calendarAccountSections = useMemo(() => {
    if (!resources) return [];
    const allItems = resources.calendar.items ?? [];
    const accounts = resources.calendar.accounts?.length
      ? resources.calendar.accounts
      : [{ label: "Kalender", address: "Kalender", source: "", status: resources.calendar.status, summary: resources.calendar.summary }];
    return accounts.map((account) => {
      const accountId = account.address ?? account.label;
      const accountItems = account.items?.length
        ? account.items
        : allItems.filter((item) => (item.account_address ?? item.account_label ?? "Kalender") === accountId);
      return { ...account, id: accountId, items: accountItems as AssistantResourceEventItem[] };
    });
  }, [resources]);
  const contactRelevanceWindowDays = resources?.contacts.relevance_window_days ?? 10;
  const defaultContactsSource = resources?.contacts.items?.length
    ? resources.contacts.items
    : resources?.contacts.relevant.length
      ? resources.contacts.relevant
      : resources?.contacts.frequent ?? [];
  const searchBaseContacts = dedupeContactList([...optimisticContacts, ...defaultContactsSource]);
  const defaultContacts = searchBaseContacts.filter(
    (contact) => !contactMatchesKeys(contact, locallyHiddenContactKeys),
  );
  const contactQuery = normalizeContactSearch(contactSearch.trim());
  const localContactMatches = contactQuery
    ? searchBaseContacts.filter((contact) => {
        const haystack = [contact.display_name, contact.organization, contact.role, contact.email, contact.phone, ...(contact.source_badges ?? [])]
          .filter(Boolean)
          .join(" ")
          .normalize("NFD")
          .replace(/[\u0300-\u036f]/g, "")
          .toLowerCase();
        return haystack.includes(contactQuery);
      })
    : [];
  const displayedContacts = contactQuery
    ? (contactSearchResults?.length ? contactSearchResults : localContactMatches).filter(
        (contact) => !contactMatchesKeys(contact, locallyHiddenContactKeys),
      )
    : defaultContacts;
  const contactSectionTitle = contactQuery
    ? contactSearchLoading && localContactMatches.length ? "Lokale Treffer · Suche läuft…" : "Suchresultate"
    : resources?.contacts.relevant.length
      ? "Relevante Kontakte"
      : "Aktive Kontakte";
  const resourceCacheUpdatedAt = resources?.cache?.resources?.email?.updated_at ?? resources?.checked_at;
  const resourceCacheCopy = resourceCacheUpdatedAt
    ? `Zuletzt aktualisiert: ${formatResourceTime(resourceCacheUpdatedAt, { year: true })}`
    : "Aktualisierung nach Bedarf";
  const refreshAction = (resource: ResourcePanelKey, label: string): ResourceCardAction => ({
    icon: <RefreshCw size={13} className={refreshing[resource] ? "animate-spin" : undefined} />,
    label: `${label} aktualisieren`,
    onClick: () => onRefresh(resource),
    disabled: !!refreshing[resource] || loading,
  });
  const openVault = () => {
    const url = resources?.vault.vault_url || "https://pass.aiwerk.ch";
    const opened = window.open(url, "_blank", "noopener,noreferrer");
    if (opened) opened.opener = null;
  };
  return (
    <>
      <aside className="hidden h-full min-h-0 w-full flex-col gap-[14px] overflow-x-hidden overflow-y-auto overscroll-contain pr-[4px] xl:flex aiwerk-scrollbar" aria-label="Ressourcen">
      <div className="min-w-0 shrink-0 overflow-x-hidden rounded-[24px] border border-[#ded4c4] bg-[rgba(255,250,242,.9)] p-[18px] shadow-[0_18px_50px_rgba(56,42,20,.08)]">
        <div className="mb-[14px] flex min-w-0 items-start justify-between gap-[12px]">
          <div className="min-w-0 flex-1">
            <p className="m-0 truncate text-[11px] font-bold uppercase tracking-[.18em] text-[#948873]">Ressourcen</p>
            <h3 className="m-0 mt-[4px] truncate text-[17px] text-[#302b24]">{`Was ${assistantSubjectName(assistantName)} nutzen kann`}</h3>
            <p className="m-0 mt-[5px] truncate text-[11px] text-[#8a7f70]">{resourceCacheCopy}</p>
          </div>
          <button
            type="button"
            onClick={() => onRefresh()}
            className="grid h-[34px] w-[34px] shrink-0 cursor-pointer place-items-center rounded-full border border-[#d9cdbc] bg-[#fffaf2] text-[#6f614e] hover:bg-[#f3eadc] disabled:cursor-wait disabled:opacity-60"
            disabled={loading}
            title="Ressourcen aktualisieren"
            aria-label="Ressourcen aktualisieren"
          >
            <RefreshCw size={15} className={loading ? "animate-spin" : undefined} />
          </button>
        </div>
        {error && <p className="mb-[12px] rounded-[12px] bg-[#f4e1da] px-[10px] py-[8px] text-[12px] text-[#7b3b2f]">Ressourcen konnten nicht geladen werden.</p>}
        <div className={`grid transition-all duration-300 ${focusedResourcePanel ? "gap-0" : "aiwerk-resource-stack gap-[14px]"}`}>
          <ResourceCard
            id="email"
            icon={<Mail size={16} />}
            title="E-Mail"
            summary={resources?.email.summary ?? "Wird geprüft…"}
            status={emailStatus}
            expanded={focusedResourcePanel === "email" || expanded.email}
            onToggle={toggle}
            focused={focusedResourcePanel === "email"}
            hidden={focusedResourcePanel !== null && focusedResourcePanel !== "email"}
            onClearFocus={clearFocusedResourcePanel}
            cardRef={setResourceCardRef("email")}
            badge={resources?.email.unread_count ? String(resources.email.unread_count) : undefined}
            action={refreshAction("email", "E-Mail")}
          >
            {resources && emailAccountSections.length ? (
              <div className="grid gap-[6px]">
                {emailAccountSections.map((account) => {
                  const accountOpen = expandedEmailAccounts[account.id] ?? false;
                  const accountListScrollable = account.items.length > 5;
                  const unreadMailItems = account.items.filter((item) => item.unread);
                  const latestMailItems = account.items.filter((item) => !item.unread);
                  const mailSections = unreadMailItems.length
                    ? [
                        { id: "new", label: "Neue Nachrichten", items: unreadMailItems },
                        ...(latestMailItems.length ? [{ id: "latest", label: "Letzte Nachrichten", items: latestMailItems }] : []),
                      ]
                    : [{ id: "latest", label: account.items.length ? "Letzte Nachrichten" : "", items: latestMailItems }];
                  const renderMailRow = (item: AssistantResourceMailItem, index: number, sectionId: string) => {
                    const canOpenMail = !!item.open_url;
                    const canAttachMail = !!(item.message_id || item.id) && !!(item.account_address || item.account_label || account.id);
                    const key = item.id ?? `${account.id}-${sectionId}-${item.subject}-${index}`;
                    const isOpenMail = activeDocumentTab?.kind === "email" && !!item.open_url && activeDocumentTab.openUrl === item.open_url;
                    const rowClass = `relative w-full min-w-0 max-w-full overflow-hidden rounded-[10px] border px-[9px] py-[7px] text-left ${
                      isOpenMail
                        ? "border-[#c8b48f] bg-[#fff8ec] shadow-[inset_3px_0_0_#b8955f] after:absolute after:right-[7px] after:top-[7px] after:h-[8px] after:w-[8px] after:rounded-bl-[8px] after:border-r after:border-t after:border-[#b8955f]"
                        : "border-transparent bg-[#f1e8dc]"
                    } ${canOpenMail ? "cursor-pointer transition hover:bg-[#eaddcc]" : ""}`;
                    const content = (
                      <>
                        <div className="flex min-w-0 max-w-full items-center gap-[6px] overflow-hidden">
                          {item.unread && <span className="h-[7px] w-[7px] shrink-0 rounded-full bg-[#8b724e]" aria-label="Neu" />}
                          <span className="block min-w-0 flex-1 truncate text-[13px] font-semibold text-[#342f27]">{item.subject || "Ohne Betreff"}</span>
                          {item.has_attachment && <Paperclip size={12} className="shrink-0 text-[#8a7a64]" aria-label="Mit Anhang" />}
                          {canOpenMail && <ExternalLink size={12} className="shrink-0 text-[#8a7a64]" aria-hidden="true" />}
                        </div>
                        <p className="m-0 mt-[2px] truncate text-[12px] text-[#7c705f]">{item.sender || "Unbekannt"}{item.received_at ? ` · ${formatResourceTime(item.received_at)}` : ""}</p>
                      </>
                    );
                    const attachItem = {
                      ...item,
                      account_address: item.account_address ?? account.address ?? account.id,
                      account_label: item.account_label ?? account.label,
                    };
                    return (
                      <div key={key} className="flex min-w-0 max-w-full items-stretch gap-[6px] overflow-hidden">
                        {canOpenMail ? (
                          <button
                            type="button"
                            onClick={() => onOpenEmail(item)}
                            className={rowClass}
                            title={item.open_url?.startsWith("http") ? "Im Webmail öffnen" : "E-Mail anzeigen"}
                          >
                            {content}
                          </button>
                        ) : (
                          <div className={rowClass}>{content}</div>
                        )}
                        <button
                          type="button"
                          onClick={() => void onAttachResource("email", attachItem as Record<string, unknown>, "E-Mail")}
                          disabled={!canAttachMail}
                          title="E-Mail an Agent anhängen"
                          aria-label="E-Mail an Agent anhängen"
                          className={`${RIGHT_RAIL_ROW_ACTION_CLASS} ${RIGHT_RAIL_ROW_ACTION_DISABLED_CLASS}`}
                        >
                          <Paperclip size={13} />
                        </button>
                      </div>
                    );
                  };
                  return (
                    <div key={account.id} className="min-w-0 overflow-hidden rounded-[12px] border border-[#e4d8c6] bg-[#fffaf2]">
                      <button
                        type="button"
                        onClick={() => toggleEmailAccount(account.id)}
                        className="flex w-full min-w-0 max-w-full cursor-pointer items-center justify-between gap-[8px] overflow-hidden px-[9px] py-[7px] text-left text-[12px] text-[#7c705f] hover:bg-[#f8efe3]"
                        aria-expanded={accountOpen}
                      >
                        <span className="flex min-w-0 flex-1 items-center gap-[6px] overflow-hidden">
                          <ChevronRight size={13} className={`shrink-0 transition-transform ${accountOpen ? "rotate-90" : ""}`} />
                          <span className="block min-w-0 max-w-full truncate">{account.address ?? account.label}</span>
                        </span>
                        <span className="shrink-0 font-semibold text-[#6b5a45]">{account.unread_count ? `${account.unread_count} neu` : "0 neu"}</span>
                      </button>
                      {accountOpen && (
                        <div className="border-t border-[#eadfce] bg-[#f8f0e5] px-[8px] py-[7px]">
                          {account.items.length ? (
                            <div className={`grid gap-[7px] ${accountListScrollable ? "max-h-[320px] overflow-y-auto pr-[2px] aiwerk-scrollbar" : ""}`}>
                              {mailSections.map((section) => section.items.length ? (
                                <div key={section.id} className="grid gap-[6px]">
                                  {section.label && <p className="m-0 px-[2px] text-[10px] font-bold uppercase tracking-[.16em] text-[#9a8b73]">{section.label}</p>}
                                  {section.items.map((item, index) => renderMailRow(item, index, section.id))}
                                </div>
                              ) : null)}
                            </div>
                          ) : (
                            <p className="m-0 px-[2px] py-[3px] text-[12px] text-[#8a7f70]">Keine Nachrichten gefunden.</p>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="m-0 text-[13px] text-[#776d5f]">Keine neuen Nachrichten oder noch keine Mailbox angebunden.</p>
            )}
          </ResourceCard>

          <ResourceCard
            id="calendar"
            icon={<CalendarDays size={16} />}
            title="Kalender"
            summary={resources?.calendar.summary ?? "Wird geprüft…"}
            status={calendarStatus}
            expanded={focusedResourcePanel === "calendar" || expanded.calendar}
            onToggle={toggle}
            focused={focusedResourcePanel === "calendar"}
            hidden={focusedResourcePanel !== null && focusedResourcePanel !== "calendar"}
            onClearFocus={clearFocusedResourcePanel}
            cardRef={setResourceCardRef("calendar")}
            action={refreshAction("calendar", "Kalender")}
          >
            {resources && calendarAccountSections.length ? (
              <div className="grid gap-[6px]">
                {calendarAccountSections.map((account) => {
                  const accountOpen = expandedCalendarAccounts[account.id] ?? false;
                  const accountListScrollable = account.items.length > 5;
                  return (
                    <div key={account.id} className="min-w-0 overflow-hidden rounded-[12px] border border-[#e4d8c6] bg-[#fffaf2]">
                      <button
                        type="button"
                        onClick={() => toggleCalendarAccount(account.id)}
                        className="flex w-full min-w-0 max-w-full cursor-pointer items-center justify-between gap-[8px] overflow-hidden px-[9px] py-[7px] text-left text-[12px] text-[#7c705f] hover:bg-[#f8efe3]"
                        aria-expanded={accountOpen}
                      >
                        <span className="flex min-w-0 flex-1 items-center gap-[6px] overflow-hidden">
                          <ChevronRight size={13} className={`shrink-0 transition-transform ${accountOpen ? "rotate-90" : ""}`} />
                          <span className="block min-w-0 max-w-full truncate">{account.label || account.address || "Kalender"}</span>
                        </span>
                        <span className="shrink-0 font-semibold text-[#6b5a45]">{account.items.length} Termine</span>
                      </button>
                      {accountOpen && (
                        <div className="border-t border-[#eadfce] bg-[#f8f0e5] px-[8px] py-[7px]">
                          {account.items.length ? (
                            <div className={`grid gap-[6px] ${accountListScrollable ? "max-h-[320px] overflow-y-auto pr-[2px] aiwerk-scrollbar" : ""}`}>
                              {account.items.map((item, index) => {
                                const key = item.id ?? `${account.id}-${item.title}-${index}`;
                                const attachItem = {
                                  ...item,
                                  account_address: item.account_address ?? account.address ?? account.id,
                                  account_label: item.account_label ?? account.label,
                                };
                                const canOpenCalendar = !!item.open_url;
                                const isOpenCalendar = activeDocumentTab?.kind === "calendar" && !!item.open_url && activeDocumentTab.openUrl === item.open_url;
                                const rowClass = `relative w-full min-w-0 max-w-full overflow-hidden rounded-[10px] border px-[9px] py-[7px] text-left ${
                                  isOpenCalendar
                                    ? "border-[#c8b48f] bg-[#fff8ec] shadow-[inset_3px_0_0_#b8955f] after:absolute after:right-[7px] after:top-[7px] after:h-[8px] after:w-[8px] after:rounded-bl-[8px] after:border-r after:border-t after:border-[#b8955f]"
                                    : "border-transparent bg-[#f1e8dc]"
                                } ${canOpenCalendar ? "cursor-pointer transition hover:bg-[#eaddcc] focus:outline-none focus:ring-2 focus:ring-[#b9a98f]" : ""}`;
                                const content = (
                                  <>
                                    <div className="flex min-w-0 max-w-full items-center gap-[6px] overflow-hidden">
                                      <span className="block min-w-0 flex-1 truncate text-[13px] font-semibold text-[#342f27]">{item.title || "Termin"}</span>
                                      {canOpenCalendar && <ExternalLink size={12} className="shrink-0 text-[#8a7a64]" aria-hidden="true" />}
                                    </div>
                                    <p className="m-0 mt-[2px] truncate text-[12px] text-[#7c705f]">{formatResourceTime(item.starts_at)}{item.location_hint ? ` · ${item.location_hint}` : ""}</p>
                                  </>
                                );
                                return (
                                  <div key={key} className="flex min-w-0 max-w-full items-stretch gap-[6px] overflow-hidden">
                                    {canOpenCalendar ? (
                                      <button
                                        type="button"
                                        onClick={() => onOpenCalendar(item)}
                                        title="Termin anzeigen"
                                        className={rowClass}
                                      >
                                        {content}
                                      </button>
                                    ) : (
                                      <div className={rowClass}>{content}</div>
                                    )}
                                    <button
                                      type="button"
                                      onClick={() => void onAttachResource("calendar_event", attachItem as Record<string, unknown>, "Termin")}
                                      title="Termin an Agent anhängen"
                                      aria-label="Termin an Agent anhängen"
                                      className={RIGHT_RAIL_ROW_ACTION_CLASS}
                                    >
                                      <Paperclip size={13} />
                                    </button>
                                  </div>
                                );
                              })}
                            </div>
                          ) : (
                            <p className="m-0 px-[2px] py-[3px] text-[12px] text-[#8a7f70]">Keine kommenden Termine.</p>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="m-0 text-[13px] text-[#776d5f]">Keine kommenden Termine oder Kalender noch nicht angebunden.</p>
            )}
          </ResourceCard>

          <ResourceCard
            id="contacts"
            icon={<UserRound size={16} />}
            title="Kontakte"
            summary={resources?.contacts.summary ?? "Wird geprüft…"}
            status={contactStatus}
            expanded={focusedResourcePanel === "contacts" || expanded.contacts}
            onToggle={toggle}
            focused={focusedResourcePanel === "contacts"}
            hidden={focusedResourcePanel !== null && focusedResourcePanel !== "contacts"}
            onClearFocus={clearFocusedResourcePanel}
            cardRef={setResourceCardRef("contacts")}
            badge={resources?.contacts.total_count ? String(resources.contacts.total_count) : undefined}
            action={[
              {
                icon: <Plus size={13} />,
                label: "Kontakt hinzufügen",
                onClick: () => setContactModalOpen(true),
                disabled: loading || savingContact,
              },
              refreshAction("contacts", "Kontakte"),
            ]}
          >
            <div className="grid gap-[8px]">
              <div className="flex min-w-0 items-center gap-[6px] rounded-[12px] border border-[#e0d4c3] bg-[#fffdf8] px-[9px] py-[7px]">
                <Search size={13} className="shrink-0 text-[#8a7a64]" />
                <input
                  value={contactSearch}
                  onChange={(event) => setContactSearch(event.target.value)}
                  placeholder="Kontakt suchen..."
                  className="min-w-0 flex-1 bg-transparent text-[13px] text-[#3d362d] outline-none placeholder:text-[#a79b89]"
                />
                {contactSearch && (
                  <button type="button" onClick={() => setContactSearch("")} className="grid h-[20px] w-[20px] place-items-center rounded-[7px] text-[#7b6b57] hover:bg-[#f0e4d4]" aria-label="Suche löschen">
                    <X size={12} />
                  </button>
                )}
              </div>
              <p className="m-0 flex items-center gap-[6px] px-[2px] text-[10px] font-bold uppercase tracking-[.16em] text-[#9a8b73]">
                {contactSearchLoading && <RefreshCw size={11} className="animate-spin text-[#9c8461]" />}
                <span>{contactSearchLoading ? "Sucht Kontakte…" : contactSectionTitle}</span>
              </p>
              {contactSearchLoading && (
                <div className="h-[3px] overflow-hidden rounded-full bg-[#eadfce]" aria-label="Suche läuft">
                  <div className="h-full w-1/2 animate-pulse rounded-full bg-[#b99b69]" />
                </div>
              )}
              {!contactQuery && (
                <p className="m-0 px-[2px] text-[11px] leading-[15px] text-[#8a7f70]">
                  Basierend auf E-Mail-Aktivität der letzten {contactRelevanceWindowDays} Tage plus zuletzt gespeicherten Kontakten. Suche findet auch ältere Kontakte.
                </p>
              )}
              {displayedContacts.length ? (
                <div className="grid max-h-[340px] gap-[8px] overflow-y-auto pr-[2px] aiwerk-scrollbar">
                  {displayedContacts.map((contact) => {
                    const contactMeta = [contact.role, contact.organization].filter(Boolean).join(" · ");
                    const contactMethods = [contact.email, contact.phone].filter(Boolean).join(" · ");
                    const contactSourceBadges = (contact.source_badges ?? []).filter((badge, index, badges) =>
                      Boolean(badge) && badges.findIndex((candidate) => candidate.toLowerCase() === badge.toLowerCase()) === index
                    );
                    const pendingHideKey = contactPendingKey(contact);
                    const isHidePending = hidingContactKeys.has(pendingHideKey);
                    return (
                      <div
                        key={contact.id}
                        onClick={() => onOpenContact(contact)}
                        className="flex min-w-0 cursor-pointer items-center gap-[8px] rounded-[10px] border border-[#d8cbb9] bg-[#fffdf8] px-[9px] py-[8px] text-[12px] text-[#6f6557] shadow-[0_1px_0_rgba(255,255,255,.78)_inset,0_7px_16px_rgba(84,63,32,.05)] transition hover:border-[#c8b48f] hover:bg-[#fff8ec]"
                        role="button"
                        tabIndex={0}
                        onKeyDown={(event) => {
                          if (event.key === "Enter" || event.key === " ") {
                            event.preventDefault();
                            onOpenContact(contact);
                          }
                        }}
                      >
                        <div className="grid h-[28px] w-[28px] shrink-0 place-items-center rounded-[8px] border border-[#e4d8c6] bg-[#f7efe3] text-[#7b6b57]">
                          <UserRound size={13} />
                        </div>
                        <div className="min-w-0 flex-1">
                          <p className="m-0 truncate text-[13px] font-semibold leading-[18px] text-[#342f27]">{contact.display_name || contact.email || contact.phone || "Kontakt"}</p>
                          {contactMeta && <p className="m-0 truncate text-[12px] leading-[16px] text-[#7c705f]">{contactMeta}</p>}
                          {contactMethods && <p className="m-0 truncate text-[11px] leading-[15px] text-[#8a7f70]">{contactMethods}</p>}
                          {contactSourceBadges.length ? (
                            <div className="mt-[5px] flex flex-wrap gap-[4px]">
                              {contactSourceBadges.map((badge) => <span key={badge} className="rounded-full border border-[#e1d3bf] bg-[#f2e8d9] px-[6px] py-[1px] text-[10px] font-bold leading-[13px] text-[#7a6850]">{badge}</span>)}
                            </div>
                          ) : null}
                        </div>
                        <div className="flex shrink-0 gap-[4px]">
                          <button
                            type="button"
                            onClick={(event) => {
                              event.stopPropagation();
                              void hideContact(contact);
                            }}
                            disabled={isHidePending}
                            className="grid h-[26px] w-[26px] place-items-center rounded-[8px] border border-[#e1d3bf] bg-[#fffaf2] text-[#8a7a64] transition hover:bg-[#efe4d4] disabled:cursor-wait disabled:opacity-55"
                            title={contactQuery ? "Aus Suchresultaten ausblenden" : "Aus Liste ausblenden"}
                            aria-label={contactQuery ? "Aus Suchresultaten ausblenden" : "Aus Liste ausblenden"}
                          >
                            <X size={12} />
                          </button>
                          <button
                            type="button"
                            onClick={(event) => {
                              event.stopPropagation();
                              void onAttachResource("contact", contact as unknown as Record<string, unknown>, "Kontakt");
                            }}
                            className="grid h-[26px] w-[26px] place-items-center rounded-[8px] border border-[#d8cbb9] bg-[#f8f0e3] text-[#6d5f4d] transition hover:bg-[#efe4d4]"
                            title="Kontakt an Agent anhängen"
                            aria-label="Kontakt an Agent anhängen"
                          >
                            <Paperclip size={12} />
                          </button>
                          {contact.email && (
                            <a href={`mailto:${contact.email}`} onClick={(event) => event.stopPropagation()} className="grid h-[26px] w-[26px] place-items-center rounded-[8px] border border-[#d8cbb9] bg-[#f8f0e3] text-[#6d5f4d] transition hover:bg-[#efe4d4]" title="E-Mail schreiben" aria-label="E-Mail schreiben">
                              <Mail size={12} />
                            </a>
                          )}
                          {contact.phone && (
                            <a href={`tel:${contact.phone}`} onClick={(event) => event.stopPropagation()} className="grid h-[26px] w-[26px] place-items-center rounded-[8px] border border-[#d8cbb9] bg-[#f8f0e3] text-[#6d5f4d] transition hover:bg-[#efe4d4]" title="Anrufen" aria-label="Anrufen">
                              <Phone size={12} />
                            </a>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <div className="rounded-[12px] border border-[#e4d8c6] bg-[#fffaf2] px-[10px] py-[9px] text-[12px] text-[#776d5f]">
                  {contactSearch.trim() ? "Keine Kontakte gefunden." : `Keine relevanten Kontakte in den letzten ${contactRelevanceWindowDays} Tagen. Suche nutzen oder Kontakt hinzufügen.`}
                </div>
              )}
            </div>
          </ResourceCard>

          <ResourceCard
            id="shared"
            icon={<FolderOpen size={16} />}
            title="Shared Ordner"
            summary={resources?.shared_folder.summary ?? "Wird geprüft…"}
            status={sharedStatus}
            expanded={focusedResourcePanel === "shared" || expanded.shared}
            onToggle={toggle}
            focused={focusedResourcePanel === "shared"}
            hidden={focusedResourcePanel !== null && focusedResourcePanel !== "shared"}
            onClearFocus={clearFocusedResourcePanel}
            cardRef={setResourceCardRef("shared")}
            action={[
              {
                icon: resources?.shared_folder.can_open_folder ? <FolderOpen size={14} /> : <ExternalLink size={14} />,
                label: resources?.shared_folder.can_open_folder
                  ? "Ordner im Dateimanager öffnen"
                  : resources?.shared_folder.cloud_url
                    ? "Ordner in cloud.aiwerk.ch öffnen"
                    : "Cloud-Ordner nicht verfügbar",
                onClick: openSharedFolder,
                disabled: (!resources?.shared_folder.can_open_folder && !resources?.shared_folder.cloud_url) || openingSharedFolder,
              },
              refreshAction("shared_folder", "Shared Ordner"),
            ]}
          >
            {resources?.shared_folder.items.length ? (
              <SharedFolderTree
                items={resources.shared_folder.items}
                expandedItems={expandedSharedItems}
                onToggle={toggleSharedItem}
                onAttachResource={onAttachResource}
                showCloudFolderLinks={!resources.shared_folder.can_open_folder}
              />
            ) : (
              <p className="m-0 text-[13px] text-[#776d5f]">Shared Ordner ist leer oder noch nicht eingerichtet.</p>
            )}
          </ResourceCard>


          <ResourceCard
            id="vault"
            icon={<KeyRound size={16} />}
            title="Passwort-Tresor"
            summary={resources?.vault.summary ?? "Wird geprüft…"}
            status={vaultStatus}
            expanded={focusedResourcePanel === "vault" || expanded.vault}
            onToggle={toggle}
            focused={focusedResourcePanel === "vault"}
            hidden={focusedResourcePanel !== null && focusedResourcePanel !== "vault"}
            onClearFocus={clearFocusedResourcePanel}
            cardRef={setResourceCardRef("vault")}
            badge={vaultHintCount ? String(vaultHintCount) : undefined}
            action={[
              {
                icon: <ExternalLink size={14} />,
                label: "Tresor öffnen",
                onClick: openVault,
                disabled: !resources?.vault.vault_url,
              },
              refreshAction("vault", "Tresor"),
            ]}
          >
            {resources?.vault ? (
              <div className="grid gap-[7px] text-[12px] text-[#746957]">
                <div className="grid gap-[6px] rounded-[12px] border border-[#e4d8c6] bg-[#fffaf2] px-[9px] py-[8px]">
                  <div className="flex items-center justify-between gap-[8px]">
                    <span>Zugangsdaten</span>
                    <strong className="text-[#4c4235]">{resources.vault.exposed_count ?? resources.vault.item_count ?? "–"}</strong>
                  </div>
                  <div className="flex items-center justify-between gap-[8px]">
                    <span>{`Von ${assistantDativeName(assistantName)} erstellt`}</span>
                    <strong className="text-[#4c4235]">{resources.vault.agent_created_count ?? 0}</strong>
                  </div>
                  {resources.vault.weak_count != null && (
                    <div className="flex items-center justify-between gap-[8px]">
                      <span>Schwach</span>
                      <strong className={resources.vault.weak_count ? "text-[#8a5b2e]" : "text-[#4c4235]"}>{resources.vault.weak_count}</strong>
                    </div>
                  )}
                  {resources.vault.reused_count != null && (
                    <div className="flex items-center justify-between gap-[8px]">
                      <span>Mehrfach verwendet</span>
                      <strong className={resources.vault.reused_count ? "text-[#8a5b2e]" : "text-[#4c4235]"}>{resources.vault.reused_count}</strong>
                    </div>
                  )}
                  {resources.vault.compromised_supported && resources.vault.compromised_count != null && (
                    <div className="flex items-center justify-between gap-[8px]">
                      <span>Kompromittiert</span>
                      <strong className={resources.vault.compromised_count ? "text-[#8a3d2e]" : "text-[#4c4235]"}>{resources.vault.compromised_count}</strong>
                    </div>
                  )}
                </div>
                {resources.vault.status === "auth_required" && (
                  <p className="m-0 rounded-[10px] bg-[#f6ead8] px-[9px] py-[7px] text-[#7a6141]">Tresor öffnen und entsperren.</p>
                )}
              </div>
            ) : (
              <p className="m-0 text-[13px] text-[#776d5f]">Tresor wird geprüft.</p>
            )}
          </ResourceCard>

          <ResourceCard
            id="todos"
            icon={<ListChecks size={16} />}
            title="Aufgaben"
            summary={resources?.todos.summary ?? "Wird geprüft…"}
            status={todoStatus}
            expanded={focusedResourcePanel === "todos" || expanded.todos}
            onToggle={toggle}
            focused={focusedResourcePanel === "todos"}
            hidden={focusedResourcePanel !== null && focusedResourcePanel !== "todos"}
            onClearFocus={clearFocusedResourcePanel}
            cardRef={setResourceCardRef("todos")}
            badge={resources?.todos.open_count ? String(resources.todos.open_count) : undefined}
            action={[
              {
                icon: <Plus size={13} />,
                label: "Aufgabe hinzufügen",
                onClick: () => setTodoModalOpen(true),
                disabled: loading || addingTodo,
              },
              refreshAction("todos", "Aufgaben"),
            ]}
          >
            {resources?.todos.items.length ? (
              <div className="grid gap-[6px]">
                {resources.todos.items.map((item) => (
                  <div key={item.id} className="flex min-w-0 items-stretch rounded-[10px] border border-[#e4d8c6] bg-[#fffaf2] text-[13px] text-[#4c4235]">
                    <button
                      type="button"
                      className="grid w-[34px] shrink-0 place-items-center border-r border-[#e4d8c6] text-[#7b6b57] transition hover:bg-[#f4eadc] disabled:cursor-not-allowed disabled:opacity-50"
                      aria-label={`${item.text} als erledigt markieren`}
                      title="Als erledigt markieren"
                      disabled={updatingTodoId === item.id}
                      onClick={(event) => {
                        event.stopPropagation();
                        void updateTodoDone(item.id, true);
                      }}
                    >
                      <span className="grid h-[15px] w-[15px] place-items-center rounded-[4px] border border-[#b9aa93] bg-[#fffdf8] text-[11px] leading-none">
                        {updatingTodoId === item.id ? "…" : ""}
                      </span>
                    </button>
                    <span className="block min-w-0 flex-1 truncate px-[9px] py-[7px]">{item.text}</span>
                    <button
                      type="button"
                      className="grid w-[34px] shrink-0 place-items-center border-l border-[#e4d8c6] text-[#7b6b57] transition hover:bg-[#f4eadc] focus:outline-none focus:ring-2 focus:ring-[#b9a98f]"
                      aria-label={`${item.text} in den Chat übernehmen`}
                      title="Aufgabe in den Chat übernehmen"
                      onClick={(event) => {
                        event.stopPropagation();
                        onAttachTodo(item);
                      }}
                    >
                      <Paperclip size={13} />
                    </button>
                  </div>
                ))}
              </div>
            ) : (
              <p className="m-0 text-[13px] text-[#776d5f]">Keine offenen Aufgaben in TODO.md.</p>
            )}
          </ResourceCard>

          <ResourceCard
            id="connectors"
            icon={<PlugZap size={16} />}
            title="Konnektoren"
            summary={connectorSummary}
            status={resourceStatusCopy(connectorCount ? "connected" : "not_configured")}
            expanded={focusedResourcePanel === "connectors" || expanded.connectors}
            onToggle={toggle}
            focused={focusedResourcePanel === "connectors"}
            hidden={focusedResourcePanel !== null && focusedResourcePanel !== "connectors"}
            onClearFocus={clearFocusedResourcePanel}
            cardRef={setResourceCardRef("connectors")}
            action={{
              icon: <RefreshCw size={13} className={reloadingMcp ? "animate-spin" : undefined} />,
              label: "Alle MCP-Server neu laden",
              onClick: () => void reloadMcpConnectors(),
              disabled: reloadingMcp,
            }}
          >
            <div className="grid gap-[7px]">
              {resources?.connectors.length ? (
                <McpConnectorTree
                  items={resources.connectors}
                  expandedItems={expandedConnectorItems}
                  onToggle={toggleConnectorItem}
                />
              ) : (
                <p className="m-0 text-[13px] text-[#776d5f]">Keine MCP-Server verfügbar.</p>
              )}
            </div>
          </ResourceCard>

        </div>
        {resources?.checked_at && <p className="m-0 mt-[12px] text-[11px] text-[#9a8f7e]">Aktualisiert {formatResourceTime(resources.checked_at)}</p>}
      </div>
      <div className="mt-auto min-w-0 shrink-0 overflow-hidden rounded-[22px] border border-[#dacdbb] bg-[rgba(255,250,242,.94)] p-[15px] shadow-[0_14px_38px_rgba(56,42,20,.07)]">
        <div className="flex min-w-0 items-start gap-[10px]">
          <span className="grid h-[34px] w-[34px] shrink-0 place-items-center rounded-[12px] bg-[#efe4d4] text-[#6f5d45]">
            <LifeBuoy size={17} />
          </span>
          <div className="min-w-0 flex-1">
            <p className="m-0 truncate text-[11px] font-bold uppercase tracking-[.16em] text-[#948873]">Hilfe & Support</p>
            <h3 className="m-0 mt-[3px] truncate text-[15px] text-[#302b24]">{`Problem mit ${assistantDativeName(assistantName)} melden`}</h3>
            <p className="m-0 mt-[4px] text-[12px] leading-[1.35] text-[#776d5f]">Schreiben Sie kurz, was nicht funktioniert. AIWerk erhält eine sichere Diagnose.</p>
          </div>
        </div>
        <button
          type="button"
          onClick={() => setSupportModalOpen(true)}
          className="mt-[12px] flex w-full cursor-pointer items-center justify-center gap-[7px] rounded-[12px] border border-[#d5c6b0] bg-[#8b724e] px-[12px] py-[9px] text-[12px] font-bold text-white transition hover:bg-[#7a6342]"
        >
          <Send size={13} />
          Nachricht an AIWerk senden
        </button>
      </div>
      </aside>
      {supportModalOpen && (
        <div className="fixed inset-0 z-50 grid place-items-center bg-[rgba(38,34,28,.32)] px-[18px]" role="dialog" aria-modal="true" aria-labelledby="support-modal-title">
          <div className="w-full max-w-[520px] rounded-[24px] border border-[#d8cbb9] bg-[#fffaf2] p-[20px] shadow-[0_24px_70px_rgba(34,28,18,.25)]">
            <div className="flex items-start justify-between gap-[12px]">
              <div>
                <p className="m-0 text-[11px] font-bold uppercase tracking-[.18em] text-[#948873]">Hilfe & Support</p>
                <h2 id="support-modal-title" className="m-0 mt-[4px] text-[18px] text-[#302b24]">Nachricht an AIWerk senden</h2>
              </div>
              <button type="button" onClick={() => setSupportModalOpen(false)} className="grid h-[30px] w-[30px] shrink-0 place-items-center rounded-[9px] bg-[#2f2b24] text-[#fff8ed] hover:bg-[#4a4034]" aria-label="Schliessen">
                <X size={15} />
              </button>
            </div>
            <label className="mt-[16px] block text-[12px] font-bold text-[#5f5446]">Kategorie</label>
            <select
              value={supportCategory}
              onChange={(event) => setSupportCategory(event.target.value)}
              className="mt-[6px] w-full rounded-[12px] border border-[#d8cbb9] bg-[#fffaf2] px-[11px] py-[9px] text-[13px] text-[#3d362d] outline-none focus:border-[#b89d72]"
            >
              <option>Agent antwortet falsch</option>
              <option>Agent hängt</option>
              <option>E-Mail / Kalender / Dateien</option>
              <option>Login / Zugriff</option>
              <option>Sonstiges</option>
            </select>
            <label className="mt-[14px] block text-[12px] font-bold text-[#5f5446]">Nachricht</label>
            <textarea
              value={supportMessage}
              onChange={(event) => setSupportMessage(event.target.value)}
              rows={5}
              maxLength={4000}
              placeholder="Was funktioniert nicht?"
              className="mt-[6px] w-full resize-none rounded-[14px] border border-[#d8cbb9] bg-[#fffaf2] px-[12px] py-[10px] text-[13px] leading-[1.45] text-[#3d362d] outline-none focus:border-[#b89d72]"
            />
            <label className="mt-[10px] flex cursor-pointer items-start gap-[8px] text-[12px] leading-[1.35] text-[#6f6557]">
              <input
                type="checkbox"
                checked={supportDiagnostics}
                onChange={(event) => setSupportDiagnostics(event.target.checked)}
                className="mt-[2px]"
              />
              Sichere Diagnose mitsenden: Verbindungsstatus und Ressourcen-Zustand, keine Passwörter und kein Chat-Dump.
            </label>
            {supportError && <p className="mt-[10px] rounded-[12px] bg-[#f4e1da] px-[10px] py-[8px] text-[12px] text-[#7b3b2f]">{supportError}</p>}
            <div className="mt-[16px] flex justify-end gap-[8px]">
              <button type="button" onClick={() => setSupportModalOpen(false)} className="rounded-[12px] border border-[#dacdbb] bg-[#fffaf2] px-[13px] py-[9px] text-[12px] font-bold text-[#6b604f] hover:bg-[#f3eadc]">Abbrechen</button>
              <button
                type="button"
                onClick={() => void submitSupport()}
                disabled={!supportMessage.trim() || sendingSupport}
                className="rounded-[12px] border border-[#9a7b51] bg-[#8b724e] px-[14px] py-[9px] text-[12px] font-bold text-white hover:bg-[#7a6342] disabled:cursor-not-allowed disabled:opacity-50"
              >
                {sendingSupport ? "Wird gesendet…" : "Senden"}
              </button>
            </div>
          </div>
        </div>
      )}

      {contactModalOpen && (
        <div className="fixed inset-0 z-40 grid place-items-center bg-[#181611]/35 p-[20px]" role="dialog" aria-modal="true" aria-labelledby="new-contact-title">
          <form
            className="w-full max-w-[500px] rounded-[24px] border border-[#ded4c4] bg-[#fffaf2] p-[22px] text-[#292720] shadow-[0_28px_80px_rgba(35,29,18,.28)]"
            onSubmit={(event) => {
              event.preventDefault();
              void submitContact();
            }}
          >
            <div className="mb-[16px] flex items-start justify-between gap-[16px]">
              <div>
                <h2 id="new-contact-title" className="m-0 text-[21px] tracking-[-0.02em]">Kontakt hinzufügen</h2>
                <p className="mt-[7px] text-[14px] leading-[1.45] text-[#706655]">Lokalen Kontakt für die CUI speichern.</p>
              </div>
              <button
                type="button"
                onClick={() => {
                  if (savingContact) return;
                  setContactModalOpen(false);
                  setContactError(null);
                }}
                aria-label="Dialog schließen"
                className="grid h-[36px] w-[36px] shrink-0 cursor-pointer place-items-center rounded-[11px] border border-[#4b4235] bg-[#292720] text-[#f8f4ed] shadow-[0_10px_24px_rgba(41,39,32,.18)] transition hover:bg-[#3a342b] focus:outline-none focus:ring-2 focus:ring-[#8b724e]/45"
              >
                <X className="h-[16px] w-[16px]" />
              </button>
            </div>
            <div className="grid grid-cols-1 gap-[10px] sm:grid-cols-2">
              {[
                ["name", "Name", "z.B. Anna Meier"],
                ["organization", "Firma", "z.B. Beispiel AG"],
                ["role", "Rolle", "z.B. Geschäftsführerin"],
                ["email", "E-Mail", "anna@example.ch"],
                ["phone", "Telefon", "+41 ..."],
              ].map(([field, label, placeholder]) => (
                <label key={field} className="grid gap-[6px] text-[11px] font-bold uppercase tracking-[.12em] text-[#8a7a65]">
                  {label}
                  <input
                    value={String(contactForm[field as keyof typeof contactForm] ?? "")}
                    onChange={(event) => setContactForm((current) => ({ ...current, [field]: event.target.value }))}
                    placeholder={placeholder}
                    className="rounded-[12px] border border-[#d8cbb8] bg-[#fffdf8] px-[11px] py-[9px] text-[13px] font-normal normal-case tracking-normal text-[#292720] outline-none transition placeholder:text-[#a79b89] focus:border-[#9a7b51] focus:ring-2 focus:ring-[#9a7b51]/20"
                  />
                </label>
              ))}
            </div>
            <label className="mt-[10px] grid gap-[6px] text-[11px] font-bold uppercase tracking-[.12em] text-[#8a7a65]">
              Notiz
              <textarea
                value={contactForm.note}
                onChange={(event) => setContactForm((current) => ({ ...current, note: event.target.value }))}
                rows={3}
                maxLength={240}
                placeholder="Kurze interne Notiz"
                className="resize-none rounded-[12px] border border-[#d8cbb8] bg-[#fffdf8] px-[11px] py-[9px] text-[13px] font-normal normal-case tracking-normal text-[#292720] outline-none transition placeholder:text-[#a79b89] focus:border-[#9a7b51] focus:ring-2 focus:ring-[#9a7b51]/20"
              />
            </label>
            <label className="mt-[10px] flex cursor-pointer items-start gap-[8px] text-[12px] leading-[1.35] text-[#6f6557]">
              <input
                type="checkbox"
                checked={contactForm.link_current_context}
                onChange={(event) => setContactForm((current) => ({ ...current, link_current_context: event.target.checked }))}
                className="mt-[2px]"
              />
              Diesen Kontakt mit dem aktuellen Kontext verknüpfen.
            </label>
            {contactError && <p className="mt-[10px] rounded-[12px] bg-[#f4e1da] px-[10px] py-[8px] text-[12px] text-[#7b3b2f]">{contactError}</p>}
            <div className="mt-[18px] flex justify-end gap-[10px]">
              <button type="button" onClick={() => setContactModalOpen(false)} disabled={savingContact} className="cursor-pointer rounded-[12px] border border-[#d8cbb8] bg-[#fffdf8] px-[14px] py-[9px] text-[13px] font-bold text-[#6d5f4d] transition hover:bg-[#f4eadc] disabled:cursor-not-allowed disabled:opacity-50">Abbrechen</button>
              <button type="submit" disabled={savingContact} className="cursor-pointer rounded-[12px] border border-[#9a7b51] bg-[#8b724e] px-[14px] py-[9px] text-[13px] font-bold text-white transition hover:bg-[#7a6342] disabled:cursor-not-allowed disabled:opacity-50">
                {savingContact ? "Speichert…" : "Hinzufügen"}
              </button>
            </div>
          </form>
        </div>
      )}

      {todoModalOpen && (
        <div className="fixed inset-0 z-40 grid place-items-center bg-[#181611]/35 p-[20px]" role="dialog" aria-modal="true" aria-labelledby="new-todo-title">
          <form
            className="w-full max-w-[460px] rounded-[24px] border border-[#ded4c4] bg-[#fffaf2] p-[22px] text-[#292720] shadow-[0_28px_80px_rgba(35,29,18,.28)]"
            onSubmit={(event) => {
              event.preventDefault();
              void submitNewTodo();
            }}
          >
            <div className="mb-[16px] flex items-start justify-between gap-[16px]">
              <div>
                <h2 id="new-todo-title" className="m-0 text-[21px] tracking-[-0.02em]">Aufgabe hinzufügen</h2>
                <p className="mt-[7px] text-[14px] leading-[1.45] text-[#706655]">Neue Aufgabe in TODO.md speichern.</p>
              </div>
              <button
                type="button"
                onClick={() => {
                  if (addingTodo) return;
                  setTodoModalOpen(false);
                  setNewTodoText("");
                }}
                aria-label="Dialog schließen"
                className="grid h-[36px] w-[36px] shrink-0 cursor-pointer place-items-center rounded-[11px] border border-[#4b4235] bg-[#292720] text-[#f8f4ed] shadow-[0_10px_24px_rgba(41,39,32,.18)] transition hover:bg-[#3a342b] focus:outline-none focus:ring-2 focus:ring-[#8b724e]/45"
              >
                <X className="h-[16px] w-[16px]" />
              </button>
            </div>
            <label className="grid gap-[7px] text-[12px] font-bold uppercase tracking-[.12em] text-[#8a7a65]">
              Aufgabe
              <textarea
                value={newTodoText}
                onChange={(event) => setNewTodoText(event.target.value)}
                autoFocus
                rows={3}
                maxLength={240}
                placeholder="z.B. Offerte prüfen"
                className="min-h-[92px] resize-none rounded-[14px] border border-[#d8cbb8] bg-[#fffdf8] px-[12px] py-[10px] text-[14px] font-normal normal-case tracking-normal text-[#292720] outline-none transition placeholder:text-[#a79b89] focus:border-[#9a7b51] focus:ring-2 focus:ring-[#9a7b51]/20"
              />
            </label>
            <div className="mt-[18px] flex justify-end gap-[10px]">
              <button
                type="button"
                onClick={() => {
                  if (addingTodo) return;
                  setTodoModalOpen(false);
                  setNewTodoText("");
                }}
                disabled={addingTodo}
                className="cursor-pointer rounded-[12px] border border-[#d8cbb8] bg-[#fffdf8] px-[14px] py-[9px] text-[13px] font-bold text-[#6d5f4d] transition hover:bg-[#f4eadc] disabled:cursor-not-allowed disabled:opacity-50"
              >
                Abbrechen
              </button>
              <button
                type="submit"
                disabled={addingTodo || !newTodoText.trim()}
                className="cursor-pointer rounded-[12px] border border-[#9a7b51] bg-[#8b724e] px-[14px] py-[9px] text-[13px] font-bold text-white transition hover:bg-[#7a6342] disabled:cursor-not-allowed disabled:opacity-50"
              >
                {addingTodo ? "Speichert…" : "Hinzufügen"}
              </button>
            </div>
          </form>
        </div>
      )}
    </>
  );
}

function McpConnectorTree({
  items,
  expandedItems,
  onToggle,
  depth = 0,
}: {
  items: AssistantConnectorSummary[];
  expandedItems: Record<string, boolean>;
  onToggle: (id: string) => void;
  depth?: number;
}) {
  return (
    <div className="grid gap-[7px]">
      {items.map((connector) => {
        const status = resourceStatusCopy(connector.status);
        const children = connector.children ?? [];
        const hasChildren = children.length > 0;
        const isExpanded = !!expandedItems[connector.id];
        const canOpen = !!connector.open_url;
        const openConnector = () => {
          if (!connector.open_url) return;
          const opened = window.open(connector.open_url, "_blank", "noopener,noreferrer");
          if (opened) opened.opener = null;
        };
        return (
          <div key={connector.id} className={depth ? "grid min-w-0 gap-[7px] overflow-hidden" : "min-w-0 overflow-hidden"}>
            <button
              type="button"
              onClick={hasChildren ? () => onToggle(connector.id) : canOpen ? openConnector : undefined}
              disabled={!hasChildren && !canOpen}
              className="flex w-full min-w-0 max-w-full cursor-pointer items-center gap-[8px] overflow-hidden rounded-[12px] bg-[#f5eee3] px-[10px] py-[8px] text-left transition hover:bg-[#efe4d4] disabled:cursor-default disabled:hover:bg-[#f5eee3]"
              aria-expanded={hasChildren ? isExpanded : undefined}
              title={canOpen ? `${connector.label} auf aiwerkmcp.com öffnen` : undefined}
              style={{ paddingLeft: 10 + depth * 14 }}
            >
              <span className="grid h-[18px] w-[18px] shrink-0 place-items-center text-[#7b6b55]">
                {hasChildren ? <ChevronRight size={14} className={isExpanded ? "rotate-90 transition-transform" : "transition-transform"} /> : canOpen ? <ExternalLink size={13} /> : null}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[13px] font-semibold text-[#342f27]">{connector.label}</span>
                <span className="block truncate text-[12px] text-[#7c705f]">{connector.description || connector.capabilities?.join(" · ") || "MCP"}</span>
              </span>
              <span className="shrink-0 text-[11px] font-semibold text-[#766b5b]">{status.label}</span>
            </button>
            {hasChildren && isExpanded && (
              <McpConnectorTree
                items={children}
                expandedItems={expandedItems}
                onToggle={onToggle}
                depth={depth + 1}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}


function SharedFolderTree({
  items,
  expandedItems,
  onToggle,
  onAttachResource,
  showCloudFolderLinks,
  depth = 0,
}: {
  items: AssistantSharedFolderItem[];
  expandedItems: Record<string, boolean>;
  onToggle: (id: string) => void;
  onAttachResource: (kind: "email" | "calendar_event" | "shared_file" | "contact", item: Record<string, unknown>, label: string) => Promise<void>;
  showCloudFolderLinks: boolean;
  depth?: number;
}) {
  return (
    <div className="grid gap-[7px]">
      {items.map((item) => {
        const isFolder = item.kind === "folder";
        const children = item.children ?? [];
        const hasChildren = isFolder && children.length > 0;
        const isExpanded = !!expandedItems[item.id];
        const canOpenFile = !isFolder && !!item.open_url;
        const canOpenCloudFolder = showCloudFolderLinks && isFolder && !!item.cloud_url;
        const meta = isFolder
          ? `${children.length || item.child_count || 0} Elemente${item.modified_at ? ` · ${formatResourceTime(item.modified_at)}` : ""}`
          : `${formatFileSize(item.size_bytes ?? 0)}${item.modified_at ? ` · ${formatResourceTime(item.modified_at)}` : ""}`;
        const handleClick = hasChildren
          ? () => onToggle(item.id)
          : canOpenFile
            ? () => openSharedFolderFile(item)
            : undefined;
        const isInteractive = hasChildren || canOpenFile;
        const rowClass = `relative w-full min-w-0 max-w-full overflow-hidden rounded-[10px] border border-transparent bg-[#f1e8dc] px-[9px] py-[7px] text-left ${
          isInteractive ? "cursor-pointer transition hover:bg-[#eaddcc] focus:outline-none focus:ring-2 focus:ring-[#b9a98f]" : "cursor-default"
        }`;
        return (
          <div key={item.id} className={depth ? "grid min-w-0 gap-[7px] overflow-hidden" : "min-w-0 overflow-hidden"}>
            <div className="flex min-w-0 max-w-full items-stretch gap-[6px] overflow-hidden">
              <button
                type="button"
                onClick={handleClick}
                disabled={!isInteractive}
                className={rowClass}
                aria-expanded={hasChildren ? isExpanded : undefined}
                title={canOpenFile ? "Datei öffnen" : undefined}
                style={{ paddingLeft: 9 + depth * 14 }}
              >
                <div className="flex min-w-0 max-w-full items-center gap-[8px] overflow-hidden">
                  <span className="grid h-[18px] w-[18px] shrink-0 place-items-center text-[#7b6b55]">
                    {hasChildren ? <ChevronRight size={14} className={isExpanded ? "rotate-90 transition-transform" : "transition-transform"} /> : null}
                  </span>
                  <span className="grid h-[22px] w-[22px] shrink-0 place-items-center rounded-[8px] bg-[#ebe1d0] text-[#78684f]">
                    {isFolder ? <FolderOpen size={14} /> : <FileText size={14} />}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block min-w-0 max-w-full truncate text-[13px] font-semibold text-[#342f27]">{item.name}</span>
                    <span className="block min-w-0 max-w-full truncate text-[12px] text-[#7c705f]">{isFolder ? "Ordner" : "Datei"} · {meta}</span>
                  </span>
                </div>
              </button>
              {canOpenCloudFolder && (
                <button
                  type="button"
                  onClick={() => openSharedFolderCloudUrl(item.cloud_url)}
                  title="Ordner in cloud.aiwerk.ch öffnen"
                  aria-label={`${item.name} in cloud.aiwerk.ch öffnen`}
                  className={RIGHT_RAIL_ROW_ACTION_CLASS}
                >
                  <ExternalLink size={13} />
                </button>
              )}
              {canOpenFile && (
                <button
                  type="button"
                  onClick={() => void onAttachResource("shared_file", item as unknown as Record<string, unknown>, "Datei")}
                  title="Datei an Agent anhängen"
                  aria-label={`${item.name} an Agent anhängen`}
                  className={RIGHT_RAIL_ROW_ACTION_CLASS}
                >
                  <Paperclip size={13} />
                </button>
              )}
            </div>
            {hasChildren && isExpanded && (
              <SharedFolderTree
                items={children}
                expandedItems={expandedItems}
                onToggle={onToggle}
                onAttachResource={onAttachResource}
                showCloudFolderLinks={showCloudFolderLinks}
                depth={depth + 1}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}

function ResourceCard({
  id,
  icon,
  title,
  summary,
  status,
  expanded,
  onToggle,
  badge,
  action,
  disabled,
  focused,
  hidden,
  onClearFocus,
  cardRef,
  children,
}: {
  id: string;
  icon: ReactNode;
  title: string;
  summary: string;
  status: { label: string; dot: string };
  expanded: boolean;
  onToggle: (id: string) => void;
  focused?: boolean;
  hidden?: boolean;
  onClearFocus?: () => void;
  cardRef?: (node: HTMLDivElement | null) => void;
  badge?: string;
  action?: ResourceCardAction | ResourceCardAction[];
  disabled?: boolean;
  children?: ReactNode;
}) {
  const actions: ResourceCardAction[] = Array.isArray(action) ? action : action ? [action] : [];
  const resourceHeaderTone = {
    surface: "bg-[#f5eadb]",
    icon: "bg-[#ead7bf] text-[#705334]",
    action: "border-[#d9c5aa] bg-[#f8efe2] text-[#705334] hover:bg-[#eedfcb]",
    badge: "bg-[#8a6842] text-white",
  };
  const cardStateClass = hidden
    ? "max-h-0 -translate-y-2 scale-[.985] overflow-hidden opacity-0 pointer-events-none"
    : focused
      ? "max-h-[1200px] translate-y-0 scale-100 opacity-100"
      : "max-h-[760px] translate-y-0 scale-100 opacity-100";
  return (
    <div ref={cardRef} className={`min-w-0 rounded-[18px] border border-[#dfd4c4] bg-[#fffaf2] shadow-[0_8px_24px_rgba(56,42,20,.05)] transition-all duration-300 ease-out ${cardStateClass}`}>
      <div className={`flex min-w-0 items-stretch gap-[6px] rounded-t-[18px] border-b border-[#e6daca] p-[12px] ${resourceHeaderTone.surface}`}>
        <button
          type="button"
          onClick={() => !disabled && onToggle(id)}
          className="flex min-w-0 flex-1 cursor-pointer items-center gap-[10px] overflow-hidden border-0 bg-transparent p-0 text-left disabled:cursor-default"
          disabled={disabled}
          aria-expanded={expanded}
        >
          <span className={`grid h-[32px] w-[32px] shrink-0 place-items-center rounded-[12px] ${resourceHeaderTone.icon}`}>{icon}</span>
          <span className="min-w-0 flex-1 overflow-hidden">
            <span className="flex min-w-0 items-center gap-[7px] text-[13px] font-bold text-[#302b24]">
              <span className="h-[7px] w-[7px] shrink-0 rounded-full" style={{ backgroundColor: status.dot }} />
              <span className="min-w-0 truncate">{title}</span>
            </span>
            <span className="mt-[2px] block min-w-0 max-w-full truncate text-[12px] text-[#706655]">{summary}</span>
          </span>
          {badge && <span className={`shrink-0 rounded-full px-[8px] py-[3px] text-[11px] font-bold ${resourceHeaderTone.badge}`}>{badge}</span>}
        </button>
        {focused && onClearFocus && (
          <button
            type="button"
            onClick={onClearFocus}
            title="Zurück zu allen Ressourcen"
            aria-label="Zurück zu allen Ressourcen"
            className={`grid h-[32px] shrink-0 cursor-pointer place-items-center rounded-[11px] border px-[10px] text-[11px] font-bold transition disabled:cursor-not-allowed disabled:opacity-45 ${resourceHeaderTone.action}`}
          >
            Alle
          </button>
        )}
        {actions.map((item) => (
          <button
            key={item.label}
            type="button"
            onClick={item.onClick}
            disabled={item.disabled}
            title={item.label}
            aria-label={item.label}
            className={`grid h-[32px] w-[32px] shrink-0 cursor-pointer place-items-center rounded-[11px] border transition disabled:cursor-not-allowed disabled:opacity-45 ${resourceHeaderTone.action}`}
          >
            {item.icon}
          </button>
        ))}
      </div>
      {expanded && children && <div className="px-[12px] pb-[12px] pt-[10px]">{children}</div>}
    </div>
  );
}

function RuntimeStatusModal({
  active,
  status,
  approvals,
  onClose,
  onSetConfig,
  onResolveApproval,
}: {
  active: RuntimeBadgeId;
  status: RuntimeStatus;
  approvals: ApprovalCard[];
  onClose: () => void;
  onSetConfig: (key: "busy" | "reasoning" | "fast" | "yolo", value: string, label: string) => void;
  onResolveApproval: (id: string, approve: boolean) => void;
}) {
  const titleByActive: Record<RuntimeBadgeId, string> = {
    busy: "Eingabe während Arbeit",
    reasoning: "Denkmodus",
    fast: "Tempo",
    approvals: "Freigaben",
  };
  const hasBlockingApprovals = active === "approvals" && approvals.length > 0;
  const closeLabel = hasBlockingApprovals ? "Ablehnen und Dialog schließen" : "Dialog schließen";

  return (
    <div className="fixed inset-0 z-40 grid place-items-center bg-[#181611]/35 p-[20px]" role="dialog" aria-modal="true" aria-labelledby="runtime-status-title">
      <div className="w-full max-w-[520px] rounded-[24px] border border-[#ded4c4] bg-[#fffaf2] p-[22px] text-[#292720] shadow-[0_28px_80px_rgba(35,29,18,.28)]">
        <div className="mb-[16px] flex items-start justify-between gap-[16px]">
          <div>
            <h2 id="runtime-status-title" className="m-0 text-[21px] tracking-[-0.02em]">{titleByActive[active]}</h2>
            <p className="mt-[7px] text-[14px] leading-[1.45] text-[#706655]">{runtimeStatusHelp(active)}</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label={closeLabel}
            className="group relative grid h-[36px] w-[36px] shrink-0 cursor-pointer place-items-center rounded-[11px] border border-[#4b4235] bg-[#292720] text-[#f8f4ed] shadow-[0_10px_24px_rgba(41,39,32,.18)] transition hover:bg-[#3a342b] focus:outline-none focus:ring-2 focus:ring-[#8b724e]/45"
          >
            <X className="h-[16px] w-[16px]" />
            {hasBlockingApprovals && (
              <span className="pointer-events-none absolute right-0 top-[42px] z-10 w-[220px] rounded-[12px] bg-[#292720] px-[11px] py-[9px] text-left text-[12px] leading-[1.35] text-white opacity-0 shadow-[0_14px_36px_rgba(0,0,0,.2)] transition group-hover:opacity-100 group-focus:opacity-100">
                Schließen wird als Ablehnen gewertet.
              </span>
            )}
          </button>
        </div>

        {active === "busy" && (
          <div className="grid gap-[10px]">
            <RuntimeChoice label="Warteschlange" active={status.busyMode === "queue"} detail="Neue Eingaben werden sauber hinten angestellt." onClick={() => onSetConfig("busy", "queue", "Eingabe: Warteschlange")} />
            <RuntimeChoice label="Lenken" active={(status.busyMode ?? "steer") === "steer"} detail="Neue Eingaben steuern die laufende Antwort, ohne sie direkt zu stoppen." onClick={() => onSetConfig("busy", "steer", "Eingabe: Lenken")} />
            <RuntimeChoice label="Unterbrechen" active={status.busyMode === "interrupt"} detail="Neue Eingaben unterbrechen die laufende Antwort." onClick={() => onSetConfig("busy", "interrupt", "Eingabe: Unterbrechen")} />
          </div>
        )}

        {active === "reasoning" && (
          <div className="grid gap-[16px]">
            <div>
              <div className="mb-[8px] text-[12px] font-bold uppercase tracking-[.12em] text-[#8a7a65]">Denkaufwand</div>
              <div className="grid grid-cols-2 gap-[8px] sm:grid-cols-3">
                {["none", "minimal", "low", "medium", "high", "xhigh"].map((effort) => (
                  <RuntimeChoice key={effort} label={reasoningEffortLabel(effort)} active={(status.reasoningEffort ?? "medium") === effort} compact onClick={() => onSetConfig("reasoning", effort, `Denken: ${reasoningEffortLabel(effort)}`)} />
                ))}
              </div>
            </div>
            <div>
              <div className="mb-[8px] text-[12px] font-bold uppercase tracking-[.12em] text-[#8a7a65]">Anzeige</div>
              <div className="grid grid-cols-2 gap-[8px]">
                <RuntimeChoice label="Verdeckt" active={(status.reasoningDisplay ?? "hide") !== "show"} detail="Zwischenüberlegungen bleiben ausgeblendet." onClick={() => onSetConfig("reasoning", "hide", "Denken verdeckt")} />
                <RuntimeChoice label="Sichtbar" active={status.reasoningDisplay === "show"} detail="Zwischenüberlegungen werden, soweit verfügbar, angezeigt." onClick={() => onSetConfig("reasoning", "show", "Denken sichtbar")} />
              </div>
            </div>
          </div>
        )}

        {active === "fast" && (
          <div className="grid gap-[10px]">
            <RuntimeChoice label="Normal" active={(status.fastMode ?? "normal") !== "fast"} detail="Ausgewogene Antwortqualität und Kosten." onClick={() => onSetConfig("fast", "normal", "Tempo: Normal")} />
            <RuntimeChoice label="Schnell" active={status.fastMode === "fast"} detail="Schnellere Verarbeitung, falls das aktuelle Modell Fast Mode unterstützt." onClick={() => onSetConfig("fast", "fast", "Tempo: Schnell")} />
          </div>
        )}

        {active === "approvals" && (
          <div className="grid gap-[10px]">
            {approvals.length === 0 ? (
              <div className="grid gap-[10px]">
                <RuntimeChoice label="Mit Rückfrage" active={status.yoloMode !== "on" && status.yoloMode !== "1"} detail="Riskante Aktionen werden vor der Ausführung gestoppt und hier zur Entscheidung angezeigt." onClick={() => onSetConfig("yolo", "off", "Freigabe: mit Rückfrage")} />
                <RuntimeChoice label="Direkt ausführen" active={status.yoloMode === "on" || status.yoloMode === "1"} detail="Riskante Aktionen laufen ohne Rückfrage. Für Kundennutzung nur bewusst einschalten." onClick={() => onSetConfig("yolo", "on", "Freigabe: direkt")} />
              </div>
            ) : (
              approvals.map((approval) => (
                <div key={approval.id} className="overflow-hidden rounded-[18px] border border-[#d2c0a8] bg-[#fffdf8] text-[14px] leading-[1.45] shadow-[0_18px_46px_rgba(48,38,22,.12)]">
                  <div className="h-[4px] bg-gradient-to-r from-[#292720] via-[#8a7658] to-[#d8c7ab]" />
                  <div className="p-[16px]">
                    <strong className="text-[15px] text-[#292720]">Aktion wartet auf Entscheidung</strong>
                    <p className="mb-[14px] mt-[7px] text-[#625a4c]">{approval.detail}</p>
                    <div className="grid grid-cols-2 gap-[8px]">
                      <button type="button" onClick={() => onResolveApproval(approval.id, true)} className="cursor-pointer rounded-[11px] border border-[#9a7b51] bg-[#8b724e] px-[12px] py-[10px] font-semibold text-white shadow-[0_10px_24px_rgba(91,70,39,.18)] hover:bg-[#7a6342]">Freigeben</button>
                      <button type="button" onClick={() => onResolveApproval(approval.id, false)} className="cursor-pointer rounded-[11px] border border-[#d7b7ad] bg-[#fffaf2] px-[12px] py-[10px] font-semibold text-[#7b3b2f] hover:bg-[#f4e2dc]">Ablehnen</button>
                    </div>
                  </div>
                </div>
              ))
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function runtimeStatusHelp(active: RuntimeBadgeId): string {
  if (active === "busy") return "Diese Einstellung entspricht /busy status und bestimmt das Verhalten bei Eingaben während einer laufenden Antwort.";
  if (active === "reasoning") return "Diese Einstellung entspricht /reasoning und beeinflusst Denkaufwand sowie Sichtbarkeit von Zwischenüberlegungen.";
  if (active === "fast") return "Diese Einstellung entspricht /fast und schaltet die Priorität der Antwortverarbeitung.";
  return "Legt fest, ob riskante Aktionen vorher abgefragt werden oder direkt ausgeführt werden.";
}

function RuntimeChoice({
  label,
  detail,
  active,
  compact,
  onClick,
}: {
  label: string;
  detail?: string;
  active?: boolean;
  compact?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={(
        "cursor-pointer rounded-[14px] border px-[13px] py-[11px] text-left transition hover:bg-[#f0e5d6] " +
        (active ? "border-[#9a7b51] bg-[#e9ddcb] text-[#292720]" : "border-[#dfd3c2] bg-[#fbf5eb] text-[#3b352c]") +
        (compact ? " text-center" : "")
      )}
    >
      <strong className="block text-[14px]">{label}</strong>
      {detail && <span className="mt-[4px] block text-[12px] font-medium leading-[1.35] text-[#706655]">{detail}</span>}
    </button>
  );
}

function AttachmentPreviewGrid({
  attachments,
  compact,
  tone = "light",
  onRemove,
}: {
  attachments: AttachmentPreview[];
  compact?: boolean;
  tone?: "light" | "dark";
  onRemove?: (id: string) => void;
}) {
  return (
    <div className={compact ? "grid gap-[8px]" : "grid grid-cols-1 gap-[8px] sm:grid-cols-2"}>
      {attachments.map((attachment) => (
        <AttachmentPreviewCard
          key={attachment.id}
          attachment={attachment}
          compact={compact}
          tone={tone}
          onRemove={onRemove ? () => onRemove(attachment.id) : undefined}
        />
      ))}
    </div>
  );
}

function AttachmentPreviewCard({
  attachment,
  compact,
  tone = "light",
  onRemove,
}: {
  attachment: AttachmentPreview;
  compact?: boolean;
  tone?: "light" | "dark";
  onRemove?: () => void;
}) {
  const isImage = Boolean(attachment.previewUrl);
  const baseClass =
    tone === "dark"
      ? "border-white/20 bg-white/10 text-white"
      : "border-[#dfd3c2] bg-[#fffaf2] text-[#3b352c]";
  const metaClass = tone === "dark" ? "text-white/75" : "text-[#746855]";
  const thumbClass = tone === "dark" ? "border-white/15 bg-white/10" : "border-[#e0d4c4] bg-[#efe6d6]";

  return (
    <div
      className={`relative flex max-w-full items-center gap-[12px] rounded-[14px] border p-[10px] ${baseClass} ${compact ? "mb-[2px]" : ""}`}
    >
      <div className={`grid h-[58px] w-[58px] shrink-0 place-items-center overflow-hidden rounded-[12px] border ${thumbClass}`}>
        {isImage ? (
          <img src={attachment.previewUrl} alt={attachment.name} className="h-full w-full object-cover" />
        ) : (
          <FileText className="h-[26px] w-[26px] opacity-80" />
        )}
      </div>
      <div className="min-w-0 flex-1 pr-[28px]">
        <div className="truncate text-[14px] font-bold">{attachment.name}</div>
        <div className={`mt-[3px] flex items-center gap-[6px] text-[12px] ${metaClass}`}>
          {isImage ? <ImageIcon className="h-[13px] w-[13px]" /> : <FileText className="h-[13px] w-[13px]" />}
          <span>{isImage ? "Bildvorschau" : "Datei"}</span>
          <span>·</span>
          <span>{formatFileSize(attachment.size)}</span>
        </div>
      </div>
      {onRemove && (
        <button
          type="button"
          onClick={onRemove}
          aria-label="Anhang entfernen"
          title="Anhang entfernen"
          className="absolute right-[8px] top-[8px] grid h-[24px] w-[24px] cursor-pointer place-items-center rounded-full border border-[#d8cbbb] bg-[#fbf5eb] text-[#5c5142] hover:bg-[#f0e5d6]"
        >
          <X className="h-[13px] w-[13px]" />
        </button>
      )}
    </div>
  );
}

function ThinkingIndicator() {
  return (
    <div className="max-w-[74%] px-[10px] py-[6px] text-[#625a4c]">
      <div className="flex items-center" role="status" aria-live="polite" aria-label="Der Assistent arbeitet an der Antwort">
        <span className="flex items-end gap-[4px]" aria-hidden="true">
          <span className="aiwerk-thinking-dot h-[6px] w-[6px] rounded-full bg-[#8b724e]" />
          <span className="aiwerk-thinking-dot h-[6px] w-[6px] rounded-full bg-[#8b724e]" />
          <span className="aiwerk-thinking-dot h-[6px] w-[6px] rounded-full bg-[#8b724e]" />
        </span>
      </div>
    </div>
  );
}
