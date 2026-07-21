import assert from "node:assert/strict"
import test from "node:test"

import { createRouteGate } from "../../integrations/opencode-isolated/plugins/route-gate-core.js"


test("route tickets authorize exactly one matching delegation", () => {
  const gate = createRouteGate({ createTicket: () => "ticket-1", now: () => 1000 })
  const issued = gate.issue("parent", {
    status: "routed",
    agent: "builder",
    candidates: ["builder"],
  })

  assert.equal(issued.ticket, "ticket-1")
  assert.throws(
    () => gate.beforeTask("parent", { subagent_type: "tester", prompt: "AF-TICKET:ticket-1" }),
    /target does not match/,
  )
  gate.beforeTask("parent", {
    subagent_type: "builder",
    prompt: "Implement it\nAF-TICKET:ticket-1",
  })
  assert.throws(
    () => gate.beforeTask("parent", {
      subagent_type: "builder",
      prompt: "AF-TICKET:ticket-1",
    }),
    /route ticket.*required/,
  )
})


test("specialist tools require child authorization", () => {
  const gate = createRouteGate({ createTicket: () => "ticket-2", now: () => 1000 })
  gate.issue("parent", { status: "routed", agent: "tester", candidates: ["tester"] })
  gate.beforeTask("parent", {
    subagent_type: "tester",
    prompt: "Verify it\nAF-TICKET:ticket-2",
  })

  assert.throws(() => gate.guardTool("child", "bash"), /validated route ticket/)
  gate.authorizeChild("child", "tester", "Verify it\nAF-TICKET:ticket-2")
  assert.doesNotThrow(() => gate.guardTool("child", "bash"))
})


test("ambiguous handoffs never issue tickets", () => {
  const gate = createRouteGate({ createTicket: () => "unused", now: () => 1000 })

  const result = gate.issue("parent", {
    status: "handoff",
    agent: null,
    candidates: ["architect", "builder"],
  })

  assert.equal(result.ticket, undefined)
  assert.throws(
    () => gate.beforeTask("parent", { subagent_type: "architect", prompt: "anything" }),
    /route ticket.*required/,
  )
})


test("expired tickets fail closed", () => {
  let current = 1000
  const gate = createRouteGate({
    createTicket: () => "ticket-3",
    now: () => current,
    ttlMs: 50,
  })
  gate.issue("parent", { status: "routed", agent: "reviewer", candidates: ["reviewer"] })
  current = 1100

  assert.throws(
    () => gate.beforeTask("parent", {
      subagent_type: "reviewer",
      prompt: "AF-TICKET:ticket-3",
    }),
    /expired/,
  )
})
