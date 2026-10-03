import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawn } from "node:child_process";

export default function (pi: ExtensionAPI) {
  let lastText = "";
  const report = (event: string, data: object = {}) => {
    if (!process.env.TMUX || !process.env.TMUX_PANE) return;
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
  pi.on("message_end", (event) => {
    if (event.message.role === "assistant") {
      lastText = event.message.content.filter((block) => block.type === "text")
        .map((block) => block.text).join("\n");
    }
  });
  pi.on("ui_prompt_start", (event) => report("ui_prompt_start", {title: event.title}));
  pi.on("ui_prompt_end", (_event, ctx) => report(ctx.isIdle() ? "idle_no_review" : "ui_prompt_end"));
  pi.on("agent_settled", () => report("agent_settled", {last_assistant_message: lastText}));
  pi.on("session_shutdown", () => report("session_shutdown"));
}
