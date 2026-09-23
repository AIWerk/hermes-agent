import { describe, expect, it } from "vitest";

import {
  sessionStartCompletionMessageId,
  sessionStartMessage,
  sessionStartPromptRequest,
  type SessionStartTask,
} from "./AiwerkAssistantPage";


describe("AIWerk CUI session-start briefing", () => {
  it("places cached briefing text directly below the welcome message", () => {
    const task: SessionStartTask = {
      kind: "session_start_task",
      mode: "cached",
      plugin_id: "aiwerk_daily_briefing",
      local_date: "2026-09-23",
      text: "## Daily briefing\nEverything is ready.",
    };

    expect(sessionStartMessage(task)).toEqual({
      id: "session-start-aiwerk_daily_briefing-2026-09-23",
      role: "agent",
      text: "## Daily briefing\nEverything is ready.",
      status: "complete",
    });
    expect(sessionStartPromptRequest(task, "runtime-session")).toBeNull();
  });

  it("shows a loading row and uses only the server-issued task token", () => {
    const task: SessionStartTask = {
      kind: "session_start_task",
      mode: "generate",
      plugin_id: "aiwerk_daily_briefing",
      local_date: "2026-09-23",
      task_token: "issued-token",
      prompt: "Generate today's briefing",
    };

    expect(sessionStartMessage(task)).toEqual({
      id: "session-start-aiwerk_daily_briefing-issued-token",
      role: "agent",
      text: "Dein tägliches Briefing wird erstellt …",
      status: "streaming",
    });
    expect(sessionStartPromptRequest(task, "runtime-session")).toEqual({
      session_id: "runtime-session",
      text: "Generate today's briefing",
      startup_task_token: "issued-token",
    });
  });

  it("ignores completion events from a previous runtime session", () => {
    const startup = { plugin_id: "aiwerk_daily_briefing", task_token: "issued-token" };

    expect(sessionStartCompletionMessageId("old-session", "new-session", startup)).toBeNull();
    expect(sessionStartCompletionMessageId("new-session", "new-session", startup)).toBe(
      "session-start-aiwerk_daily_briefing-issued-token",
    );
  });

  it("rejects malformed or unsupported startup tasks", () => {
    expect(sessionStartMessage({ kind: "other" } as unknown as SessionStartTask)).toBeNull();
    expect(sessionStartMessage({
      kind: "session_start_task",
      mode: "cached",
      plugin_id: "aiwerk_daily_briefing",
      local_date: "2026-09-23",
      text: "",
    })).toBeNull();
  });
});
