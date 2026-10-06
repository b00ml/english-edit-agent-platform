# Agent 应用开发面试：英语教研 AI 内容生成平台 2.0 深度拷打与踩坑复盘

> 适用岗位：Agent 应用开发、AI 应用后端、LLM 应用工程师校招/初级社招。
> 本文不是项目宣传稿，而是面试时的证据化答题稿。答案必须以当前代码为准；设计文档中“计划实现”的内容不能冒充已经上线或已经压测验证。

## 0. 证据边界与使用规则

### 0.1 权威顺序

1. 当前源码和测试结果：`backend/app/`、`backend/tests/`、`deploy/`、`backend/scripts/`。
2. 项目约束：`AGENTS.md`、`CLAUDE.md`、`.ai/rules/`。
3. 设计与变更记录：`docs/优化技术设计3.0.md`、`docs/优化记录.md`、`docs/AI内容生成平台2.0-技术架构设计.md`、`docs/tasks.md`。

当文档和代码冲突时，以代码为准，并在面试中主动说明“文档规划”和“代码现状”的差异。

### 0.2 当前可直接说的事实

- 工作流有 `generate -> validate -> qc -> revise/store/reject/submit_review -> human_review` 八类节点，定义在 `backend/app/workflow/graph.py`。
- 默认 checkpointer 是 `PostgresSaver`；初始化失败可以降级为 `MemorySaver`，生产/预发布 readiness 默认会阻断该降级。跨连接 checkpoint 和 outbox dead replay 有 `backend/scripts/recovery_drill.py` 演练证据。
- Celery 使用 `acks_late`、worker lost reject 和瞬态基础设施异常重试；批量任务拆为独立 `GenerationTaskItem`，代码在 `backend/app/worker/tasks.py`。
- 结构化输出是 JSON + Pydantic v2/JSON Schema 二次校验 + 字段错误回注重试，默认最多三次，代码在 `backend/app/engine/structured_output.py`。
- 质量链路有独立 Judge 配置、多轮采样平均、Cohen's kappa 计算和人工驳回驱动的权重校准机制，但不能编造真实线上 kappa 或人工驳回率。
- 当前非集成测试命令为：

  ```powershell
  cd backend
  $env:PYTHONPATH='.'
  pytest -q -o addopts="" -m "not integration"
  ```

  本轮结果：`240 passed, 5 deselected, 6 warnings`。没有重新执行 coverage，因此不能声称当前仍然达到 80%。

### 0.3 当前不能说的话

- 不能说实现了 exactly-once。Celery 重投、数据库提交、Outbox relay 之间仍有跨系统窗口。
- 不能说“真实 LLM worker 被 kill 后恢复已验证”。现有恢复脚本验证的是 synthetic checkpoint 和 outbox 事件。
- 不能说已经完成完整多租户隔离。`GenerationService.get_task()` 查询路径、部分知识库路径和基础 repository 仍需统一 tenant scope。
- 不能说 `request_hash` 已经提供并发唯一保证。当前模型字段是普通索引，repository 也只过滤 `pending/running`。
- 不能把 Pydantic 说成语义正确性保证；跨字段逻辑、事实正确性和教学质量仍由业务校验、Judge 和人工终审负责。

### 0.4 项目事实卡（面试开场前先把口径说清）

| 项目 | 当前可核实口径 |
|---|---|
| 核心定位 | 面向英语教研的可编排、可评估 AI 内容生产线：选题 → 生成 → 校验 → 质检 → 改版 → 人审 → 入库/发布 |
| 主要语言 | Python 3.12（FastAPI、LangGraph、Celery）+ TypeScript（React/Vite）+ YAML/`.st`/Markdown 配置 |
| 仓库 Stars | 未验证：当前工作区没有可用的公开远程仓库元数据，不应在面试中编造数字 |
| Issue 证据 | 未验证：本地仓库未提供 issue 数据，远程地址也不可用；本文只引用源码、测试、文档和本地提交历史 |
| 维护状态 | 本地提交历史显示最近提交为 `2026-09-06`，包含 CI、部署和工程文档更新；只能说明近期有提交，不能等同于公开社区活跃度 |
| 当前测试口径 | 非集成测试 `240 passed, 5 deselected`；本轮未重新执行 coverage |
| 公开生产规模 | 未验证：没有流量、SLA、成本或线上质量数据，回答时必须使用“本地演练/单测验证”而非“生产证明” |

## 1. 项目 90 秒介绍

### 推荐回答

这是一个英语习题 AI 内容生产平台，不是单次聊天接口。它把“选题、生成、结构校验、自动质检、改版、人工终审、入库和发布”拆成可恢复的 Agent 工作流。题型通过 YAML 模板、独立 prompt 和 `SKILL.md` 接入，运行时由 LangGraph 根据校验结果、质量分和人工裁决做条件路由；FastAPI 提供任务和审核 API，Celery/Redis 承担异步执行，PostgreSQL 保存业务状态、TraceLog 和 checkpoint，pgvector 支持知识库检索。

我解决的核心问题有三个：第一，模型输出不稳定，使用 Pydantic 二次校验和字段级错误回注；第二，自动质检没有闭环，引入独立 Judge、人工一致性指标和驳回驱动的权重校准；第三，长任务和人工卡点不可恢复，使用 `PostgresSaver`、`interrupt`/`Command(resume)`、item 级幂等和 Celery 重试。

但当前仍有明确边界：任务创建和每个 item 的投递还没有完全收敛到同一 Outbox 语义，`request_hash` 未升级为 active partial unique index，多租户 scope 也不是所有 service/repository 的强制约束。因此我会把它描述为“具备生产化治理骨架的个人项目”，而不是已经承担生产流量的分布式平台。

### 面试官会抓的漏洞

- 你说“新增题型不改代码”，请指出运行时加载和校验位置，而不是只说 YAML。
- 你说“断点续跑”，是 LangGraph checkpoint 恢复，还是 Celery 任务重试？两者职责不同。
- 你说“幂等”，到底是 API 去重、item 状态去重、checkpoint 续跑，还是消息投递去重？必须分别回答。

## 2. 架构总览与职责边界

