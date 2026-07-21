import { randomUUID } from "node:crypto"


const AGENTS = new Set(["architect", "builder", "reviewer", "tester", "general"])
const SENSITIVE_TOOLS = new Set(["bash", "edit", "apply_patch", "write"])


export function createRouteGate(options = {}) {
  const createTicket = options.createTicket ?? randomUUID
  const now = options.now ?? Date.now
  const ttlMs = options.ttlMs ?? 5 * 60 * 1000
  const sessionRoutes = new Map()
  const tickets = new Map()
  const authorizedChildren = new Map()

  function issue(sessionID, route) {
    if (route?.status === "handoff" && route.agent === null) return { ...route }
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
  }

  function authorizeChild(sessionID, agent, text) {
    const match = typeof text === "string" ? text.match(/AF-TICKET:([A-Za-z0-9._-]+)/) : null
    const record = match ? tickets.get(match[1]) : undefined
    if (!record || !record.consumed || record.target !== agent) {
      throw new Error("AF route denied: child session lacks a matching route ticket")
    }
    authorizedChildren.set(sessionID, { ticket: record.ticket, target: record.target })
  }

  function guardTool(sessionID, tool) {
    if (SENSITIVE_TOOLS.has(tool) && !authorizedChildren.has(sessionID)) {
      throw new Error(`AF route denied: ${tool} requires a validated route ticket`)
    }
  }

  function revokeSession(sessionID) {
    authorizedChildren.delete(sessionID)
    const ticket = sessionRoutes.get(sessionID)
    sessionRoutes.delete(sessionID)
    if (ticket) tickets.delete(ticket)
  }

  return { authorizeChild, beforeTask, guardTool, issue, revokeSession }
}
