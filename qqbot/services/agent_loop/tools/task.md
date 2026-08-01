# 工具：`task`

## 功能

`task` 用于创建和维护跨 tick 持续存在的任务。该工具的
`execution_mode` 为 `inline`，调用会在当前 AgentLoop tick 内完成并生成对应的
任务事件。

## 参数

`action` 决定参数分支：

| action | 必填参数 | 可选参数 | 说明 |
| --- | --- | --- | --- |
| `create` | `description` | `related_tools`、`parent_task_id`、`task_ref` | 创建 pending 任务 |
| `note` | `task_id`、`note` | 无 | 追加进度记录，不改变任务状态 |
| `complete` | `task_id` | `result_summary` | 将任务状态改为 done |
| `fail` | `task_id`、`reason` | 无 | 将任务状态改为 failed |

字段约束：

- `description`、`note` 最长 200 个字符，且不能为空。
- `result_summary`、`reason` 最长 1000 个字符。
- `related_tools` 为非空工具名组成的数组。
- `parent_task_id` 为可选的父任务 ID。
- `task_ref` 是当前 actions 列表内的临时别名。

## 同一 actions 列表内引用

`create` 分支可在 `arguments.task_ref` 中定义别名。后续 `call_tool` 动作可在
动作顶层填写同一 `task_ref`，调度器会将其解析为刚创建任务的真实 `task_id`。

`arguments.task_ref` 用于定义别名；后续动作顶层的 `task_ref` 用于关联任务。
`note`、`complete` 和 `fail` 的目标任务统一通过 `arguments.task_id` 指定。
触发事件通过 `call_tool.triggered_by_event_id` 传入，不属于本工具的
`arguments`。

## 返回

- `create`：`{"action":"create","task_id":"...","task_ref":"...","state":"pending"}`
- `note`：`{"action":"note","task_id":"...","state":"unchanged"}`
- `complete`：`{"action":"complete","task_id":"...","state":"done"}`
- `fail`：`{"action":"fail","task_id":"...","state":"failed"}`

四个分支都会产生普通工具调用终态；对应的任务事件随该内联调用一并持久化。