```text
React/Vite
   |
FastAPI routes -> service/repository -> PostgreSQL
   |                    |              |
   |                    |              +-- generation/content/quality/trace/checkpoint
   |                    +-- model router / structured output / quality / RAG
   |
Celery worker <- Redis broker <- Outbox relay
   |
LangGraph state graph
   generate -> validate -> qc -> revise -> store/reject
                         \-> submit_review -> interrupt -> Command(resume)
```

### 2.1 模块划分判断

当前目录划分基本能表达领域边界：`workflow` 负责图和状态，`engine` 负责模型调用、结构化输出、质检和 trace，`worker` 负责异步任务，`services/repositories` 负责业务和数据访问，`rag` 负责 embedding/retrieval。题型配置位于 `backend/templates/`，prompt 和 skill 独立存放。

主要架构风险不在目录名，而在边界执行不够强：部分路由仍直接接触 repository；tenant scope 没有变成统一的 `TenantContext`；任务创建仍保留旧实例直接 `send_task()` fallback；因此“分层存在”不等于“分层约束已经被编译器和数据库强制”。

### 2.2 相关知识点速查

| 知识点 | 面试中必须说清的核心含义 | 本项目落点 |
|---|---|---|
| Agent vs. workflow | Agent 的关键是状态、决策/工具调用和可恢复执行；调用一次 LLM 不构成 Agent | LangGraph 条件边、改版循环、人工 interrupt |
| 状态机/DAG | 状态机适合有限状态和回路，DAG 适合无环依赖；回路必须有熔断条件 | `after_validate`/`after_qc` + revision 上限 |
| Durable execution | 执行进度和业务状态持久化，进程重启后能从已知点恢复 | `PostgresSaver`，但降级 `MemorySaver` 只具进程内语义 |
| At-least-once | 消息可能重复但不应丢；`acks_late`/重试天然带来重复执行 | item 状态、thread id、唯一键和 Outbox 降低重复影响 |
| Idempotency | 同一业务请求重复执行，最终业务结果不增加；不能只靠“查一次再插入” | 当前 request hash 仍缺数据库唯一约束 |
| Transactional Outbox | 业务提交与待发送事件同库同事务，relay 负责异步发送/重试 | `TaskOutbox`、dead/replay；不解决 DB 与 Redis 原子性 |
| Retry taxonomy | 瞬态网络/依赖故障可重试；认证、schema 设计和业务拒绝不能无限重试 | Celery `autoretry_for` + 结构错误回注，均有上限 |
| JSON Schema/Pydantic | 保证字段形状、类型和显式约束；不保证事实、跨字段语义和教学质量 | `structured_output.py` 二次校验 |
| LLM-as-Judge | Judge 是评估器，不是真值；需 rubric、人工样本和偏差监控 | 独立 judge model、多轮均值、人工校准 |
| Cohen's kappa | 在 accuracy 之外扣除随机一致性；类别不平衡和小样本会影响解释 | 二值 auto/manual 口径，`n < 30` 不做验收结论 |
| RAG | 检索增强生成，召回上下文不是模型参数；chunk、top-k、metadata 影响质量 | pgvector + embedding + tenant filter |
| 向量维度 | 同一向量列必须固定维度；换模型要版本化重建，不能混写 | `Vector(1024)` 是迁移风险点 |
| Tenant isolation | 每一次读写都要有不可伪造的租户上下文；角色权限不能替代租户边界 | 当前部分路径已过滤，尚未统一 `TenantContext` |
| RBAC/ABAC | RBAC 控角色，ABAC 还要看租户、资源状态和操作者身份 | JWT + 角色矩阵 + 发布状态前置条件 |
| Liveness/readiness | liveness 只说明进程活着；readiness 才决定能否接流量 | DB/Redis/migration/checkpointer 探针 |
| Observability | 日志回答“发生了什么”，trace 关联调用链，metrics 聚合趋势 | `trace_id`、TraceLog、成本/延迟/token |
| Config/version snapshot | 质量问题必须能回到当时的 prompt、skill、模板和模型 | SHA-256 hash + task/quality snapshot |
| Contract governance | Pydantic/OpenAPI 可约束接口形状；兼容性仍需版本和 CI 检查 | schema 已有，OpenAPI breaking-change 门禁待补 |

## 3. 第一轮：项目背景、Agent 定义与技术选型

### Q1：为什么这是 Agent 应用，而不是普通的 LLM 流水线？

**参考答案：**

普通流水线通常是固定顺序的函数链；本项目存在持久状态、条件路由、循环改版、人工中断和恢复。`after_validate()` 会在结构不合法时回到 `generate`，`after_qc()` 会根据质量分选择 `store`、`submit_review`、`revise` 或 `reject`；人工节点通过 `interrupt()` 暂停，并由 API 用 `Command(resume)` 恢复。它的核心不是“调用了 LLM”，而是模型作为工作流中的决策和执行节点，状态可以跨调用、跨进程继续推进。

**面试官追问：** 如果去掉 LLM，只保留状态机，它还是 Agent 吗？

**防守边界：** “Agent”不是营销标签。这里使用的是有持久状态和工具/模型节点的有限状态工作流，不是开放式自主规划 Agent。任务路径目前由代码定义，不是通用 DAG 编辑器。

**代码证据：** `backend/app/workflow/graph.py`：`after_validate`、`after_qc`、`human_review_node`、`resume_human_review`。

### Q2：为什么选 LangGraph，而不是 Celery chain、Temporal 或自己写状态机？

**参考答案：**

Celery 适合消息投递和后台执行，但不擅长表达“图状态、条件边、人工 interrupt、基于 checkpoint 的 resume”。Temporal 更适合大规模、强工作流持久化，但引入服务端和 SDK 约束，对个人项目的部署成本更高。LangGraph 直接提供图节点、条件路由、`interrupt` 和 checkpointer，适合把 LLM 状态、人工裁决和改版循环放在一张可测试的图里。Celery 在本项目中只负责把执行任务可靠地交给 worker，二者是编排和投递的分工，不是互相替代。

**面试官追问：** LangGraph 的 checkpoint 和 Celery 的 ack 是否重复？

**参考答案：** 不重复。ack 解决消息是否从 broker 删除；checkpoint 解决图执行到了哪一个状态。worker 在节点中途崩溃时，ack 让任务重投，checkpoint 让重投后的图跳过已完成节点。两者缺一不可。

