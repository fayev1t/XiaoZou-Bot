Use `task` only for work that must remain visible across ticks. A quick reply or a
single lightweight tool call does not need a task.

The tool has four branches:

- `action="create"`: requires `description`; optionally accepts `related_tools`,
  `parent_task_id`, and `task_ref`. The result contains the real `task_id`, the same
  `task_ref`, and `state="pending"`.
- `action="note"`: requires `task_id` and a concise `note`. It records durable
  cross-tick progress without changing task state.
- `action="complete"`: requires `task_id`; `result_summary` is optional. Use it as
  soon as the Planner-side objective is finished. If the objective was to authorize a
  reply, successful completion of the `reply` tool is enough; do not wait for
  `runtime.reply_flushed` merely to close the planning task.
- `action="fail"`: requires `task_id` and `reason`. Use it when the objective cannot
  be completed or is deliberately abandoned.

`task` executes inline during the current AgentLoop tick, but it is still a normal
tool call with `agent.tool_called`, terminal result, batch completion, and the usual
next-tick wake.

For create-and-use in one actions list:

1. Call `task` with `arguments.task_ref="T1"` on the create branch.
2. A later `call_tool` action may set its top-level `task_ref="T1"`; the dispatcher
   resolves it to the newly returned `task_id`.

The two locations mean different things: `arguments.task_ref` defines the alias;
top-level `call_tool.task_ref` attaches that later tool call to the created task. Do
not put top-level `task_id` or `task_ref` on the `task` tool call itself—the target of
note/complete/fail belongs in `arguments.task_id`.

The event that caused a create belongs in the normal top-level
`call_tool.triggered_by_event_id`; do not duplicate it inside `arguments`.
