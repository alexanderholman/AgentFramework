import { randomUUID } from "node:crypto"


const AGENTS = new Set(["architect", "builder", "reviewer", "tester", "general"])
const SENSITIVE_TOOLS = new Set([
  "bash",
  "edit",
  "apply_patch",
  "write",
  "read",
  "glob",
  "grep",
  "list",
  "webfetch",
  "websearch",
])


export function createRouteGate(options = {}) {
  const createTicket = options.createTicket ?? randomUUID
  const now = options.now ?? Date.now
  const ttlMs = options.ttlMs ?? 5 * 60 * 1000
  const sessionRoutes = new Map()
  const sessionStates = new Map()
  const tickets = new Map()
  const authorizedChildren = new Map()

  function begin(sessionID) {
    const previousTicket = sessionRoutes.get(sessionID)
    if (previousTicket) tickets.delete(previousTicket)
    sessionRoutes.delete(sessionID)
    sessionStates.set(sessionID, { status: "awaiting_route" })
  }

  function issue(sessionID, route) {
    if (route?.status === "handoff" && route.agent === null) {
      sessionStates.set(sessionID, { status: "handoff" })
      return { ...route }
    }
    if (route?.status !== "routed" || !AGENTS.has(route.agent)) {
      throw new Error("AF route denied: invalid routing decision")
    }
    const ticket = createTicket()
    const record = {
      ticket,
      issuerSessionID: sessionID,
      target: route.agent,
      issuedAt: now(),
      consumed: false,
    }
    tickets.set(ticket, record)
    sessionRoutes.set(sessionID, ticket)
    sessionStates.set(sessionID, { status: "issued", ticket })
    return { ...route, ticket }
  }

  function beforeTask(sessionID, args) {
    const ticket = sessionRoutes.get(sessionID)
    const record = ticket ? tickets.get(ticket) : undefined
    if (!record || record.consumed) {
      throw new Error("AF route denied: an unconsumed route ticket is required")
    }
    if (now() - record.issuedAt > ttlMs) {
      sessionRoutes.delete(sessionID)
      throw new Error("AF route denied: route ticket expired")
    }
    if (args?.subagent_type !== record.target) {
      throw new Error("AF route denied: delegated target does not match route ticket")
    }
    if (typeof args?.prompt !== "string" || !args.prompt.includes(`AF-TICKET:${ticket}`)) {
      throw new Error("AF route denied: delegated prompt is missing the route ticket")
    }
    record.consumed = true
    sessionRoutes.delete(sessionID)
    sessionStates.set(sessionID, { status: "awaiting_result", ticket })
  }

  function authorizeChild(sessionID, agent, text) {
    const match = typeof text === "string" ? text.match(/AF-TICKET:([A-Za-z0-9._-]+)/) : null
    const record = match ? tickets.get(match[1]) : undefined
    if (!record || !record.consumed || record.target !== agent) {
      throw new Error("AF route denied: child session lacks a matching route ticket")
    }
    if (now() - record.issuedAt > ttlMs) {
      throw new Error("AF route denied: route ticket expired")
    }
    if (record.authorizedChild) {
      throw new Error("AF route denied: route ticket already authorized a child session")
    }
    record.authorizedChild = sessionID
    authorizedChildren.set(sessionID, { ticket: record.ticket, target: record.target })
  }

  function completeTask(sessionID, output) {
    const wrapped = typeof output === "string"
      ? output.match(/<task_result>\s*([\s\S]*?)\s*<\/task_result>/)
      : null
    const result = (wrapped ? wrapped[1] : output)?.trim?.() ?? ""
    sessionStates.set(sessionID, { status: result ? "completed" : "failed" })
  }

  function guardDispatcherText(sessionID) {
    const status = sessionStates.get(sessionID)?.status
    if (status === "handoff" || status === "completed") return
    if (status === "awaiting_route" || status === undefined) {
      throw new Error("AF route denied: a route is required before dispatcher text")
    }
    throw new Error("AF route denied: a non-empty specialist result is required")
  }

  function guardSpecialistText(sessionID) {
    if (!authorizedChildren.has(sessionID)) {
      throw new Error("AF route denied: specialist text requires a validated route ticket")
    }
  }

  function guardAgent(sessionID, agent) {
    const authorization = authorizedChildren.get(sessionID)
    if (authorization && authorization.target !== agent) {
      throw new Error("AF route denied: active agent does not match route ticket")
    }
  }

  function guardTool(sessionID, tool, agent) {
    if (agent) guardAgent(sessionID, agent)
    if (tool === "question") {
      if (agent === "dispatcher") {
        if (sessionStates.get(sessionID)?.status !== "handoff") {
          throw new Error("AF route denied: dispatcher question requires an ambiguous handoff")
        }
        return
      }
      if (!authorizedChildren.has(sessionID)) {
        throw new Error("AF route denied: question requires a validated route ticket")
      }
      return
    }
    if (SENSITIVE_TOOLS.has(tool) && !authorizedChildren.has(sessionID)) {
      throw new Error(`AF route denied: ${tool} requires a validated route ticket`)
    }
  }

  function revokeSession(sessionID) {
    authorizedChildren.delete(sessionID)
    sessionStates.delete(sessionID)
    const ticket = sessionRoutes.get(sessionID)
    sessionRoutes.delete(sessionID)
    if (ticket) tickets.delete(ticket)
  }

  return {
    authorizeChild,
    beforeTask,
    begin,
    completeTask,
    guardDispatcherText,
    guardAgent,
    guardSpecialistText,
    guardTool,
    issue,
    revokeSession,
  }
}