**防守边界：** 没有做 Temporal 级别的全量故障语义比较，也没有对 LangGraph checkpoint 做高并发压测。

**代码证据：** `backend/app/worker/celery_app.py` 的 `task_acks_late/task_reject_on_worker_lost`；`backend/app/workflow/graph.py` 的 `_get_checkpointer`。

### Q3：八个节点分别做什么？为什么不把逻辑写成一个大函数？

**参考答案：**

`generate` 调模型生成题目；`validate` 做 Pydantic/业务结构校验；`qc` 调 Judge 并计算质量分；`revise` 将质检反馈回注模型进行改版；`store` 持久化通过内容；`reject` 落库失败终态；`submit_review` 创建待审记录并保存 thread 信息；`human_review` 中断等待人工裁决。拆节点让每个节点有明确输入输出和可观测边界，checkpoint 可以按节点恢复，测试也能针对路由和副作用隔离。

**面试官追问：** 为什么 `submit_review` 和 `human_review` 必须是两个节点？

**参考答案：** `submit_review` 先将内容和审核任务写入数据库，确保前端能看到待审对象；`human_review` 再调用 `interrupt`。如果在写库前中断，审核对象可能只存在于 checkpoint，查询 API 无法发现；如果把持久化和 interrupt 混在一个可重复执行函数中，resume 或重投时容易重复写通知和审核记录。当前 `human_review_node` 在 interrupt 前无副作用，恢复后才写人工质检记录。

**代码证据：** `backend/app/workflow/graph.py`：`submit_review_node`、`human_review_node`。

### Q4：改版熔断怎么保证不会无限调用模型？

**参考答案：** `after_qc()` 检查当前 `revision_count`/最大改版次数。质量达标进入 `store`，灰区进入人工审核，低分且未到上限进入 `revise`，达到上限进入 `reject`。每次改版都增加计数，失败最终进入可审计的 rejected 状态，不会在图中无限循环。

**面试官追问：** 改版时如何证明质检反馈真正进入 prompt？

**参考答案：** 不能只看状态字段。需要在 `revise` 的模型调用 Trace 中记录反馈摘要或 prompt hash，并用回归测试断言第二次调用的 user prompt 包含上一次 Judge 的具体反馈。历史上曾存在每次改版两次生成且反馈没有真正回注的问题，已通过状态机测试暴露并修复。

**代码证据：** `backend/app/workflow/graph.py` 的 `after_qc`、`revise_node`；`docs/优化记录.md` 中改版双生成缺陷记录。

### Q5：项目中 Agent 的“工具”是什么？如何扩展新题型？

**参考答案：** 模型调用、知识检索、结构校验、Judge 和持久化都可以视为受代码约束的工具能力。新题型的接入入口是 YAML 模板、独立 `.st` prompt 和 `skills/<id>/SKILL.md`；`template_loader.py`、`prompt_loader.py`、`skill_registry.py` 负责加载与注册，图节点只消费统一的运行配置和 schema。新增题型不应复制一套 Python 节点。

**防守边界：** “不改代码”只对模板/schema/prompt 兼容的题型成立；如果新题型需要完全不同的外部工具或持久化模型，仍需要扩展代码和迁移。

### Q6：项目最大的架构性风险是什么？

**参考答案：** 不是 LangGraph 本身，而是跨边界一致性：数据库事务、Outbox、Celery broker、LangGraph checkpoint 和租户权限分属不同组件，当前没有一个全局事务。尤其是任务创建后投递、每个 item 的 `.delay()`、request hash 去重和查询租户过滤仍存在缺口。面试中我会主动把项目定位为“可靠性骨架已搭建，分布式一致性尚未完全闭环”。

## 4. 第二轮：Checkpoint、interrupt、HITL 与幂等

### Q7：`interrupt()` 和 `Command(resume)` 如何配对？

**参考答案：** `human_review_node` 调用 `interrupt({content_id, qc_score, dimension_scores})`，LangGraph 将当前图状态和等待点写入 checkpointer；审核 API 根据持久化的 `thread_id` 调用 `resume_human_review()`，内部执行 `graph.invoke(Command(resume=decision), config={configurable: {thread_id}})`。恢复时 `interrupt()` 返回 decision，节点继续写人工质检、更新 `passed/rejected` 状态并发送必要通知。

**关键依赖：** `thread_id` 必须稳定，checkpointer 必须与恢复进程共享。worker 使用 `task_id:item_index` 作为 item 粒度 thread id；使用 MemorySaver 时只能在同一个进程内恢复。

**面试官追问：** 如果两个审核员同时 resume 怎么办？

**参考答案：** 当前需要依靠业务层状态条件和数据库更新结果保证只有一个有效裁决；代码没有声明一个完整的审计级“抢占审核锁”协议。进一步优化应增加 `review_version`/乐观锁、唯一人工决策约束和重复 resume 的幂等响应。

**代码证据：** `backend/app/workflow/graph.py`：`human_review_node`、`resume_human_review`；`backend/app/worker/tasks.py` 的 thread id 构造。

### Q8：PostgresSaver 初始化失败降级 MemorySaver 的后果是什么？

**参考答案：** MemorySaver 只存在当前进程。worker 重启、换进程或扩容后，之前的 interrupt 状态不可见，人工审核无法从另一进程恢复；消息重投也可能重新走模型节点。它只适合开发/测试或明确接受 degraded 语义的环境。项目的 `/api/health/ready` 在 staging/production 默认阻断这种降级，除非设置 `ALLOW_MEMORY_CHECKPOINTER`。

**面试官追问：** 为什么不直接让服务启动失败？

**参考答案：** 开发环境需要快速启动，测试也需要隔离；因此代码保留显式可配置的降级，但把是否接流量交给 readiness，而不是静默把生产变成内存语义。生产配置还应将 `CHECKPOINTER_BACKEND=postgres` 固定并配告警。

**代码证据：** `backend/app/workflow/graph.py`：`_get_checkpointer`；`backend/app/health.py`；`backend/app/config.py`。

### Q9：`acks_late` 为什么必然要求幂等？

