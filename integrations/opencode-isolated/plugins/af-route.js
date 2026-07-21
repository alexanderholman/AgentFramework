import { tool } from "@opencode-ai/plugin"

import { createRouteGate } from "../lib/route-gate-core.js"


function messageText(parts) {
  return (parts ?? [])
    .filter((part) => part?.type === "text" && typeof part.text === "string")
    .map((part) => part.text)
    .join("\n")
}


export const AFRoutePlugin = async () => {
  const gate = createRouteGate()
  const seenCalls = new Set()
  const cli = process.env.AF_ROUTER_CLI
  if (!cli) throw new Error("AF_ROUTER_CLI is required")

  return {
    tool: {
      af_route: tool({
        description: "Route a complete request through AF and issue a single-use delegation ticket.",
        args: {
          request: tool.schema.string().min(1).max(10000),
        },
        async execute(args, context) {
          if (context.agent !== "dispatcher") {
            throw new Error("AF route denied: only dispatcher may issue route tickets")
          }
          const process = Bun.spawn([cli, "route-agent", args.request], {
            stdout: "pipe",
            stderr: "pipe",
          })
          const [exitCode, stdout, stderr] = await Promise.all([
            process.exited,
            new Response(process.stdout).text(),
            new Response(process.stderr).text(),
          ])
          if (exitCode !== 0) {
            throw new Error(`AF route failed: ${stderr.trim() || "unknown failure"}`)
          }
          const issued = gate.issue(context.sessionID, JSON.parse(stdout))
          const ticketLine = issued.ticket ? `\nAF-TICKET:${issued.ticket}` : ""
          return `${JSON.stringify(issued)}${ticketLine}`
        },
      }),
    },

    "chat.message": async (input, output) => {
      const text = messageText(output.parts)
      if (!text.includes("AF-TICKET:")) return
      const agent = input.agent ?? output.message?.agent
      gate.authorizeChild(input.sessionID, agent, text)
    },

    "tool.execute.before": async (input, output) => {
      const key = `${input.sessionID}:${input.callID}`
      if (seenCalls.has(key)) return
      if (input.tool === "task") gate.beforeTask(input.sessionID, output.args)
      gate.guardTool(input.sessionID, input.tool)
      seenCalls.add(key)
    },

    event: async ({ event }) => {
      if (event.type !== "session.deleted") return
      const sessionID = event.properties?.info?.id ?? event.properties?.sessionID
      if (sessionID) gate.revokeSession(sessionID)
    },
  }
}
