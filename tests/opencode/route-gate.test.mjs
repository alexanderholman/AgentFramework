import assert from "node:assert/strict"
import test from "node:test"

import { createRouteGate } from "../../integrations/opencode-isolated/lib/route-gate-core.js"


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
  assert.throws(
    () => gate.authorizeChild("second-child", "tester", "AF-TICKET:ticket-2"),
    /already authorized/,
  )
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


test("expired consumed tickets cannot authorize children", () => {
  let current = 1000
  const gate = createRouteGate({
    createTicket: () => "ticket-4",
    now: () => current,
    ttlMs: 50,
  })
  gate.issue("parent", { status: "routed", agent: "builder", candidates: ["builder"] })
  gate.beforeTask("parent", {
    subagent_type: "builder",
    prompt: "AF-TICKET:ticket-4",
  })
  current = 1100

  assert.throws(
    () => gate.authorizeChild("child", "builder", "AF-TICKET:ticket-4"),
    /expired/,
  )
})


test("dispatcher text requires a successful routed delegation", () => {
  const gate = createRouteGate({ createTicket: () => "ticket-5", now: () => 1000 })
  gate.begin("parent")
  assert.throws(() => gate.guardDispatcherText("parent"), /route is required/)

  gate.issue("parent", { status: "routed", agent: "reviewer", candidates: ["reviewer"] })
  gate.beforeTask("parent", {
    subagent_type: "reviewer",
    prompt: "AF-TICKET:ticket-5",
  })
  gate.completeTask("parent", "<task_result>\n\n</task_result>")
  assert.throws(() => gate.guardDispatcherText("parent"), /specialist result is required/)

  gate.completeTask("parent", "<task_result>Reviewed output</task_result>")
  assert.doesNotThrow(() => gate.guardDispatcherText("parent"))
})


test("ambiguous handoff allows dispatcher clarification text", () => {
  const gate = createRouteGate({ createTicket: () => "unused", now: () => 1000 })
  gate.begin("parent")
  gate.issue("parent", {
    status: "handoff",
    agent: null,
    candidates: ["architect", "builder"],
  })

  assert.doesNotThrow(() => gate.guardDispatcherText("parent"))
})


test("authorized child cannot switch to another agent", () => {
  const gate = createRouteGate({ createTicket: () => "ticket-6", now: () => 1000 })
  gate.issue("parent", { status: "routed", agent: "general", candidates: ["general"] })
  gate.beforeTask("parent", {
    subagent_type: "general",
    prompt: "AF-TICKET:ticket-6",
  })
  gate.authorizeChild("child", "general", "AF-TICKET:ticket-6")

  assert.throws(() => gate.guardAgent("child", "builder"), /agent does not match/)
  assert.throws(() => gate.guardTool("child", "edit", "builder"), /agent does not match/)
  assert.doesNotThrow(() => gate.guardTool("child", "read", "general"))
})