**参考答案：** `acks_late=True` 表示任务执行完成后才 ack。worker 在外部副作用已经发生、但 ack 还没发出去时崩溃，broker 会重新投递同一任务。若生成、写库、发通知都不可重复，结果就是重复题目或重复审核记录。因此本项目需要 item 状态检查、稳定 thread id、数据库唯一约束和副作用的幂等 key；acks_late 不是“自动不重复”，它只是把“至少一次”暴露出来。

**面试官追问：** checkpoint 能保证所有副作用 exactly-once 吗？

**参考答案：** 不能。checkpoint 只描述图状态；如果节点先调用模型或发消息，再在 checkpoint 提交前崩溃，重跑仍可能再次调用。需要把业务落库放入幂等事务，外部发送走 Outbox，并给下游使用稳定 event/task id。

### Q10：Outbox 能解决什么，不能解决什么？

**参考答案：** Outbox 把“业务任务创建”和“待投递事件”放进同一个数据库事务，relay 再异步发送到 Celery；即使 broker 短时不可用，也可以重试、退避、转 dead 并 replay。`TaskOutbox.event_id` 作为稳定事件 id，并被用作 Celery `task_id`，可以降低重复投递造成的影响。

它不能把 PostgreSQL 和 Redis 变成一个原子事务，也不能保证 worker 已经执行成功；relay 在发送成功但状态更新失败时仍可能重复发送。因此要说“至少一次 + 幂等消费/重放”，不能说 exactly-once。

**代码证据：** `backend/app/outbox.py`：`relay_pending`、`replay_dead`；`backend/scripts/replay_outbox.py`、`backend/scripts/recovery_drill.py`。

### Q11：当前任务投递为什么还不能称为完整 Outbox？

**参考答案：** `GenerationService.create_task()` 在提交后仍保留兼容旧实例的直接 `send_task()` fallback；`dispatch_generation_items()` 创建 item 后又通过 `generate_single_item.delay()` 循环投递。也就是说，任务级 outbox 和 item 级投递并未完全统一，异常窗口仍存在。下一步应让 item dispatch 也生成 outbox 事件，relay 负责唯一出口，移除直接发送 fallback，并对 active request hash 建 PostgreSQL 部分唯一索引。

### Q12：为什么不能声称 request_hash 已经防止重复任务？

**参考答案：** `GenerationTask.request_hash` 在 `backend/app/models.py` 是普通 `index=True`；`TaskRepository.get_by_request_hash()` 只过滤 `pending/running`，不能覆盖 dispatched/awaiting_review，也没有数据库唯一约束。并发请求可能同时查不到记录并各自插入。正确修复是先清理历史重复数据，再建立 `(tenant_id, request_hash) WHERE status IN (...)` 的 partial unique index，插入冲突时捕获 `IntegrityError` 返回已有任务。

## 5. 第三轮：结构化输出、重试与模型路由

### Q13：为什么不用 Outlines 或 constrained decoding？

**参考答案：** 当前云端 OpenAI 兼容 API 不开放 logits/语法约束能力，Outlines 无法控制服务端采样；因此采用“模型输出 JSON + 服务端 Pydantic v2/JSON Schema 二次校验 + 字段级错误回注重试”。这保证了进入业务层的数据形状可验证，并且 Trace 中能统计 attempt、success、失败原因和成本。

这不是 logits 级约束解码的完全等效替代。模型仍可能产生语义错误、跨字段矛盾或事实错误，必须继续经过业务校验和 Judge。

**代码证据：** `backend/app/engine/structured_output.py`。

### Q14：结构化输出如何处理顶层数组和 `{"questions": [...]}` 包装？

**参考答案：** 不能直接对预期对象调用 `.keys()`。代码先做输入形状归一化：顶层数组按 schema 需要包装或逐项处理，常见的单键列表包装会提取内部列表，再交给动态 Pydantic 模型和 JSON Schema 校验。校验失败生成字段路径、错误类型和简短上下文，回注到下一轮请求；达到最大重试次数后返回明确失败，而不是把错误对象继续写入内容库。

**历史踩坑：** 早期顶层数组导致 `.keys()` 崩溃；后续 `{"questions": [...]}` 被当作错误重复重试。修复必须配回归测试，不能只在 prompt 中要求“请返回对象”。

### Q15：如何区分 retryable 和 non-retryable？

**参考答案：** 结构不符合 schema、字段缺失、JSON 解析失败属于可通过错误回注修复的模型输出错误，但必须受最大次数和预算限制；认证失败、模型不存在、权限拒绝、不可恢复的配置错误不应无脑重试。Celery 侧只对 Redis timeout、数据库 `OperationalError` 等瞬态基础设施故障自动重试；业务生成失败保持失败或部分成功语义。

**面试官追问：** 如果模型连续返回坏 JSON，怎样避免成本爆炸？

**参考答案：** 限制 `max_attempts`，记录每次 token/cost，按错误类型统计，必要时切换备用模型或直接标记失败；不能把无限重试当作可靠性。

### Q16：模型路由如何做主备、cooldown 和预算？

**参考答案：** 路由配置从 `config.py` 和模型档案读取，模板可以指定 `run_config.judge_model`；全局 Judge 模型再回退到 `JUDGE_MODEL_NAME`、`LLM_MODEL_NAME`。路由器应对超时、5xx、限流和预算耗尽分类，主模型失败后进入 cooldown，选择备用或默认降级，并将每次选择和失败原因写入 Trace。模型名不能硬编码在业务函数里。

**防守边界：** 当前代码已经有模型档案引用校验、fallback/cooldown 基础机制，但不能把它描述成带实时容量预测的调度系统；预算约束的真实线上效果也没有压测数据。

**代码证据：** `backend/app/engine/router.py`、`backend/app/model_governance.py`、`backend/app/config.py`。

## 6. 第四轮：质量闭环、Judge 与统计口径

### Q17：为什么需要独立 Judge 模型？如何避免自偏好？

**参考答案：** 生成模型既写题又评价自己会产生自偏好，容易把自身风格误当成质量。质量链路支持单独 `judge_model`，把 rubric、模板和题目作为评估输入，生成模型和 Judge 模型在配置上解耦；Judge 的每轮调用写 Trace，可核算成本和延迟。人工终审作为外部真值来源，用于评估和校准。

**面试官追问：** 换成另一个模型就一定没有偏差吗？

