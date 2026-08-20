# 五层记忆的最小落地存储方案

> 角色：Planner
>
> 更新日期：2026-08-20
>
> 状态：PROPOSED
>
> 适用范围：单用户、本地智能体、PostgreSQL 主存储、向量数据库检索、Neo4j 图增强

## 目标与非目标

### 目标

在保留五层逻辑记忆的前提下，把第一版物理存储收敛到可维护的最小集合：

1. 短期窗口直接复用当前 Session 和 Message，不重复建设窗口表、上下文表和召回表。
2. 用户偏好使用单用户键值表，支持显式覆盖和进程重启后读取。
3. 长期记忆使用单主表，只保留来源、状态、重要性、去重哈希和向量索引状态。
4. Task 记忆沿用现有 Task、ToolCall、Approval 事实，只增加一个可选的步骤表。
5. 向量数据库只承担相似检索，不成为记忆事实源。
6. Neo4j 只承担实体关系和多跳关系召回，不在 PostgreSQL 再复制一套图数据库表。

### 非目标

- 当前不引入 `users`、`projects`、租户、权限域或账户体系。
- 当前不引入 `context_builds`、`context_items`、`retrieval_runs`、`retrieval_candidates`。
- 当前不引入记忆版本表、证据表、反馈表、任务产物表或通用事件溯源表。
- 当前不做长期记忆自动过期 Worker、复杂冲突仲裁和自动合并。
- 当前不把所有 Session 消息、工具输出和任务审计复制到向量库或 Neo4j。

## 总体架构边界

```text
PostgreSQL（唯一权威事实源）
├─ sessions / messages       短期窗口记忆
├─ preferences               用户偏好
├─ memories                  长期记忆
├─ tasks / task_steps        Task 记忆
├─ tool_calls / approvals    执行事实与审批审计
└─ task_commits              代码版本关联

向量数据库（可重建索引）
└─ user_memory               偏好和长期记忆的相似检索

Neo4j（可重建图投影）
└─ Entity / Memory 节点及关系

ContextBuilder（代码职责，不建表）
└─ 当前窗口 + 直接偏好 + 向量召回 + 图召回 → Prompt
```

PostgreSQL 提交成功后，向量库和 Neo4j 暂时不可用不能让普通对话或任务执行失败；后续再增加
重试机制，不提前建设完整消息队列和事件平台。

## 一、短期窗口记忆

继续使用现有 `sessions` 和 `messages` 表，不新增 `short_term_memory`、`session_summaries`
或 `conversation_windows` 表。当前窗口由活动叶子向根回溯得到，并在代码中按消息数量或
Token 预算裁剪。`messages` 只保存用户可见的 `user`/`assistant` 内容，不保存 system、tool、
隐藏推理和完整审计输出。其他 Session 和兄弟分支默认不进入当前窗口。

当前表核心字段：

```text
sessions: session_id, title, active_leaf_id, created_at, updated_at, last_message_at
messages: message_id, session_id, parent_message_id, role, content, task_id,
          execution_status, created_at
```

第一版只实现“最近 N 条消息 + 当前分支约束”。真实会话超出预算后，再评估是否在 `sessions`
增加摘要字段，不预先创建独立摘要体系。

## 二、用户偏好记忆

单用户不需要 `users`、`scope_type`、`scope_id` 或偏好事件历史。偏好表只保存当前生效值，
修改时直接覆盖旧值。

### 最小表：`preferences`

```sql
CREATE TABLE preferences (
    preference_key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'user',
    updated_at TIMESTAMPTZ NOT NULL
);
```

- `preference_key`：例如 `review_mode`、`language`、`answer_style`。
- `value_json`：统一使用 JSON，兼容字符串、数字、布尔值和小对象。
- `source`：第一版只允许 `user` 和 `system`；模型不能静默覆盖用户偏好。
- `updated_at`：用于诊断和最新值判断。

读取失败或 JSON 损坏时使用安全默认值；密钥、Token、Cookie、密码和任务正文不得写入偏好表。
用户明确设置的值优先级最高。

## 三、长期记忆

第一版只需要“记忆内容 + 最小治理字段 + 来源引用”，不建设 revision、evidence、feedback、
conflict 和 archive 表。

### 最小表：`memories`

```sql
CREATE TABLE memories (
    memory_id TEXT PRIMARY KEY,
    memory_type TEXT NOT NULL,
    content TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_ref TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    importance REAL NOT NULL DEFAULT 0.5,
    content_hash TEXT NOT NULL,
    embedding_status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
```

- `memory_type`：第一版使用 `fact`、`project`、`lesson`；偏好单独放 `preferences`。
- `source_type`：`user`、`conversation`、`task`、`system`。
- `source_ref`：来源的 `message_id` 或 `task_id`，没有来源时为空但必须说明原因。
- `status`：`active`、`disabled`；不引入复杂生命周期。
- `importance`：召回排序的稳定权重。
- `content_hash`：用于去重和判断向量是否需要重建。
- `embedding_status`：`pending`、`ready`、`failed`，用于简单索引补偿。

