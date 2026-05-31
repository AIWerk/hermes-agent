import { CalendarDays, ChevronRight, ExternalLink, FileText, FolderOpen, Image as ImageIcon, Mail, Mic, Paperclip, Pencil, PlugZap, RefreshCw, Square, Volume2, VolumeX, X } from "lucide-react";
import { Fragment, type CSSProperties, type PointerEvent as ReactPointerEvent, type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Markdown } from "@/components/Markdown";
import { getHermesUserDisplayName } from "@/lib/dashboard-flags";
import { GatewayClient, type GatewayEvent } from "@/lib/gatewayClient";
import { HERMES_BASE_PATH, api, type AssistantConnectorSummary, type AssistantResourcesResponse, type AssistantResourceStatus, type AssistantSharedFolderItem, type AssistantUploadedAttachment, type ModelInfoResponse } from "@/lib/api";

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

function formatResourceTime(value?: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString("de-CH", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function openSharedFolderCloudUrl(url?: string | null): void {
  if (!url) return;
  const opened = window.open(url, "_blank", "noopener,noreferrer");
  if (opened) opened.opener = null;
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

function chatHeaderCopy(
  busy: boolean,
  approvalCount: number,
  connection: ConnectionState,
  statusLabel: string,
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
    subtitle: "Schreiben Sie, was Rocky erledigen soll.",
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
  const last = next[next.length - 1];
  if (last?.role === "agent" && last.status === "streaming") {
    next[next.length - 1] = { ...last, text: text || last.text, status };
    return next;
  }
  next.push({ id: newId("agent"), role: "agent", text, status });
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
  const activeTurnModeRef = useRef<ConversationMode>("main");
  const activeToolAnchorRef = useRef<string | undefined>(undefined);
  const activeSideToolAnchorRef = useRef<string | undefined>(undefined);
  const conversationModeRef = useRef<ConversationMode>("main");

  const showToast = useCallback((text: string) => {
    setToast(text);
    window.setTimeout(() => setToast(null), 1800);
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

  const liveNotesText = useMemo(() => formatLiveNotes(liveNotes), [liveNotes]);
  const headerBadges = useMemo(() => statusBadges(runtimeStatus, approvals.length), [runtimeStatus, approvals.length]);
  const chatHeader = useMemo(
    () => chatHeaderCopy(busy, approvals.length, connection, statusLabel),
    [busy, approvals.length, connection, statusLabel],
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

  const refreshResources = useCallback(async () => {
    setResourcesLoading(true);
    try {
      const resources = await api.getAssistantResources();
      setResourceSummary(resources);
      setResourcesError(null);
    } catch (e) {
      setResourcesError(e instanceof Error ? e.message : "Ressourcen konnten nicht geladen werden.");
    } finally {
      setResourcesLoading(false);
    }
  }, []);

  const reloadMcpServers = useCallback(async () => {
    const gateway = gatewayRef.current;
    if (!gateway || !sessionId) {
      showToast("MCP-Reload nicht verbunden");
      return;
    }
    showToast("MCP-Server werden neu geladen…");
    try {
      await gateway.request("slash.exec", { session_id: sessionId, command: "reload-mcp" }, 120_000);
      window.setTimeout(() => void refreshResources(), 400);
      window.setTimeout(() => void refreshResources(), 1800);
      showToast("MCP-Server neu geladen");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      showToast("MCP-Reload fehlgeschlagen");
      throw e;
    }
  }, [refreshResources, sessionId, showToast]);

  useEffect(() => {
    const initial = window.setTimeout(() => void refreshResources(), 0);
    const timer = window.setInterval(() => void refreshResources(), 120_000);
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
    if ((!text && attachments.length === 0) || !gateway || !sessionId || busy) return;
    const attachmentNames = attachments.map((file) => file.name).join(", ");
    const submitText = text || `Anhänge: ${attachmentNames || "Dateien"}`;
    const gatewayText = text && attachmentNames ? `${text}\n\nAnhänge: ${attachmentNames}` : submitText;
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
        @keyframes aiwerk-thinking-pulse {
          0%, 80%, 100% { opacity: .32; transform: translateY(0); }
          40% { opacity: 1; transform: translateY(-3px); }
        }
        .aiwerk-thinking-dot { animation: aiwerk-thinking-pulse 1.2s ease-in-out infinite; }
        .aiwerk-thinking-dot:nth-child(2) { animation-delay: .15s; }
        .aiwerk-thinking-dot:nth-child(3) { animation-delay: .3s; }
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
      <div className="aiwerk-assistant grid h-dvh min-h-0 grid-cols-1 overflow-hidden lg:grid-cols-[380px_1fr]">
        {/* Sidebar */}
        <aside className="hidden h-dvh min-h-0 flex-col gap-[20px] overflow-hidden bg-[#292720] p-[24px] text-[#f8f4ed] lg:flex">
          <div className="flex items-center gap-[12px]">
            <div className="grid h-[48px] w-[48px] place-items-center rounded-[15px] bg-[#d7b98e] text-[20px] font-extrabold text-[#292720]">
              R
            </div>
            <div>
              <strong className="text-[20px]">Rocky</strong>
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
              "grid min-h-0 grid-cols-1 gap-[12px] xl:grid-cols-[minmax(0,1fr)_10px_var(--right-rail-width)] " +
              (isResizingRightRail ? "cursor-col-resize select-none" : "")
            }
            style={{ "--right-rail-width": `${rightRailWidth}px` } as CSSProperties}
          >
            {/* Chat panel */}
            <div
              className="aiwerk-chat-panel relative grid h-full min-h-0 grid-rows-[auto_minmax(0,1fr)_auto] overflow-hidden rounded-[24px] border border-[#ded4c4] bg-[rgba(255,250,242,.86)] shadow-[0_18px_50px_rgba(56,42,20,.08)]"
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

              <div>
                <div className="flex gap-[10px] border-t border-[#e3d9c9] bg-[#fbf5eb] p-[16px]">
                  <button
                    type="button"
                    onClick={() => void startVoiceInput()}
                    disabled={voiceState === "transcribing" || !sessionId || busy || connection !== "open"}
                    title={voiceState === "recording" ? "Aufnahme stoppen" : "Spracheingabe"}
                    aria-label={voiceState === "recording" ? "Aufnahme stoppen" : "Spracheingabe"}
                    className={
                      "grid w-[46px] cursor-pointer place-items-center rounded-[14px] border transition hover:-translate-y-[1px] disabled:cursor-not-allowed disabled:opacity-50 " +
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
                    className="grid w-[46px] cursor-pointer place-items-center rounded-[14px] border border-[#d9d0c1] bg-[#fffaf2] text-[#4b4235] hover:bg-[#f2eadf]"
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
                  <input
                    type="text"
                    value={input}
                    onChange={(e) => setInput(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
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
                    className="flex-1 rounded-[14px] border border-[#d9d0c1] bg-white px-[14px] py-[13px] text-[15px] outline-none"
                  />
                  <button
                    type="button"
                    onClick={() => void submit("main")}
                    disabled={(!input.trim() && attachedFiles.length === 0) || !sessionId || busy || connection !== "open"}
                    className="cursor-pointer rounded-[12px] border border-[#9a7b51] bg-[#8b724e] px-[14px] py-[10px] font-semibold text-white hover:bg-[#7a6342] disabled:cursor-not-allowed disabled:opacity-50"
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
                  <div className="flex gap-[10px]">
                    <input
                      type="text"
                      value={sideInput}
                      onChange={(e) => setSideInput(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") {
                          e.preventDefault();
                          void submit("side");
                        }
                      }}
                      placeholder="Nachricht in der Nebenunterhaltung…"
                      className="min-w-0 flex-1 rounded-[14px] border border-[#d9d0c1] bg-white px-[13px] py-[12px] text-[14px] outline-none"
                    />
                    <button
                      type="button"
                      onClick={() => void submit("side")}
                      disabled={!sideInput.trim() || !sessionId || busy || connection !== "open"}
                      className="cursor-pointer rounded-[12px] border border-[#9a7b51] bg-[#8b724e] px-[13px] py-[9px] font-semibold text-white hover:bg-[#7a6342] disabled:cursor-not-allowed disabled:opacity-50"
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
              className="group hidden h-full cursor-col-resize items-center justify-center rounded-full focus:outline-none focus:ring-2 focus:ring-[#b89d72]/40 xl:flex"
            >
              <span className="h-[72px] w-[3px] rounded-full bg-[#d8cdbd] transition group-hover:bg-[#b89d72] group-focus-visible:bg-[#b89d72]" />
            </button>

            {/* Right side panels */}
            <ResourcesRail
              resources={resourceSummary}
              loading={resourcesLoading}
              error={resourcesError}
              attachments={attachedFiles}
              approvals={approvals}
              onRefresh={() => void refreshResources()}
              onReloadMcp={reloadMcpServers}
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


function ResourcesRail({
  resources,
  loading,
  error,
  attachments,
  approvals,
  onRefresh,
  onReloadMcp,
}: {
  resources: AssistantResourcesResponse | null;
  loading: boolean;
  error: string | null;
  attachments: AttachmentPreview[];
  approvals: ApprovalCard[];
  onRefresh: () => void;
  onReloadMcp: () => Promise<void>;
}) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({
    email: true,
    calendar: false,
    shared: false,
    connectors: false,
  });
  const [expandedSharedItems, setExpandedSharedItems] = useState<Record<string, boolean>>({});
  const [expandedConnectorItems, setExpandedConnectorItems] = useState<Record<string, boolean>>({});
  const [openingSharedFolder, setOpeningSharedFolder] = useState(false);
  const [reloadingMcp, setReloadingMcp] = useState(false);
  const toggle = (id: string) => setExpanded((current) => ({ ...current, [id]: !current[id] }));
  const toggleSharedItem = (id: string) => setExpandedSharedItems((current) => ({ ...current, [id]: !current[id] }));
  const toggleConnectorItem = (id: string) => setExpandedConnectorItems((current) => ({ ...current, [id]: !current[id] }));
  const reloadMcpConnectors = async () => {
    if (reloadingMcp) return;
    setReloadingMcp(true);
    try {
      await onReloadMcp();
    } finally {
      setReloadingMcp(false);
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
  const connectorCount = resources?.connectors.length ?? 0;
  const connectorSummary = resources
    ? connectorCount
      ? `${connectorCount} MCP-Server verfügbar`
      : "Keine MCP-Server verfügbar"
    : "Wird geprüft…";
  return (
    <aside className="hidden max-h-[calc(100vh-56px)] w-full content-start gap-[14px] overflow-y-auto pr-[2px] xl:grid aiwerk-scrollbar" aria-label="Ressourcen">
      <div className="rounded-[24px] border border-[#ded4c4] bg-[rgba(255,250,242,.9)] p-[18px] shadow-[0_18px_50px_rgba(56,42,20,.08)]">
        <div className="mb-[14px] flex items-start justify-between gap-[12px]">
          <div>
            <p className="m-0 text-[11px] font-bold uppercase tracking-[.18em] text-[#948873]">Ressourcen</p>
            <h3 className="m-0 mt-[4px] text-[17px] text-[#302b24]">Was Rocky nutzen kann</h3>
          </div>
          <button
            type="button"
            onClick={onRefresh}
            className="grid h-[34px] w-[34px] shrink-0 cursor-pointer place-items-center rounded-full border border-[#d9cdbc] bg-[#fffaf2] text-[#6f614e] hover:bg-[#f3eadc] disabled:cursor-wait disabled:opacity-60"
            disabled={loading}
            title="Ressourcen aktualisieren"
            aria-label="Ressourcen aktualisieren"
          >
            <RefreshCw size={15} className={loading ? "animate-spin" : undefined} />
          </button>
        </div>
        {error && <p className="mb-[12px] rounded-[12px] bg-[#f4e1da] px-[10px] py-[8px] text-[12px] text-[#7b3b2f]">Ressourcen konnten nicht geladen werden.</p>}
        <div className="grid gap-[10px]">
          <ResourceCard
            id="email"
            icon={<Mail size={16} />}
            title="E-Mail"
            summary={resources?.email.summary ?? "Wird geprüft…"}
            status={emailStatus}
            expanded={expanded.email}
            onToggle={toggle}
            badge={resources?.email.unread_count ? String(resources.email.unread_count) : undefined}
          >
            {resources?.email.items.length ? (
              <div className="grid gap-[8px]">
                {!resources.email.unread_count && <p className="m-0 text-[12px] text-[#8a7f70]">Zuletzt im Posteingang</p>}
                {resources.email.items.slice(0, 5).map((item, index) => (
                  <div key={item.id ?? `${item.subject}-${index}`} className="rounded-[12px] bg-[#f5eee3] px-[10px] py-[8px]">
                    <div className="flex items-center gap-[6px]">
                      {item.unread && <span className="h-[7px] w-[7px] shrink-0 rounded-full bg-[#8b724e]" aria-label="Neu" />}
                      <p className="m-0 min-w-0 flex-1 truncate text-[13px] font-semibold text-[#342f27]">{item.subject || "Ohne Betreff"}</p>
                      {item.has_attachment && <Paperclip size={12} className="shrink-0 text-[#8a7a64]" aria-label="Mit Anhang" />}
                    </div>
                    <p className="m-0 mt-[2px] truncate text-[12px] text-[#7c705f]">{item.sender || "Unbekannt"}{item.received_at ? ` · ${formatResourceTime(item.received_at)}` : ""}</p>
                  </div>
                ))}
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
            expanded={expanded.calendar}
            onToggle={toggle}
          >
            {resources?.calendar.items.length ? (
              <div className="grid gap-[8px]">
                {resources.calendar.items.slice(0, 5).map((item, index) => (
                  <div key={item.id ?? `${item.title}-${index}`} className="rounded-[12px] bg-[#f5eee3] px-[10px] py-[8px]">
                    <p className="m-0 truncate text-[13px] font-semibold text-[#342f27]">{item.title || "Termin"}</p>
                    <p className="m-0 mt-[2px] truncate text-[12px] text-[#7c705f]">{formatResourceTime(item.starts_at)}{item.location_hint ? ` · ${item.location_hint}` : ""}</p>
                  </div>
                ))}
              </div>
            ) : (
              <p className="m-0 text-[13px] text-[#776d5f]">Keine kommenden Termine oder Kalender noch nicht angebunden.</p>
            )}
          </ResourceCard>

          <ResourceCard
            id="shared"
            icon={<FolderOpen size={16} />}
            title="Shared Ordner"
            summary={resources?.shared_folder.summary ?? "Wird geprüft…"}
            status={sharedStatus}
            expanded={expanded.shared}
            onToggle={toggle}
            action={{
              icon: resources?.shared_folder.can_open_folder ? <FolderOpen size={14} /> : <ExternalLink size={14} />,
              label: resources?.shared_folder.can_open_folder
                ? "Ordner im Dateimanager öffnen"
                : resources?.shared_folder.cloud_url
                  ? "Ordner in cloud.aiwerk.ch öffnen"
                  : "Cloud-Ordner nicht verfügbar",
              onClick: openSharedFolder,
              disabled: (!resources?.shared_folder.can_open_folder && !resources?.shared_folder.cloud_url) || openingSharedFolder,
            }}
          >
            {resources?.shared_folder.items.length ? (
              <SharedFolderTree
                items={resources.shared_folder.items}
                expandedItems={expandedSharedItems}
                onToggle={toggleSharedItem}
                showCloudFolderLinks={!resources.shared_folder.can_open_folder}
              />
            ) : (
              <p className="m-0 text-[13px] text-[#776d5f]">Shared Ordner ist leer oder noch nicht eingerichtet.</p>
            )}
          </ResourceCard>

          <ResourceCard
            id="connectors"
            icon={<PlugZap size={16} />}
            title="Konnektoren"
            summary={connectorSummary}
            status={resourceStatusCopy(connectorCount ? "connected" : "not_configured")}
            expanded={expanded.connectors}
            onToggle={toggle}
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

          <ResourceCard
            id="material"
            icon={<FileText size={16} />}
            title="Aktuelles Material"
            summary={attachments.length ? `${attachments.length} Dateien bereit` : "Keine Dateien in dieser Unterhaltung"}
            status={resourceStatusCopy(attachments.length ? "connected" : "not_configured")}
            expanded={false}
            onToggle={() => undefined}
            disabled
          />

          <ResourceCard
            id="actions"
            icon={<Square size={15} />}
            title="Nächste Aktionen"
            summary={approvals.length ? `${approvals.length} Freigabe offen` : "Keine Aktion wartet"}
            status={resourceStatusCopy(approvals.length ? "limited" : "connected")}
            expanded={false}
            onToggle={() => undefined}
            disabled
          />
        </div>
        {resources?.checked_at && <p className="m-0 mt-[12px] text-[11px] text-[#9a8f7e]">Aktualisiert {formatResourceTime(resources.checked_at)}</p>}
      </div>
    </aside>
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
        return (
          <div key={connector.id} className={depth ? "grid gap-[7px]" : undefined}>
            <button
              type="button"
              onClick={hasChildren ? () => onToggle(connector.id) : undefined}
              disabled={!hasChildren}
              className="flex min-w-0 items-center gap-[8px] rounded-[12px] bg-[#f5eee3] px-[10px] py-[8px] text-left transition hover:bg-[#efe4d4] disabled:cursor-default disabled:hover:bg-[#f5eee3]"
              aria-expanded={hasChildren ? isExpanded : undefined}
              style={{ paddingLeft: 10 + depth * 14 }}
            >
              <span className="grid h-[18px] w-[18px] shrink-0 place-items-center text-[#7b6b55]">
                {hasChildren ? <ChevronRight size={14} className={isExpanded ? "rotate-90 transition-transform" : "transition-transform"} /> : null}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[13px] font-semibold text-[#342f27]">{connector.label}</span>
                <span className="block truncate text-[12px] text-[#7c705f]">{connector.capabilities?.join(" · ") || "MCP"}</span>
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
  showCloudFolderLinks,
  depth = 0,
}: {
  items: AssistantSharedFolderItem[];
  expandedItems: Record<string, boolean>;
  onToggle: (id: string) => void;
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
        return (
          <div key={item.id} className={depth ? "grid gap-[7px]" : undefined}>
            <div className="flex items-center gap-[6px]">
              <button
                type="button"
                onClick={handleClick}
                disabled={!hasChildren && !canOpenFile}
                className="flex min-w-0 flex-1 items-center gap-[8px] rounded-[12px] bg-[#f5eee3] px-[10px] py-[8px] text-left transition hover:bg-[#efe4d4] disabled:cursor-default disabled:hover:bg-[#f5eee3]"
                aria-expanded={hasChildren ? isExpanded : undefined}
                title={canOpenFile ? "Datei öffnen" : undefined}
                style={{ paddingLeft: 10 + depth * 14 }}
              >
                <span className="grid h-[18px] w-[18px] shrink-0 place-items-center text-[#7b6b55]">
                  {hasChildren ? <ChevronRight size={14} className={isExpanded ? "rotate-90 transition-transform" : "transition-transform"} /> : null}
                </span>
                <span className="grid h-[22px] w-[22px] shrink-0 place-items-center rounded-[8px] bg-[#ebe1d0] text-[#78684f]">
                  {isFolder ? <FolderOpen size={14} /> : <FileText size={14} />}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[13px] font-semibold text-[#342f27]">{item.name}</span>
                  <span className="block truncate text-[12px] text-[#7c705f]">{isFolder ? "Ordner" : "Datei"} · {meta}</span>
                </span>
              </button>
              {canOpenCloudFolder && (
                <button
                  type="button"
                  onClick={() => openSharedFolderCloudUrl(item.cloud_url)}
                  title="Ordner in cloud.aiwerk.ch öffnen"
                  aria-label={`${item.name} in cloud.aiwerk.ch öffnen`}
                  className="grid h-[34px] w-[34px] shrink-0 cursor-pointer place-items-center rounded-[11px] border border-[#dfd4c4] bg-[#f8f0e3] text-[#6d5f4d] transition hover:bg-[#efe4d4]"
                >
                  <ExternalLink size={13} />
                </button>
              )}
            </div>
            {hasChildren && isExpanded && (
              <SharedFolderTree
                items={children}
                expandedItems={expandedItems}
                onToggle={onToggle}
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
  children,
}: {
  id: string;
  icon: ReactNode;
  title: string;
  summary: string;
  status: { label: string; dot: string };
  expanded: boolean;
  onToggle: (id: string) => void;
  badge?: string;
  action?: { icon: ReactNode; label: string; onClick: () => void; disabled?: boolean };
  disabled?: boolean;
  children?: ReactNode;
}) {
  return (
    <div className="rounded-[18px] border border-[#dfd4c4] bg-[#fffaf2] shadow-[0_8px_24px_rgba(56,42,20,.05)]">
      <div className="flex items-stretch gap-[6px] p-[12px]">
        <button
          type="button"
          onClick={() => !disabled && onToggle(id)}
          className="flex min-w-0 flex-1 cursor-pointer items-center gap-[10px] border-0 bg-transparent p-0 text-left disabled:cursor-default"
          disabled={disabled}
          aria-expanded={expanded}
        >
          <span className="grid h-[32px] w-[32px] shrink-0 place-items-center rounded-[12px] bg-[#eee4d6] text-[#6d5f4d]">{icon}</span>
          <span className="min-w-0 flex-1">
            <span className="flex items-center gap-[7px] text-[13px] font-bold text-[#302b24]">
              <span className="h-[7px] w-[7px] rounded-full" style={{ backgroundColor: status.dot }} />
              {title}
            </span>
            <span className="mt-[2px] block truncate text-[12px] text-[#7a705f]">{summary}</span>
          </span>
          {badge && <span className="rounded-full bg-[#8b724e] px-[8px] py-[3px] text-[11px] font-bold text-white">{badge}</span>}
        </button>
        {action && (
          <button
            type="button"
            onClick={action.onClick}
            disabled={action.disabled}
            title={action.label}
            aria-label={action.label}
            className="grid h-[32px] w-[32px] shrink-0 cursor-pointer place-items-center rounded-[11px] border border-[#dfd4c4] bg-[#f8f0e3] text-[#6d5f4d] transition hover:bg-[#efe4d4] disabled:cursor-not-allowed disabled:opacity-45 disabled:hover:bg-[#f8f0e3]"
          >
            {action.icon}
          </button>
        )}
      </div>
      {expanded && children && <div className="border-t border-[#eadfce] px-[12px] pb-[12px] pt-[10px]">{children}</div>}
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
