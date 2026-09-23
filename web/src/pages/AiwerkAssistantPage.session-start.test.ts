import { describe, expect, it } from "vitest";

import {
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
      id: "session-start-aiwerk_daily_briefing",
      role: "agent",
      text: "## Daily briefing\nEverything is ready.",
      status: "complete",
    });
    expect(sessionStartPromptRequest(task, "runtime-session")).toBeNull();
  });

  it("shows a loading row and builds a detached background request for generation", () => {
    const task: SessionStartTask = {
      kind: "session_start_task",
      mode: "generate",
      plugin_id: "aiwerk_daily_briefing",
      local_date: "2026-09-23",
      prompt: "Generate today's briefing",
    };

    expect(sessionStartMessage(task)).toEqual({
      id: "session-start-aiwerk_daily_briefing",
      role: "agent",
      text: "Dein tägliches Briefing wird erstellt …",
      status: "streaming",
    });
    expect(sessionStartPromptRequest(task, "runtime-session")).toEqual({
      session_id: "runtime-session",
      text: "Generate today's briefing",
      startup_task: {
        plugin_id: "aiwerk_daily_briefing",
        local_date: "2026-09-23",
      },
    });
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
