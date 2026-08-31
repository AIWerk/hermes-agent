import { useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import { Bot, Send, Square, User } from "lucide-react";

import { Markdown } from "@/components/Markdown";
import { api } from "@/lib/api";
import { buildApprovalResponseParams } from "@/lib/cui-approval";
import { buildWelcomeMessage, resolveGreetingName } from "@/lib/cui-greeting";
import { CUI_SUPPORTED_SLASH_COMMANDS, isCuiSlashInput, slashBase } from "@/lib/cui-slash";
import { GatewayClient, type ConnectionState } from "@/lib/gatewayClient";

interface Message {
  id: string;
  role: "assistant" | "user" | "system";
  text: string;
}

interface ApprovalRequest {
  id: string;
  prompt: string;
}

function messageText(payload: unknown): string {
  if (!payload || typeof payload !== "object") return "";
  const record = payload as Record<string, unknown>;
  for (const key of ["text", "content", "message", "delta"]) {
    if (typeof record[key] === "string") return record[key] as string;
  }
  return "";
}

export function AiwerkAssistantPage() {
  const gateway = useMemo(() => new GatewayClient(), []);
  const sessionId = useRef("");
  const [connection, setConnection] = useState<ConnectionState>("idle");
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [error, setError] = useState("");
  const [approval, setApproval] = useState<ApprovalRequest | null>(null);

  useEffect(() => {
    let cancelled = false;
    const offState = gateway.onState(setConnection);
    const offDelta = gateway.on<Record<string, unknown>>("message.delta", (event) => {
      const text = messageText(event.payload);
      if (!text) return;
      setMessages((current) => {
        const last = current.at(-1);
        if (last?.role === "assistant" && last.id === "stream") {
          return [...current.slice(0, -1), { ...last, text: last.text + text }];
        }
        return [...current, { id: "stream", role: "assistant", text }];
      });
    });
    const offComplete = gateway.on<Record<string, unknown>>("message.complete", (event) => {
      const finalText = messageText(event.payload);
      setMessages((current) => {
        const last = current.at(-1);
        if (last?.id !== "stream") {
          return finalText
            ? [...current, { id: crypto.randomUUID(), role: "assistant", text: finalText }]
            : current;
        }
        return [
          ...current.slice(0, -1),
          { ...last, id: crypto.randomUUID(), text: finalText || last.text },
        ];
      });
    });
    const offError = gateway.on<{ message?: string }>("error", (event) => {
      setError(event.payload?.message || "Die Verbindung zum Assistenten ist fehlgeschlagen.");
    });
    const offApproval = gateway.on<Record<string, unknown>>("approval.request", (event) => {
      const payload = event.payload ?? {};
      const id = String(payload.id || payload.request_id || "");
      if (id) setApproval({ id, prompt: messageText(payload) || "Aktion bestätigen?" });
    });

    void (async () => {
      let displayName: string | null = null;
      try {
        const identity = await api.getAuthMe();
        displayName = resolveGreetingName(identity.display_name?.split(/\s+/, 1)[0], "customer");
      } catch {
        displayName = null;
      }
      if (!cancelled) {
        setMessages([
          {
            id: "welcome",
            role: "assistant",
            text: buildWelcomeMessage(displayName, "customer"),
          },
        ]);
      }
      try {
        await gateway.connect();
        const created = await gateway.request<{ session_id: string }>("session.create", {
          source: "web",
          close_on_disconnect: true,
        });
        sessionId.current = created.session_id;
      } catch (reason) {
        if (!cancelled) setError(reason instanceof Error ? reason.message : String(reason));
      }
    })();

    return () => {
      cancelled = true;
      offState();
      offDelta();
      offComplete();
      offError();
      offApproval();
      gateway.close();
    };
  }, [gateway]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const text = input.trim();
    if (!text || !sessionId.current) return;
    setInput("");
    setError("");
    setMessages((current) => [
      ...current,
      { id: crypto.randomUUID(), role: "user", text },
    ]);
    try {
      if (isCuiSlashInput(text)) {
        const command = slashBase(text);
        if (!CUI_SUPPORTED_SLASH_COMMANDS.has(command)) {
          throw new Error("Dieser Slash-Befehl ist in der Kundenansicht nicht verfügbar.");
        }
        await gateway.request("slash.exec", {
          session_id: sessionId.current,
          command: text,
        });
      } else {
        await gateway.request("prompt.submit", {
          session_id: sessionId.current,
          text,
        });
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const respondApproval = async (approved: boolean) => {
    if (!approval) return;
    try {
      await gateway.request(
        "approval.respond",
        buildApprovalResponseParams(sessionId.current, approval.id, approved),
      );
      setApproval(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  return (
    <main data-aiwerk-surface="assistant" className="min-h-dvh bg-background-base text-text-primary">
      <div className="mx-auto flex min-h-dvh max-w-4xl flex-col px-4 py-6 sm:px-8">
        <header className="mb-6 flex items-center justify-between border-b border-current/15 pb-4">
          <div>
            <p className="text-xs uppercase tracking-[0.2em] text-text-secondary">AIWerk</p>
            <h1 className="text-xl font-semibold">Persönlicher Assistent</h1>
          </div>
          <span className="text-xs text-text-secondary" aria-live="polite">{connection}</span>
        </header>

        <section aria-label="Unterhaltung" className="flex-1 space-y-4 overflow-y-auto pb-6">
          {messages.map((message) => (
            <article key={message.id} className="flex gap-3">
              <span aria-hidden className="mt-1">{message.role === "user" ? <User size={18} /> : <Bot size={18} />}</span>
              <div className="min-w-0 flex-1 rounded-xl border border-current/10 bg-background-elevated p-4">
                <Markdown content={message.text} />
              </div>
            </article>
          ))}
        </section>

        {approval && (
          <section className="mb-4 rounded-xl border border-warning/50 p-4" aria-label="Bestätigung erforderlich">
            <p className="mb-3">{approval.prompt}</p>
            <div className="flex gap-2">
              <button type="button" onClick={() => void respondApproval(true)} className="rounded bg-primary px-3 py-2">Bestätigen</button>
              <button type="button" onClick={() => void respondApproval(false)} className="rounded border px-3 py-2">Ablehnen</button>
            </div>
          </section>
        )}

        {error && <p role="alert" className="mb-3 text-sm text-destructive">{error}</p>}

        <form onSubmit={submit} className="flex gap-2 border-t border-current/15 pt-4">
          <label htmlFor="assistant-input" className="sr-only">Nachricht</label>
          <textarea
            id="assistant-input"
            value={input}
            onChange={(event) => setInput(event.target.value)}
            rows={2}
            placeholder="Was soll ich erledigen?"
            className="min-h-12 flex-1 resize-none rounded-xl border border-current/20 bg-background-elevated px-4 py-3"
          />
          <button type="submit" disabled={!input.trim() || connection !== "open"} aria-label="Senden" className="rounded-xl bg-primary px-4">
            <Send size={18} />
          </button>
          <button type="button" onClick={() => setInput("/stop")} aria-label="Stoppen" className="rounded-xl border px-4">
            <Square size={18} />
          </button>
        </form>
      </div>
    </main>
  );
}

export default AiwerkAssistantPage;
