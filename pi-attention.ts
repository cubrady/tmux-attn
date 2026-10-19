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
  const report = (event: string, data: object = {}) => {
    if (!process.env.TMUX || !process.env.TMUX_PANE) return;
    beat();
    // Absolute paths are filled by install.py. No provider calls or extra dependencies.
    const child = spawn("__PYTHON__", ["__ATTENTION__", "event", "pi", event], {
      stdio: ["pipe", "ignore", "ignore"],
    });
    return new Promise<void>((resolve) => {
      child.on("error", () => resolve());
      child.on("close", () => resolve());
      child.stdin.on("error", () => {});
      child.stdin.end(JSON.stringify(data));
    });
  };
  pi.on("session_start", () => report("session_start"));
  pi.on("agent_start", () => { lastText = ""; return report("agent_start"); });
  pi.on("tool_execution_start", () => report("tool_execution_start"));
  pi.on("message_end", (event) => {
    if (event.message.role === "assistant") {
      lastText = event.message.content.filter((block) => block.type === "text")
        .map((block) => block.text).join("\n");
    }
  });
  pi.on("ui_prompt_start", (event) => report("ui_prompt_start", {
    title: event.title,
    kind: event.kind,
  }));
  pi.on("ui_prompt_end", (_event, ctx) => report(ctx.isIdle() ? "idle_no_review" : "ui_prompt_end"));
  pi.on("agent_settled", () => report("agent_settled", {last_assistant_message: lastText}));
  pi.on("session_shutdown", () => report("session_shutdown"));
}
