import { Plugin } from "@opencode/plugin/tui";
import { spawn } from "node:child_process";
import { createEffect, createRoot } from "solid-js";

// OpenCode reports its state from the TUI process itself. The TUI is the only
// process that knows which tmux pane is running it, so this plugin reports
// events directly instead of relying on transcript discovery in attention.py.
export default Plugin.define({
  id: "tmux-attention",
  setup(context) {
    if (!process.env.TMUX || !process.env.TMUX_PANE) return;

    let tracked = "";
    let running = false;
    let waiting = false;
    let completion = "";
    let lastText = "";
    let stepFresh = false;
    const subscriptions = [];

    const reset = () => {
      running = false;
      waiting = false;
      completion = "";
      lastText = "";
      stepFresh = false;
    };
    const subscribe = (type, handler) => {
      subscriptions.push(context.data.on(type, handler));
    };

    const activeSession = () => {
      try {
        const route = context.ui.router.current();
        return route.type === "session" ? route.sessionID : "";
      } catch {
        return "";
      }
    };
    const sessionInfo = (sid) => {
      try {
        return context.data.session.get(sid);
      } catch {
        return undefined;
      }
    };
    const sessionStatus = (sid) => {
      try {
        return context.data.session.status(sid);
      } catch {
        return "idle";
      }
    };
    const withMeta = (sid, data = {}, model) => {
      const info = sid ? sessionInfo(sid) : undefined;
      const payload = { ...data };
      if (payload.session_id === undefined && sid) payload.session_id = sid;
      if (payload.title === undefined && info?.title) payload.title = info.title;
      const ref = model || info?.model;
      if (payload.model === undefined && ref?.id) payload.model = ref.id;
      if (payload.effort === undefined && ref) payload.effort = ref.variant || "";
      return payload;
    };
    const send = (event, data = {}, sid = tracked, model) => {
      if (!process.env.TMUX || !process.env.TMUX_PANE) return;
      try {
        const child = spawn("__PYTHON__", ["__ATTENTION__", "event", "opencode", event], {
          stdio: ["pipe", "ignore", "ignore"],
        });
        child.on("error", () => {});
        child.stdin.on("error", () => {});
        child.stdin.end(JSON.stringify(withMeta(sid, data, model)));
      } catch {
        // Attention must never disturb the agent.
      }
    };
    // Follow the routed session, or keep following the last routed session while
    // the user is on the home/plugin page. Other background sessions never drive it.
    const accept = (sid) => {
      if (!sid) return false;
      const active = activeSession();
      if (active ? active !== sid : sid !== tracked) return false;
      if (sid !== tracked) {
        tracked = sid;
        reset();
      }
      return true;
    };
    const started = (sid, model) => {
      running = true;
      waiting = false;
      completion = "";
      send("agent_start", {}, sid, model);
    };
    const busy = (sid) => {
      if (running && !waiting) return;
      started(sid);
    };
    const permissionDetail = (permission) => {
      const label = permission?.message || permission?.action || "Tool permission";
      const resources = Array.isArray(permission?.resources)
        ? permission.resources.slice(0, 2).join(", ")
        : "";
      return String(resources ? `${label}: ${resources}` : label).slice(0, 200);
    };
    const permissions = (sid) => {
      try {
        return context.data.session.permission.list(sid) || [];
      } catch {
        return [];
      }
    };
    const lastAssistantText = (sid) => {
      try {
        const messages = context.data.session.message.list(sid) || [];
        for (let index = messages.length - 1; index >= 0; index--) {
          const message = messages[index];
          if (message?.type !== "assistant" || !Array.isArray(message.content)) continue;
          const text = message.content
            .filter((part) => part?.type === "text" && typeof part.text === "string")
            .map((part) => part.text)
            .join("\n\n")
            .trim();
          if (text) return text;
        }
      } catch {
        // A missing message cache must not block the state transition.
      }
      return "";
    };
    const finish = (sid, event, data = {}) => {
      if (completion === "failed" || completion === "interrupted") return;
      if (event === "Stop" && completion) return;
      completion = event === "Stop" ? "succeeded" : event === "StopFailure" ? "failed" : "interrupted";
      running = false;
      waiting = false;
      const text = (lastText || lastAssistantText(sid) || "").trim();
      send(event, { ...data, last_assistant_message: text }, sid);
      lastText = "";
    };
    const meta = (sid, data = {}, model) => {
      if (!accept(sid)) return;
      send("opencode_meta", data, sid, model);
    };
    // Bring the pane in line with the session the user is currently viewing.
    const syncActive = (sid) => {
      tracked = sid;
      reset();
      const waitingList = permissions(sid);
      if (waitingList.length) {
        waiting = true;
        send("PermissionRequest", { tool_input: { description: permissionDetail(waitingList[0]) } }, sid);
        return;
      }
      if (sessionStatus(sid) === "running") {
        started(sid);
        return;
      }
      const info = sessionInfo(sid);
      const idle = info?.time?.idle;
      if (idle !== undefined && (info.time.viewed === undefined || idle > info.time.viewed)) {
        lastText = lastAssistantText(sid);
        if (info.outcome === "failed") finish(sid, "StopFailure");
        else if (info.outcome === "interrupted") finish(sid, "Interrupt");
        else finish(sid, "Stop");
      } else {
        send("idle_no_review", {}, sid);
        completion = "succeeded";
      }
    };

    subscribe("session.step.started", (event) => {
      const sid = event.data.sessionID;
      if (!accept(sid)) return;
      stepFresh = true;
      if (!running || waiting) {
        started(sid, event.data.model);
      } else if (event.data.model) {
        meta(sid, {}, event.data.model);
      }
    });
    subscribe("session.inbox.enqueued", (event) => {
      const sid = event.data.sessionID;
      if (!accept(sid) || event.data.item?.type !== "user") return;
      running = true;
      waiting = false;
      completion = "";
      lastText = "";
      stepFresh = false;
      send("UserPromptSubmit", { prompt: event.data.item.payload?.text || "" }, sid);
    });
    subscribe("session.execution.started", (event) => {
      const sid = event.data.sessionID;
      if (!accept(sid)) return;
      if (!running || waiting) {
        lastText = "";
        stepFresh = false;
        started(sid);
      }
    });
    subscribe("session.status", (event) => {
      const sid = event.data.sessionID;
      if (!accept(sid)) return;
      const status = event.data.status?.type;
      if (status === "busy" || status === "retry") busy(sid);
      else if (status === "idle" && (running || waiting)) finish(sid, "Stop");
    });
    subscribe("session.text.ended", (event) => {
      const sid = event.data.sessionID;
      if (!accept(sid)) return;
      const text = String(event.data.text || "").trim();
      if (!text) return;
      lastText = !stepFresh && lastText ? `${lastText}\n\n${text}` : text;
      stepFresh = false;
      if (lastText.length > 20000) lastText = lastText.slice(-12000);
    });
    subscribe("session.execution.succeeded", (event) => {
      const sid = event.data.sessionID;
      if (!accept(sid)) return;
      finish(sid, "Stop");
    });
    subscribe("session.idle", (event) => {
      const sid = event.data.sessionID;
      if (!accept(sid) || (!running && !waiting)) return;
      finish(sid, "Stop");
    });
    subscribe("session.execution.failed", (event) => {
      const sid = event.data.sessionID;
      if (!accept(sid)) return;
      finish(sid, "StopFailure");
    });
    subscribe("session.execution.interrupted", (event) => {
      const sid = event.data.sessionID;
      if (!accept(sid)) return;
      finish(sid, "Interrupt");
    });
    subscribe("session.deleted", (event) => {
      const sid = event.data.sessionID;
      if (sid !== tracked) return;
      send("SessionEnd", {}, sid);
      tracked = "";
      reset();
    });
    subscribe("permission.asked", (event) => {
      const sid = event.data.sessionID;
      if (!accept(sid)) return;
      waiting = true;
      running = false;
      completion = "";
      send("PermissionRequest", { tool_input: { description: permissionDetail(event.data) } }, sid);
    });
    subscribe("permission.replied", (event) => {
      const sid = event.data.sessionID;
      if (!accept(sid)) return;
      completion = "";
      const remaining = permissions(sid);
      if (remaining.length) {
        waiting = true;
        running = false;
        send("PermissionRequest", { tool_input: { description: permissionDetail(remaining[0]) } }, sid);
      } else {
        waiting = false;
        running = true;
        send("PostToolUse", {}, sid);
      }
    });
    subscribe("session.created", (event) => {
      meta(event.data.sessionID, event.data.title ? { title: event.data.title } : {}, event.data.model);
    });
    subscribe("session.renamed", (event) => {
      meta(event.data.sessionID, { title: event.data.title });
    });
    subscribe("session.model.selected", (event) => {
      meta(event.data.sessionID, {}, event.data.model);
    });

    // Local navigation does not emit an event of its own, so follow the router.
    // Re-entry must resync even for the same session: it may have finished while
    // the user was on the home or plugin route.
    let routed = "";
    const disposeRoute = createRoot((dispose) => {
      createEffect(() => {
        const sid = activeSession();
        if (sid === routed) return;
        routed = sid;
        if (sid) syncActive(sid);
      });
      return dispose;
    });
    return () => {
      for (const dispose of subscriptions.splice(0)) dispose();
      disposeRoute();
    };
  },
});
