# 工具：`task`

## 功能

`task` 用于创建和维护跨 tick 持续存在的任务。它是 effect 程序函数，调用会在
当前 AgentLoop tick 内完成并生成对应的任务事件。

## 参数

`action` 决定参数分支：

| action | 必填参数 | 可选参数 | 说明 |
| --- | --- | --- | --- |
| `create` | `description` | `related_tools`、`parent_task_id` | 创建 pending 任务 |
| `note` | `task_id`、`note` | 无 | 追加进度记录，不改变任务状态 |
| `complete` | `task_id` | `result_summary` | 将任务状态改为 done |
| `fail` | `task_id`、`reason` | 无 | 将任务状态改为 failed |

字段约束：

- `description`、`note` 最长 200 个字符，且不能为空。
- `result_summary`、`reason` 最长 1000 个字符。
- `related_tools` 为非空工具名组成的数组。
- `parent_task_id` 为可选的父任务 ID。
## 同一程序内引用

`create` 的返回值直接存进普通变量，后续调用读取它的 `task_id`：

```python
t = task(action="create", description="整理资料")
wait(seconds=60, note="稍后继续整理", task_id=t.task_id)
```

`note`、`complete` 和 `fail` 的目标任务统一通过本函数的 `task_id` 参数指定。
effect 的系统关联参数 `triggered_by_event_id=` 由 Program API 统一提供。

## 返回

- `create`：`{"action":"create","task_id":"...","state":"pending"}`
- `note`：`{"action":"note","task_id":"...","state":"unchanged"}`
- `complete`：`{"action":"complete","task_id":"...","state":"done"}`
- `fail`：`{"action":"fail","task_id":"...","state":"failed"}`

四个分支都会产生普通工具调用终态；对应的任务事件与 terminal 一并持久化。