**参考答案：** 不一定。独立模型只能降低同模型自偏好，不能证明客观正确。需要固定评测集、人工盲审、多模型交叉评估和按题型/模板分层分析。

### Q18：Cohen's kappa 表示什么？为什么要至少 30 个样本？

**参考答案：** kappa 是在准确率基础上扣除随机一致性的 chance-corrected agreement：`κ=(Po-Pe)/(1-Pe)`。本项目把 Judge `auto_score >= threshold` 和人工 `manual_score > 0` 都二值化，输出混淆矩阵、accuracy、precision、recall、F1 和 kappa。样本太少或类别极度不平衡时，kappa 方差很大，不能拿少量样本做验收数字，因此脚本对 `n < 30` 只提示“不建议解释”。

**防守边界：** 当前只实现总体验收级二值 kappa，不是维度级 kappa，也不能声称已有真实线上 kappa 数值。

**代码证据：** `backend/app/engine/agreement.py`、`backend/scripts/judge_agreement.py`。

### Q19：人工驳回如何反向校准权重？

**参考答案：** 从“自动高分但人工驳回”的 false positive 集合出发，按维度统计放水程度；对经常放水的维度降低权重，并设置权重下限，避免某维度被一次异常样本打成零。默认至少 20 个标注样本、至少 5 个 false positive 才执行校准；整体驳回率超过 5% 时收紧阈值。每次调整应保存旧权重、新权重、样本窗口和配置 hash，保证可审计。

**面试官追问：** 这种算法会不会把真实难题全部拒掉？

**参考答案：** 会有误伤风险，所以必须按题型、模板版本、租户和时间窗口分层，设置最小样本守卫、权重下限和人工抽样复核；不能把全局驳回率一个数字直接当作质量真值。

**代码证据：** `backend/app/calibration.py`。

### Q20：质量闭环的最大未完成项是什么？

**参考答案：** 机制已有，但真实数据闭环还不充分：没有在本轮验证中给出线上 kappa、人工驳回率下降或成本下降的实验结果；审核者身份、质量配置快照和分模板/租户/时间窗口统计必须确保每条质量记录都带齐，否则回溯时会把不同版本混在一起。

## 7. 第五轮：状态管理、事务与并发

### Q21：内容状态机如何防止 rejected 直接发布？

**参考答案：** `backend/app/domain/status.py` 集中定义迁移规则，`ContentService.publish_content()` 只允许 `passed -> published`，并记录 `published_by/published_at`。`rejected` 是终态，不能直接发布。状态迁移应由服务层统一执行，不能让路由直接改字符串。

**面试官追问：** 如果两个审核请求同时更新呢？

**参考答案：** 需要数据库条件更新或乐观锁，检查旧状态仍是 `awaiting_review`，成功行数为 1 才算裁决成功；当前实现有状态约束，但并发审核的完整锁定和审计协议仍应补强。

### Q22：批量任务为什么拆成 GenerationTaskItem？

**参考答案：** 批量任务中单题失败不应让所有题目重做。每个 item 有独立状态、`thread_id` 和尝试信息，聚合任务从 item 状态重新计算；已 `succeeded`、`awaiting_review` 或终态的 item 在重投时跳过。这样可以表达部分成功，也能把重试粒度从整批缩小到单题。

**代码证据：** `backend/app/worker/tasks.py`：item 状态更新、汇总逻辑和 `generate_single_item`。

### Q23：数据库事务和图状态如何保持一致？

**参考答案：** 图节点内的业务写库使用显式 SQLAlchemy session；checkpoint 是另一套持久化。不能假设两者原子一致。做法是：状态机节点尽量先完成幂等业务写入，再让图推进；为关键记录增加唯一键和状态条件；跨系统消息通过 Outbox；恢复时先读取业务终态，已完成 item 直接跳过。仍需承认节点副作用与 checkpoint 提交之间有窗口。

### Q24：项目的“并发安全”目前做到哪一步？

**参考答案：** 已有 item 级唯一 thread、状态聚合、重试和部分去重；但 `request_hash` 不是数据库唯一约束，知识/任务查询也不是所有路径都强制租户条件，两个审核员并发 resume 也需要乐观锁。当前是“关键路径有幂等保护，但没有全局并发协议”。

## 8. 第六轮：RAG、Embedding 与租户隔离

### Q25：RAG 链路如何工作？

**参考答案：** 文档解析后切 chunk，调用 embedding 服务生成向量，写入 PostgreSQL `pgvector`；查询时对问题做同模型 embedding，在 `retriever.py` 进行相似度检索并注入生成上下文。知识上下文和生成参数分开注入，避免把检索结果混成模型配置。embedding 调用记录 trace、token、成本和耗时。

**面试官追问：** chunk 大小和 top-k 怎么定？

**参考答案：** 不能拍脑袋。应通过题型评测集比较召回率、上下文长度、生成质量和成本，按文档结构保留标题/来源 metadata；top-k 需要与模型上下文窗口和重复率共同调参。

### Q26：`Vector(1024)` 有什么风险？换 embedding 模型怎么办？

**参考答案：** 数据库向量列维度必须和 embedding 模型输出一致。当前 `KnowledgeChunk.embedding` 固定为 `Vector(1024)`；更换到其他维度会导致写入失败或相似度不可比。迁移方案是新增版本化向量列/表，按新模型重建索引和数据，双写或灰度读取，验证召回后再切换，不能直接修改列定义并混写旧向量。

### Q27：当前多租户隔离是否真正闭环？

**参考答案：** 没有。`retriever.py` 已支持 `tenant_id` 过滤，viewer 内容查询有租户条件；但 `BaseRepository.get_by_id()` 不强制 tenant scope，`GenerationService.get_task()` 路由没有把 `current_user` 传入 service，`KnowledgeService.retrieve_knowledge()` 也不是所有路径都明确使用用户租户。`GenerateRequest` 仍保留客户端 `tenant_id` 字段，虽然创建时改用 `current_user.tenant_id`。这属于必须在面试中主动暴露的越权审查点。

**修复方案：** 认证依赖生成不可伪造的 `TenantContext`；所有 repository 查询默认接收 context 并自动拼接 tenant；跨租户 admin 必须走显式系统权限；增加不同租户同 ID、任务、知识和内容的负向测试。

## 9. 第七轮：API 契约、权限与安全

### Q28：API 契约如何保证稳定？

