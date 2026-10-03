import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
export default function (pi: ExtensionAPI) {
  pi.registerCommand("attncheck", {
    description: "Local attention UI validation; no model request",
    handler: async (_args, ctx) => {
      await ctx.ui.confirm("Attention validation", "Local test: confirm to finish");
    },
  });
}
