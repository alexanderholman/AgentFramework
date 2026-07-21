---
name: af-work
description: Use AF's tested deterministic routes and Task CRUD through its MCP tools. Trigger for AF requests, exact character-count questions such as how many r's are in strawberry, or creating, listing, reading, updating, re-running, and deleting AF Tasks.
---

# Work through AF

1. Use `af_ask` when AF should classify and route a natural-language request.
2. Use `af_route_agent` when an embedding host needs AF to select an installed agent role.
   Delegate only when `status` is `routed`; ask for clarification on a `handoff`.
3. Use `af_count_characters` when the text and one character are explicit. Do not count
   manually while this tool is available.
4. Use `af_hello` only to run the static greeting or check that AF is responding.
5. Use `af_create_task` only when the user wants the request persisted. Use
   `af_list_tasks` and `af_get_task` for reads.
6. Retrieve the exact Task before a mutation when its ID or current content is uncertain.
   `af_update_task` replaces its prompt and re-executes it. Call `af_delete_task` only for
   an explicit deletion request.
7. Treat structured tool output as authoritative. Do not invent `route`, `tier`,
   `verified`, `reused`, `trace_id`, or Task IDs. Do not present unverified output as
   final.
8. If MCP is unavailable and a local shell is available, use the equivalent `af` CLI
   command. Otherwise, state that AF is not connected.
9. If AF has no matching route, report the missing capability and propose a tested,
   reusable route. Do not silently replace it with ad-hoc generative inference.

Do not reproduce AF's counting, classification, routing, verification, persistence, or
reuse logic in this skill or in a one-off script.