**参考答案：** FastAPI 路由使用 Pydantic schema 做输入输出校验，统一异常体系映射错误码；状态值集中在 domain status；配置和版本 hash 写入任务快照。契约应通过 OpenAPI 导出并在 CI 做 breaking-change 检查。当前代码已经有 schema 和统一错误，但还没有完整的 OpenAPI 兼容性门禁，因此不能说契约治理已经完成。

### Q29：viewer 能否读取草稿？

**参考答案：** 设计上 viewer 只能读取发布或授权范围内的数据，service 层必须把角色和 tenant scope 一起传入。当前 `ContentService` 对 viewer 有过滤，但 admin/editor 查询路径不统一使用 `TenantContext`，所以必须通过 API 负向测试验证“跨租户草稿不可见”，不能只看一个 endpoint。

### Q30：Agent 调外部工具时如何控制权限？

**参考答案：** API key、JWT、角色矩阵和审计由代码/基础设施强制，不依赖 prompt。外部工具调用应有 allowlist、超时、预算、参数 schema、敏感信息脱敏和 trace；不允许模型直接拼接任意 URL 或执行 shell。当前项目主要是云模型、embedding 和数据库工具，没有通用 shell sandbox，因此不能声称具备通用工具沙箱。

### Q31：生产配置做了哪些 fail-fast？

**参考答案：** `config.py` 禁止默认 JWT secret、要求最小长度，禁止默认管理员密码和空/占位 LLM、embedding key；Compose 对数据库密码、Langfuse secret 等使用 `${VAR:?}` 强制注入。健康检查分 live、ready 和依赖探针，生产下 checkpointer 降级会让 ready 失败。

**代码证据：** `backend/app/config.py`、`backend/app/health.py`、`deploy/docker-compose.yml`。

## 10. 第八轮：可观测性、版本治理与成本

### Q32：一次生成如何完整回放？

**参考答案：** 用 `trace_id` 贯穿模型、Judge、embedding 和任务日志；TraceLog 记录模型、prompt 版本、输入输出摘要、attempt/success、latency、token 和 cost。`versioning.py` 对模板、prompt、skill、model profile 做稳定 JSON 序列化和 SHA-256 hash，并把任务版本快照和质量配置快照保存下来。回放时按 `trace_id` 和 task version 找到当时的配置，而不是读取当前模板。

**防守边界：** 不能把所有原始 prompt/输出都无限期保存，需脱敏、大小限制和保留策略；当前要继续核对每一类调用是否都完成 tenant 和敏感字段治理。

### Q33：线上质量突然下降，你如何定位？

**参考答案：** 先按模板版本、模型档案、租户和时间窗口切分质量指标；检查结构化输出首过率、重试率、Judge 分布、人工驳回率、模型 fallback/cooldown、RAG 召回和成本。用 `trace_id` 抽样回放，确认是模型变更、prompt/skill hash 变化、知识库版本、解析失败还是状态路由错误。没有版本快照就无法把问题归因到具体配置，因此版本治理不是附属日志，而是质量定位前提。

### Q34：如何做成本控制？

**参考答案：** 每次模型和 embedding 调用记录 token、单价、耗时和模型名，任务级汇总按生成、校验、Judge、改版拆分；对最大重试次数、单任务预算和 fallback 模型设置硬限制。成本优化优先减少无效改版、重复调用和过大的 RAG 上下文，而不是只换便宜模型。

## 11. 第九轮：部署、迁移与恢复

### Q35：本地如何启动？生产启动有哪些依赖？

**参考答案：** Docker Compose 编排 PostgreSQL/pgvector、Redis、Langfuse、backend 和 worker；backend 启动命令先执行 `alembic upgrade head` 再启动 Uvicorn，服务依赖数据库/Redis healthcheck。生产需要显式注入 JWT、管理员、LLM、embedding 和 Langfuse secrets；不能使用默认值。

### Q36：Alembic 多 head 怎么处理？

**参考答案：** 迁移文件必须只有一个当前 head；新分支合并迁移时先用 `alembic heads` 检查，必要时创建 merge revision，再在干净数据库执行 `upgrade head`。历史上曾出现多 head 和重复索引问题，修复后用真实 PostgreSQL 验证从旧 revision 连续升级到最新 revision。

### Q37：备份恢复演练应证明什么？

**参考答案：** 不只是“备份文件存在”。要验证独立恢复库能完成迁移、业务表和关键状态可读、checkpoint 能被新连接恢复、dead outbox 能 replay/relay，最后清理演练数据。`backend/scripts/recovery_drill.py` 已对 PostgresSaver 跨连接 interrupt/resume 和 synthetic outbox dead replay 提供可重复脚本。

**防守边界：** 当前没有把真实 LLM worker kill/restart、真实云模型副作用和端到端生产流量恢复全部验证，因此不能扩大演练结论。

### Q38：Redis 宕机、worker 被 kill、数据库提交后 broker 失败分别怎么办？

**参考答案：** Redis 宕机：Celery 任务按瞬态错误重试，服务 readiness 应显示依赖失败；worker kill：`acks_late + reject_on_worker_lost` 让未 ack 任务重投，item 状态和 checkpoint 跳过已完成步骤；DB commit 后 broker 失败：Outbox 事件仍在 pending，relay 恢复后发送。若是直接 `.delay()` 的 item 投递，就存在丢投窗口，这正是当前未闭环点。

## 12. 第十轮：测试与工程质量

### Q39：240 个测试说明了什么？

**参考答案：** 本轮非集成测试是 240 条通过、5 条 integration 被排除。它说明 schema、路由、校准、任务状态、治理和部分恢复逻辑有回归保护；不等于系统在生产可靠，也不等于覆盖率仍是 80%+。集成测试需要真实 PostgreSQL/pgvector，覆盖率门槛在 CI 配置中，但本轮没有重新运行 coverage。

### Q40：你会给哪些模块补集成测试？

**参考答案：** 优先补四类：一是两进程 PostgresSaver interrupt/resume；二是数据库提交与 Outbox relay 之间的故障窗口；三是不同租户读取同 ID 资源的负向用例；四是 `request_hash` 并发插入和唯一冲突。再补真实 Redis worker kill/retry，但必须隔离云模型调用并使用可控 fake provider。