模型推断出的内容不应无条件写入长期记忆，第一版只保存用户明确要求记住或由确定规则提取的
内容。更新记忆时直接更新原记录，同时更新
`content_hash` 和 `updated_at`。记忆正文不得包含完整密钥、Cookie、密码或敏感文件原文。

## 四、Task 记忆

当前已有 `tasks`、`tool_calls`、`approvals`、`task_commits`，它们继续保存执行事实，不另造
一套任务记忆表。

### 可选最小表：`task_steps`

只有当任务需要展示计划步骤、恢复中断任务或查询“进行到哪一步”时才新增：

```sql
CREATE TABLE task_steps (
    step_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    sequence_no INTEGER NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    result_summary TEXT,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    UNIQUE(task_id, sequence_no)
);
```

不新增 `task_step_attempts`、`task_artifacts`、`task_memory_links`。工具调用需要归属步骤时，
在现有 `tool_calls` 增加可空 `step_id` 即可。

## 五、图增强记忆

第一版只在 Neo4j 保存最小图，不在 PostgreSQL 建 `entities`、`entity_relations`、
`memory_entities` 的复制表：

```text
(:Entity { entity_id, name, entity_type })
(:Memory { memory_id, memory_type, status })
(:Memory)-[:ABOUT]->(:Entity)
(:Entity)-[:RELATED_TO {
    predicate,
    confidence,
    source_memory_id
}]->(:Entity)
```

Neo4j 中的 `memory_id` 必须对应 PostgreSQL 的 `memories.memory_id`。图中不保存长期记忆的
完整正文；召回得到 ID 后回 PostgreSQL 读取内容。Neo4j 是可重建的召回投影，不是唯一事实源。

第一版只做一跳或两跳关系扩展。Neo4j 更新失败时普通任务仍可完成，图召回安全降级。

## 六、向量数据库设计

只建立一个集合，例如：

```text
collection: user_memory
point_id:   memory_id 或 pref:<preference_key>
```

Payload 只保存过滤和定位字段：

```json
{
  "source_type": "preference" | "memory",
  "source_id": "...",
  "memory_type": "fact" | "project" | "lesson",
  "status": "active",
  "content_hash": "...",
  "updated_at": "..."
}
```

向量库可以保存用于检索的文本副本，但 PostgreSQL 仍是正文权威来源。应用拿到相似 ID 后必须
回表读取，避免向量库旧文本直接进入上下文。

### 召回流程

```text
用户请求
→ 生成查询向量
→ user_memory 集合 Top-K 检索
→ 过滤 active 和允许类型
→ 按 source_id 回 PostgreSQL
→ 去重、限制数量、拼接 Prompt
```

精确偏好查询优先直接查表，例如“当前审查模式”直接读取 `preferences.review_mode`，不依赖
相似度。向量检索主要处理模糊语义，例如“以前遇到过什么类似问题”。偏好和长期记忆共用一个
集合，通过 `source_type` 过滤；短期窗口不默认全部向量化。

建议默认 Top-K 为 5～10，最终进入 Prompt 的偏好和长期记忆合计不超过 3～5 条；内容更新后
通过 `content_hash` 判断是否需要重新生成向量。

## 七、最终第一版表清单

### 直接复用当前工程

```text
sessions, messages, tasks, tool_calls, approvals, task_commits
```

### 本轮新增

```text
preferences, memories
```

### 按真实需求再新增

```text
task_steps
```

### 明确暂不新增

```text
users / projects
session_summaries
memory_revisions / memory_evidence / memory_feedback
task_step_attempts / task_artifacts / task_memory_links
context_builds / context_items
retrieval_runs / retrieval_candidates
graph_outbox
```

## 验收与交接

实现前必须验证：

1. 连续对话只依赖 `sessions/messages`，没有重复短期记忆表。
2. 偏好可按键读取、覆盖和重启后恢复，损坏时使用安全默认值。
3. 长期记忆可创建、更新、禁用、去重，并能按来源回溯到 Message 或 Task。
4. Task 步骤可选；没有步骤需求时，现有 `tasks/tool_calls` 仍能完整记录执行事实。
5. 向量库只保存可重建索引，检索 ID 后始终回 PostgreSQL 读取最新正文。
6. Neo4j 暂时不可用不阻断普通任务，图召回可以安全降级。
7. 精确偏好查询不依赖向量相似度，模糊问题才使用向量召回。
8. 记忆、偏好、向量 Payload 和图属性不包含密钥、Token、Cookie、密码或完整敏感正文。

## 反锚定检查

- 本方案锁定最小数据职责和安全语义，不锁定 PostgreSQL、Neo4j 或向量数据库的具体供应商。
- 不强制 `task_steps` 必须立即实施；只有真实任务规划或恢复需求出现时才启用。
- 不强制异步消息队列、Outbox 或复杂迁移框架；当索引可靠性成为问题时再单独规划。
- 如果后续需要多用户、审计版本、冲突治理或复杂图同步，应新增独立规划，不回填本最小方案。
