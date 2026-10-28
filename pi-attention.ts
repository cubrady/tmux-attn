import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

export default function (pi: ExtensionAPI) {
  let lastText = "";
  const stateDir = process.env.TMUX_ATTN_STATE_DIR
    || join(process.env.HOME || "", ".local/state/tmux-attn");
  const beat = () => {
    try {
      const pane = process.env.TMUX_PANE || "unknown";
      mkdirSync(join(stateDir, "pi-heartbeats"), { recursive: true });
      writeFileSync(join(stateDir, "pi-heartbeats", `${pane.replace("%", "")}.json`), JSON.stringify({
        at: Date.now() / 1000,
        pane,
        pid: process.pid,
      }));
    } catch {
      /* best-effort heartbeat for observer */
    }
  };
  let lastPrompt = "";
  const isChildSession = (ctx?: any): boolean => {
    try {
      if (process.argv.some((arg) => arg === "-p" || arg === "--print" || arg === "--mode")) {
        return true;
      }
      return Boolean(ctx?.sessionManager?.getHeader?.()?.parentSession);
    } catch {
      return false;
    }
  };
  const sessionPath = (ctx: any): string => {
    try {
      return ctx?.sessionManager?.getSessionFile?.() || "";
    } catch {
      return "";
    }
  };
  const report = (event: string, data: object = {}, ctx?: any) => {
    if (!process.env.TMUX || !process.env.TMUX_PANE || isChildSession(ctx)) return;
    beat();
    const transcript_path = sessionPath(ctx);
    const payload = transcript_path ? { transcript_path, ...data } : data;
    // Absolute paths are filled by install.py. No provider calls or extra dependencies.
    const child = spawn("__PYTHON__", ["__ATTENTION__", "event", "pi", event], {
      stdio: ["pipe", "ignore", "ignore"],
    });
    return new Promise<void>((resolve) => {
      child.on("error", () => resolve());
      child.on("close", () => resolve());
      child.stdin.on("error", () => {});
      child.stdin.end(JSON.stringify(payload));
    });
  };
  pi.on("session_start", (_event, ctx) => report("session_start", {}, ctx));
  pi.on("before_agent_start", (event) => {
    if (event.messageType === "user" && typeof event.prompt === "string" && event.prompt.trim()) {
      lastPrompt = event.prompt.trim();
    }
    return undefined;
  });
  pi.on("agent_start", (_event, ctx) => {
    lastText = "";
    const prompt = lastPrompt;
    lastPrompt = "";
    return report("agent_start", prompt ? { prompt } : {}, ctx);
  });
  pi.on("tool_execution_start", (_event, ctx) => report("tool_execution_start", {}, ctx));
  pi.on("message_end", (event) => {
    if (event.message.role === "assistant") {
      lastText = event.message.content.filter((block) => block.type === "text")
        .map((block) => block.text).join("\n");
    }
  });
  pi.on("ui_prompt_start", (event, ctx) => report("ui_prompt_start", {
    title: event.title,
    kind: event.kind,
  }, ctx));
  pi.on("ui_prompt_end", (_event, ctx) => report(ctx.isIdle() ? "idle_no_review" : "ui_prompt_end", {}, ctx));
  pi.on("agent_settled", (_event, ctx) => report("agent_settled", {last_assistant_message: lastText}, ctx));
  pi.on("session_shutdown", (_event, ctx) => report("session_shutdown", {}, ctx));
}