**代码证据：** `backend/tests/`；`backend/scripts/recovery_drill.py`；`.github/workflows/backend-ci.yml`。

## 13. 项目踩坑、根因、修复与后续优化

### 13.1 改版链路双生成，反馈没有进入 prompt

- **现象：** 每次改版产生两次 LLM 调用，质量反馈没有真正影响下一轮生成。
- **根因：** 状态机节点边界和 prompt 组装没有单一责任，测试只断言调用次数，没有断言反馈内容。
- **修复：** 将 `revise` 作为独立节点；把上一轮 Judge feedback 显式放入下一轮 user prompt；增加图路由测试和 prompt 内容断言。
- **继续优化：** Trace 保存 feedback hash、attempt 和 parent trace；对相同 feedback 做去重，防止无效改版。
- **证据：** `backend/app/workflow/graph.py`；`docs/优化记录.md` 改版缺陷记录。

### 13.2 顶层数组和嵌套包装导致结构校验崩溃/重复重试

- **现象：** 模型返回数组时 `.keys()` 抛异常；返回 `{"questions": [...]}` 时被误判为 schema 错误。
- **根因：** 直接假设模型输出形状，没有先归一化。
- **修复：** 归一化顶层 object/list 和单键列表包装，再进入 Pydantic/JSON Schema；错误摘要按字段路径回注。
- **继续优化：** 为每个题型记录输入形状分布和失败原因，超过阈值自动触发 prompt/schema 回归。
- **证据：** `backend/app/engine/structured_output.py`、`backend/tests/test_output_schema_validation.py`。

### 13.3 LangGraph 状态键未显式声明

- **现象：** 新增节点写入的 key 没有进入统一 state schema，导致恢复后字段丢失或路由读取默认值。
- **根因：** 把 Python dict 当作无约束状态，缺少 `TypedDict`/状态字段审查。
- **修复：** 集中声明 `GenState`，节点只返回增量字段；为路由、恢复和缺省值补测试。
- **继续优化：** 对状态字段做版本化，迁移旧 checkpoint 时显式填充默认值。
- **证据：** `backend/app/workflow/graph.py` 的状态定义和构图代码。

### 13.4 Alembic 多 head 和重复索引

- **现象：** 多条迁移分支同时存在，自动索引与显式索引重复。
- **根因：** 分支并行改表，没有在合并前检查 `alembic heads` 和 SQLAlchemy `index=True` 生成行为。
- **修复：** 增加 merge revision；移除重复索引声明；用真实 PostgreSQL 从旧 head 升级到最新 head。
- **继续优化：** CI 在 migration job 中执行 `alembic heads`、新鲜库 upgrade、备份库 upgrade 和 downgrade 风险检查。
- **证据：** `backend/alembic/versions/`、`docs/优化记录.md`、`backend/scripts/migration_precheck.py`。

### 13.5 Langfuse `latest` 标签造成主版本漂移

- **现象：** Compose 使用浮动镜像，v2 API/环境变量和 v3 行为不一致。
- **根因：** 依赖版本没有锁定，部署文件看似可用但重启后运行版本可能变化。
- **修复：** pin Langfuse v2 镜像，并在初始化脚本中创建独立数据库。
- **继续优化：** 所有基础设施镜像使用 digest 或明确 patch 版本，升级走演练和回滚方案。
- **证据：** `deploy/docker-compose.yml`、`deploy/postgres-init/01-create-langfuse-db.sql`。

### 13.6 前端类型错误直到 Docker 才暴露

- **现象：** 本地后端测试通过，前端构建时才发现 API 类型或 import 错误。
- **根因：** CI 没有在 PR 阶段执行 frontend typecheck/build。
- **修复：** 将 `tsc --noEmit` 和 Vite build 纳入 CI；API schema 变更同步更新类型。
- **继续优化：** 从 FastAPI OpenAPI 自动生成前端 client，减少手写 DTO 漂移。
- **证据：** `frontend/`、`.github/workflows/`。

### 13.7 TraceLog 字段逐次追加但缺少统一版本契约

- **现象：** task_id、template_id、model、cost 等字段在不同阶段追加，历史记录结构不一致。
- **根因：** Trace 没有事件 schema 版本和强制构造器。
- **修复：** 集中 trace helper，所有调用带 `trace_id`、task/template/tenant、attempt/success、latency/cost。
- **继续优化：** 增加 `trace_schema_version`、敏感字段脱敏和 retention policy；对关键字段做数据库约束。
- **证据：** `backend/app/engine/trace.py`、`backend/app/engine/observability.py`、`backend/app/models.py`。

### 13.8 默认密钥和健康检查掩盖故障

- **现象：** 默认 JWT secret 或空 key 可以启动；旧健康检查只代表进程存活，无法说明数据库、Redis、迁移和 checkpoint 可用。
- **根因：** liveness 与 readiness 混用，配置校验没有按环境分层。
- **修复：** `config.py` 生产 fail-fast；`health.py` 增加依赖探针和 MemorySaver readiness 阻断；Compose 用 `${VAR:?}`。
- **继续优化：** 增加告警、启动配置摘要（不打印 secret）和依赖故障演练。
- **证据：** `backend/app/config.py`、`backend/app/health.py`、`deploy/docker-compose.yml`。

### 13.9 reject 不落库、发布绕过状态机

- **现象：** 早期 `reject_node()` 只返回状态，数据库没有失败终态；发布 API 没有严格检查 `passed`。
- **根因：** 图状态和领域状态分离，路由缺少状态迁移前置条件。
- **修复：** `reject` 写入内容/任务失败状态；`ContentService.publish_content()` 仅允许 `passed -> published`，记录发布人和时间。
- **继续优化：** 所有状态迁移通过 domain service 和条件 UPDATE，补并发审核测试。
- **证据：** `backend/app/domain/status.py`、`backend/app/services/content_service.py`。

### 13.10 任务去重和投递双写没有完全收敛

- **现象：** 普通 request hash 索引挡不住并发重复；数据库提交、旧 fallback `send_task()`、item `.delay()` 存在多条投递路径。
- **根因：** 兼容旧逻辑保留过久，Outbox 只覆盖了部分层级。
- **修复方向：** 清理 active 重复任务；建立 `(tenant_id, request_hash)` partial unique index；所有 task/item dispatch 只写 Outbox；relay 使用稳定 event_id；消费端按 item/thread 幂等。
- **验收：** 100 个并发相同请求最多一个 active task；模拟 relay crash、broker timeout、重复 replay 均不产生重复业务结果。
- **证据：** `backend/app/services/generation_service.py`、`backend/app/outbox.py`、`backend/app/repositories/task_repository.py`、`backend/app/models.py`。

## 14. 当前仍未闭环的高风险问题（按优先级）

### P0：投递与去重一致性

1. 用数据库 partial unique index 取代普通 `request_hash` 索引。
2. 统一 task/item Outbox，删除直接 `send_task()`/`.delay()` fallback。
3. `get_by_request_hash()` 覆盖所有 active 状态，并用 `IntegrityError` 处理并发竞态。

### P0：租户隔离

1. 引入不可由客户端覆盖的 `TenantContext`。
2. 所有 service/repository 默认带 tenant 条件，`get_by_id()` 不允许无 scope 查询。
3. 对任务、内容、知识、Trace 和审核 API 加跨租户负向集成测试。

### P1：质量数据可复现

1. 每条人工审核记录保存审核者身份、模板/prompt/skill/model hash 和质量配置快照。
2. 统计按模板版本、租户、时间窗口、题型分层。
3. 真实采集至少 30 个配对样本后再报告 kappa，不能用单测数字冒充线上效果。

### P1：恢复语义与成本边界

1. 增加真实 fake-provider worker kill/restart 演练，覆盖节点副作用和 checkpoint 窗口。
2. 对每个任务设置 token/cost/deadline 上限；模型重试和改版共享预算。
3. 将 Trace schema version、脱敏和 retention 纳入运维策略。

## 15. 面试中绝对不能说的表述

| 不要说 | 应该说 |
|---|---|
| “已经 exactly-once” | “采用至少一次投递，靠稳定 id、状态和唯一约束降低重复；跨系统仍有窗口” |
| “PostgresSaver 保证任何故障都能恢复” | “已验证跨连接 checkpoint 恢复；真实 LLM worker kill 仍需补演练” |
| “完整多租户隔离” | “部分查询已带 tenant 过滤，仍在收敛统一 TenantContext” |
| “Pydantic 保证题目质量” | “保证结构和字段约束，语义质量由业务校验、Judge 和人工审核负责” |
| “240 个测试且覆盖率 80%+” | “本轮 240 条非集成测试通过；覆盖率本轮未重跑，CI 有 coverage gate” |
| “Outbox 解决了消息一致性” | “Outbox 解决业务提交到待投递事件的可靠记录，不解决 broker 和 DB 原子性” |
| “换独立 Judge 就没有偏差” | “降低同模型自偏好，需要人工样本和分层评估验证” |

## 16. 30 秒快答卡片

- **为什么 LangGraph？** 因为需要图路由、循环改版、interrupt 和 checkpoint；Celery 只负责异步投递。
- **为什么 acks_late？** worker 崩溃可重投；代价是至少一次，必须幂等。
- **为什么 PostgresSaver？** MemorySaver 跨进程丢状态；Postgres 提供可恢复 checkpoint。
- **为什么 Pydantic？** 云端没有 logits 约束；服务端二次校验比只信 prompt 可靠，但不保证语义。
- **为什么独立 Judge？** 减少生成模型自评偏差；仍需人工校准。
- **kappa 是什么？** 扣除随机一致性的协议一致性指标；小样本不做验收。
- **Outbox 做什么？** 事务内记录待投递事件，relay 重试/dead/replay；不等于 exactly-once。
- **最大技术债？** task/item 投递未完全 Outbox 化、request hash 非唯一、多租户 scope 未统一。
- **线上质量下降怎么查？** 按版本/模型/租户/时间切 Trace，检查首过率、重试、Judge、人工驳回、RAG 和成本。

## 17. 证据索引

| 主题 | 代码/文档证据 |
|---|---|
| 工作流节点和路由 | `backend/app/workflow/graph.py`：`after_validate`、`after_qc`、`submit_review_node`、`human_review_node` |
| checkpoint 降级和恢复 | `backend/app/workflow/graph.py`：`_get_checkpointer`；`backend/app/health.py`；`backend/scripts/recovery_drill.py` |
| Celery 重试和 item 幂等 | `backend/app/worker/tasks.py`、`backend/app/worker/celery_app.py`、`backend/app/worker/recovery.py` |
| Outbox/dead/replay | `backend/app/outbox.py`、`backend/scripts/replay_outbox.py` |
| 去重风险 | `backend/app/models.py`：`GenerationTask.request_hash`；`backend/app/repositories/task_repository.py`；`backend/app/services/generation_service.py` |
| 结构化输出 | `backend/app/engine/structured_output.py`、`backend/tests/test_output_schema_validation.py` |
| Judge/kappa/校准 | `backend/app/engine/quality.py`、`backend/app/engine/agreement.py`、`backend/app/calibration.py` |
| 状态与发布 | `backend/app/domain/status.py`、`backend/app/services/content_service.py` |
| RAG/向量维度 | `backend/app/rag/retriever.py`、`backend/app/rag/embedding.py`、`backend/app/models.py`：`Vector(1024)` |
| 版本治理 | `backend/app/versioning.py`、`backend/app/model_governance.py` |
| 生产配置和部署 | `backend/app/config.py`、`backend/app/health.py`、`deploy/docker-compose.yml` |
| 测试口径 | `backend/tests/`；命令见本文 0.2；CI 见 `.github/workflows/backend-ci.yml` |
| 设计与变更历史 | `docs/优化技术设计3.0.md`、`docs/优化记录.md`、`docs/tasks.md` |

## 18. 最终答题原则

面试时不要把组件名称当作能力证明。每说一个“可靠”“可恢复”“可扩展”，都要立即补充：保证的边界是什么、依赖哪个文件/函数、失败窗口在哪里、测试验证到哪一步、下一步如何修复。这个项目真正有价值的地方不是堆了 LangGraph、Celery、PostgreSQL 和 pgvector，而是已经暴露并修复了一批真实的 Agent 工程问题；真正会被追问的地方，则是分布式一致性、租户安全和线上数据证据尚未完全闭环。
