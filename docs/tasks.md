# 英语教研 AI 内容生成平台 2.0 开发任务清单

> 配套文档：《AI内容生成平台2.0-PRD.md》《AI内容生成平台2.0-技术架构设计.md》《优化记录.md》《优化技术设计3.0.md》
> 版本：V3.0 设计阶段
> 技术栈：LangGraph + FastAPI + Pydantic v2 + Celery/Redis + PostgreSQL + Langfuse + React + Docker Compose

---

## 任务总览

| 阶段 | 目标 | 任务数 | 已完成 |
|---|---|---|---|
| P0 基础闭环 | 单选模板 + 生成→校验→质检→入库 + 前端工作台 | 16 | **16** ✅ |
| P1 骨架扩展 | 完形/阅读模板 + 断点续跑 + 路由降级 + RAG + Prompt/Skill 工程化 | 9 | **9** ✅ |
| P2 质量优化 | 质检校准 + 数据回流 + 微调 | 6 | **5** |
| P3 平台完善 | Trace 回放 + 成本报表 + 对外化预留 | 5 | **3** |
| V2.1 优化技术设计 2.0 | 质量闭环合法化 + 可靠性加固（P0×6 + P1×4） | 10 | **10** ✅ |
| V3.0 优化技术设计 3.0 | 生产一致性 + 安全边界 + 可运维性加固（P0×4 + P1×5 + P2×3） | 12 | **4** |

> 每个任务标注：优先级（P0/P1/P2）、依赖、验收标准。方括号 `[x]` 标记完成状态。

---

## P0 基础闭环（16 项）

### A. 项目脚手架与 Docker 环境

- [x] **A1. 初始化 monorepo 目录结构**
  - 依赖：无
  - 内容：按架构文档第 12 节创建 `backend/ frontend/ deploy/ docs/` 目录骨架
  - 验收：目录结构存在且命名与文档一致
  - 完成：目录结构已建立，含 backend/app、backend/templates、backend/alembic、frontend、deploy

- [x] **A2. 编写 docker-compose.yml（基础服务）**
  - 依赖：无
  - 内容：编排 postgres:16、redis:7、langfuse，配置 `english-edit-net` 网络，DB/Redis 不对外暴露端口
  - 验收：`docker compose up` 后三服务健康运行
  - 完成：三服务（postgres/redis/langfuse）+ backend + worker + frontend + nginx 全链路编排

- [x] **A3. 初始化后端 Python 项目**
  - 依赖：无
  - 内容：`backend/` 内建 pyproject.toml/requirements，依赖 FastAPI、SQLAlchemy 2、Alembic、LangGraph、Outlines、Celery、langfuse
  - 验收：`pip install` 可成功，启动空 FastAPI 应用返回健康检查
  - 完成：pyproject.toml 配置齐全，FastAPI 应用 + 健康检查端点 /health

- [x] **A4. 编写 backend Dockerfile**
  - 依赖：A3
  - 内容：Python 3.12 镜像，安装依赖，配合 Alembic 迁移入口
  - 验收：镜像可构建，容器可启动
  - 完成：Python 3.12-slim 镜像，含 Alembic 迁移入口

### B. 数据模型与迁移

- [x] **B1. SQLAlchemy 模型定义**
  - 依赖：A3
  - 内容：实现 `question_template / generation_task / content_item / quality_record / model_profile / trace_log` 六表，含 tenant_id 预留字段
  - 验收：模型可导入，无缺字段（对照架构文档第 7 节）
  - 完成：六表 + knowledge_chunk（RAG）+ trace_log(task_id/template_id) + generation_task(request_hash) 扩展字段

- [x] **B2. Alembic 首次迁移**
  - 依赖：A2、B1
  - 内容：生成初始迁移，连接 postgres 建表
  - 验收：`alembic upgrade head` 成功建 6 张表
  - 完成：多版本迁移（初始六表 → 质检分 → 模型路由 → knowledge_chunk → request_hash → trace_log task_id → trace_log template_id）

### C. 题型模板与配置

- [x] **C1. 模板加载器**
  - 依赖：B1
  - 内容：实现从 `backend/templates/*.yaml` 加载题型模板，校验格式后入库
  - 验收：启动时加载单选模板，`question_template` 表有记录
  - 完成：`template_loader.py` 启动时自动扫描加载 YAML 模板入库

- [x] **C2. 单选模板 YAML**
  - 依赖：无
  - 内容：按架构文档第 6 节实现 single_choice 模板（input/output/quality_rules/gen_prompt/run_config）
  - 验收：YAML 可被加载器解析并通过校验
  - 完成：single_choice.yaml 含完整 schema + quality_rules + gen_prompt（.st 引用）+ model_profile 分档

- [x] **C3. 模型路由配置（model_profile）**
  - 依赖：B1
  - 内容：实现 standard/lite 模型配置，含 provider、model_name、cost_tier、is_default
  - 验收：可在 DB 中 insert 并读取模型配置
  - 完成：model_profile 表预置 standard/lite/high 三档，按题型×难度分档路由

### D. 后端核心引擎

- [x] **D1. Outlines 约束解码模块**
  - 依赖：C2
  - 内容：由 output_schema 生成 Pydantic 模型，用 Outlines 生成 JSON 约束，调用云 API
  - 验收：对单选模板调用可返回符合 schema 的 JSON
  - 完成：`structured_output.py` 实现 Pydantic v2 schema 构建 + 云 API 调用 + JSON 二次校验

- [x] **D2. Pydantic 二次校验 + 重试**
  - 依赖：D1
  - 内容：校验失败重试 3 次，耗尽标记失败
  - 验收：故意传坏 schema 的返回，确认重试与失败标记逻辑
  - 完成：3 次重试 + 指数退避 + 最终失败标记 StructuredOutputError

- [x] **D3. LLM-judge 自动质检**
  - 依赖：D2
  - 内容：按 quality_rules 逐维度打分加权（0-100），阈值 70
  - 验收：生成 quality_record，含总分与维度分
  - 完成：`quality.py` 实现多轮采样均值 + 加权汇总 + TraceLog 记录

- [x] **D4. 模型路由 + 降级**
  - 依赖：C3、D1
  - 内容：按模板 run_config.model_profile 路由，主→备→默认降级
  - 验收：主模型失败时降级到备用，Trace 中记录
  - 完成：`router.py` 实现分档路由（default/hard）+ 主→备→默认降级链

### E. LangGraph 流水线

- [x] **E1. LangGraph 状态机**
  - 依赖：D1-D4
  - 内容：实现 GenState 与节点（generate/validate/qc/revise/done），含改版 loop（M=3）
  - 验收：跑通 生成→校验→质检→入库 全链路，低分走改版
  - 完成：`graph.py` 实现完整状态机 + 质检低分改版 + 入库节点

- [x] **E2. LangGraph checkpoint 断点续跑**
  - 依赖：E1
  - 内容：接入 checkpointer，中断后可续跑不重复已完成
  - 验收：模拟中断后重跑，已完成条目不重复生成
  - 完成：MemorySaver checkpointer 已接入

### F. Celery 任务队列

- [x] **F1. Celery worker + 任务定义**
  - 依赖：E1
  - 内容：定义批量生成任务，数量切片子任务，并发=5
  - 验收：入队后可被 worker 消费并完成生成
  - 完成：`worker/tasks.py` 实现批量切片 + 并发控制

- [x] **F2. 任务状态与进度**
  - 依赖：F1、B1
  - 内容：任务状态（待执行/执行中/部分成功/成功/失败）与进度更新
  - 验收：任务各状态正确流转，进度可查询
  - 完成：generation_task 状态机完整 + 进度字段实时更新

### G. FastAPI 接口

- [x] **G1. 生成/任务/内容/质检 API**
  - 依赖：F2、B1
  - 内容：实现 发起生成、查询任务/进度、内容检索、质检标注、成本查询 接口
  - 验收：REST 接口可按 PRD 功能调用并返回正确数据
  - 完成：`api/routes.py` 含 /generate、/tasks、/contents、/quality、/costs、/knowledge 全套接口

### H. 前端工作台

- [x] **H1. React 项目初始化**
  - 依赖：G1
  - 内容：Vite + React 18 + TS 初始化，配置 API 代理
  - 验收：开发服务器可启动
  - 完成：Vite + React 18 + TypeScript + Ant Design 组件库

- [x] **H2. 生成页 + 任务页**
  - 依赖：H1
  - 内容：选择题型、填参数、发起生成；任务列表/进度/重试
  - 验收：可发起生成并看到任务进度
  - 完成：GeneratePage + TasksPage 已实现，支持 3 题型参数表单

- [x] **H3. 内容库 + 质检页**
  - 依赖：H1、G1
  - 内容：内容检索/筛选/预览/发布；质检标注（通过/驳回）
  - 验收：可查看生成内容并进行人工质检标注
  - 完成：ContentsPage + QualityPage（含状态/题型筛选）已实现

- [x] **H4. 前端 Dockerfile + nginx**
  - 依赖：H2/H3
  - 内容：React 构建镜像，nginx 部署，接入 compose
  - 验收：`docker compose up` 后前端可访问
  - 完成：nginx 镜像 + compose 编排，`localhost:3000` 可访问

---

## P1 骨架扩展（9 项）

- [x] **I1. 完形填空模板 + 流水线**
  - 依赖：P0 闭环
  - 内容：cloze 模板 YAML + 适配 LangGraph 生成/质检
  - 验收：可生成完形题并通过质检
  - 完成：`cloze.yaml` 加载入库；增强 structured_output 支持嵌套数组/子对象递归渲染与二次校验；生成→质检→入库验证通过（质检分 94.38）

- [x] **I2. 阅读理解模板 + 流水线**
  - 依赖：P0 闭环
  - 内容：reading 模板 YAML + 适配流水线
  - 验收：可生成阅读题（含多小题）并通过质检
  - 完成：`reading.yaml` 加载入库；短文+多小题嵌套结构生成成功（质检分 92.62）；前端动态表单与通用预览组件已适配

- [x] **I3. RAG 知识库接入**
  - 依赖：I1/I2
  - 内容：教材/课标/真题分块入向量库，按知识点检索注入生成上下文
  - 验收：生成时命中相关知识片段，内容有依据
  - 完成：pgvector + 阿里云百炼 text-embedding-v3；新增 `knowledge_chunk` 表与 `app/rag/`（embedding/indexer/retriever）；生成前按知识点检索注入 rag_context；知识库管理 API（上传/列表/删除/检索）；索引→检索→注入→生成→质检→入库全链路验证通过（质检分 93.75）

- [x] **I4. 模型路由降级完善**
  - 依赖：P0
  - 内容：按题型复杂度/难度差异化路由，降级链生产化
  - 验收：复杂阅读题走高性能模型，单选走 lite
  - 完成：`run_config.model_profile` 支持按难度分档的字典配置（`default`/`hard` 等，缺档回退 default，再回退任意档位）；`router._resolve_primary_profile_name` 解析分档，字符串形态向后兼容；`single_choice` 默认走 lite、`cloze`/`reading` 默认 standard、难题升级 high；新增 5 个难度路由单测，全套 41 个测试通过

- [x] **I5. 质检/成本前端页面**
  - 依赖：G1、H3
  - 内容：质检页完善 + 成本页（按题型/模型/任务聚合）
  - 验收：成本报表可查看
  - 完成：质检页新增状态/题型筛选；成本页支持按 题型/模型/任务 三维度聚合 + 题型筛选；`TraceLog` 新增 `task_id`/`template_id` 列（迁移 c2d8/d3e9）并在生成/质检链路记录，成本接口按 `group_by` 聚合；41 测试通过、前端 tsc 通过、端到端验证三维度聚合正确

- [x] **I6. 站内消息通知**
  - 依赖：G1
  - 内容：任务完成/失败/驳回通知，站内信
  - 验收：任务完成时收到站内通知
  - 完成：新增 `app_notification` 表（迁移 e4a1）+ `app/notification.py` 服务；任务结束（succeeded/partially/failed）在 Celery worker 收尾生成通知，人工质检驳回在 `/api/quality/{id}/review` 生成通知；新增 `/api/notifications`（列表/未读数/单条已读/全部已读）接口；前端新增消息页 + 侧边栏未读角标（30s 轮询）；4 个通知单测通过，端到端验证全生命周期（创建→未读→已读→清零）

- [x] **I7. 重复提交去重**
  - 依赖：G1
  - 内容：按题型+输入参数哈希去重，重复提示
  - 验收：相同参数重复发起被拦截
  - 完成：`app/dedup.py` 提供规范化参数指纹；`generation_task` 新增 `request_hash` 列与唯一索引迁移；生成接口对仍在执行（pending/running）的相同任务返回 409 `DUPLICATE_TASK`；新增 `DuplicateTaskError` 与 5 个指纹单测通过

- [x] **I8. 批量任务稳定性**
  - 依赖：P0
  - 内容：大批量（如 500+）任务测试、失败重试、并发压测
  - 验收：大批量稳定完成，无超时丢任务
  - 完成：Celery 任务超时加固（`task_soft_time_limit=600s` / `task_time_limit=900s`，防单任务卡死整体阻塞）；新增 `scripts/batch_stress.py` 压测脚本（先统一 commit 再投递 Celery，修复 worker 因看不到未提交行而误判「任务不存在」导致任务假死的问题）；容器内 500 任务并发压测验证：任务稳定推进、无超时丢任务、无「任务不存在」卡死、5 并发 worker 持续消费直至达终态

- [x] **I9. Prompt 工程化升级**
  - 依赖：P0
  - 内容：Prompt 独立文件化（.st system/user 分离）+ Skill 架构（SKILL.md + skill.meta.yml）+ 结构化 6 段式 Prompt（Role/Task/质量标准/命制规则/难度分级/约束/Output Format）
  - 验收：新增 `backend/prompts/`（6 个 .st 文件）+ `backend/skills/`（4 个 skill 目录）+ `app/prompt_loader.py` + `app/skill_registry.py`；Prompt 行数 ↑3900%，结构化 6/6 段完整；50 测试通过；端到端生成质量显著提升
  - 完成：6 个 .st prompt 文件（system/user 分离，40-50 行结构化）+ 4 个 skill（single_choice/cloze/reading/generate_question）+ prompt_loader/skill_registry 加载器；9 个新测试；端到端验证生成质量提升（真实语境题干 + 同质选项 + 详细解析）。详见 `docs/优化记录.md` OPT-002

---

## P2 质量优化（6 项）

- [x] **J1. 质检标准校准**
  - 依赖：P1
  - 内容：用人工抽检结果校准 judge 权重/threshold
  - 验收：校准后人工驳回率 ≤5%
  - 完成：新增 `quality_calibration` 表（迁移 g6b7c8d9e0f1）+ `QualityRecord.reason` 列；`app/calibration.py` 纯函数模块（`fit_weights` 放水度计算与降权归一化 + `collect_labeled` 标注对收集 + `recalibrate` 编排与守卫 + `get_effective_weights` 覆盖权重读取）；`quality.py` `_aggregate_score` 支持 `weights_override`；`graph.py` `qc_node` 接入覆盖权重；新增 `POST /api/quality/calibrate` + `GET /api/quality/calibration`；前端看板页新增"质检校准中心"（一键校准 + 生效/默认权重对比表 + 驳回率）；8 个校准纯函数单测，全量 109 测试通过。详见 `docs/优化记录.md` OPT-014
  - 已实现链路：低分自动改版（graph.py after_qc）→ 人工驳回记录（review_content 存 reason）→ 假阳性样本（auto 高分但 manual 驳回）→ 计算放水度 → 降权；质量收益与 ≤5% 目标待独立标注集验证。

- [x] **J2. 数据回流**
  - 依赖：P1
  - 内容：高质量样本沉淀为 few-shot / 微调语料
  - 验收：回流样本可检索、可导出
  - 完成：新增 `sample_pool` 表（迁移 f5a6，含 item_id 唯一/template_id/knowledge_point 索引 + payload 快照）+ `app/sample_pool.py` 服务（`is_eligible` 高质量判定、`pool_item` 幂等沉淀、`sync_eligible` 自动同步人工通过项、`list_samples` 多条件检索、`remove_sample` 移除）；新增 `/api/samples`（沉淀）、`/api/samples/sync`（自动同步）、`/api/samples`（检索）、`/api/samples/export`（JSONL 导出）、`DELETE /api/samples/{id}`；前端新增样本库页（浏览/自动沉淀/导出/移除，复用通用预览）；11 个新单测，全量 73 测试通过，前端 tsc 通过。详见 `docs/优化记录.md` OPT-009

- [~] **J3. 微调评估（QLoRA）— 暂缓**
  - 依赖：J2
  - 内容：评估是否值得微调，选测 QLoRA+DPO
  - 验收：微调前后质检通过率对比报告
  - 状态：**暂缓**。微调需本地部署模型，当前平台为云端 API 调用（Qwen/DeepSeek/GLM），与现有技术路线不符；数据回流（J2）已先行落地，待未来接入本地/可微调模型时再评估。

- [x] **J4. judge 稳定采样**
  - 依赖：P1
  - 内容：judge 多次采样取均值，降低抖动
  - 验收：同一样本多次质检分方差下降
  - 完成：`quality.py` 实现 rubric 结构化注入（system/user 分离，分档评分标准）+ `aggregate_rounds` 多轮均值聚合（缺失维度按轮独立处理）+ 单轮失败跳过不阻断；3 个模板 quality_rules 均含 rubric 分档；`judge_stability.py` 交错采样方差验证脚本（单次方差为 0 即判通过）；`test_quality.py` 新增 TestAggregateRounds/TestBuildJudgePrompt 单测；JUDGE_SAMPLE_ROUNDS 默认调为 1（rubric 已使单次稳定，多轮收益有限徒增成本），保留 rounds 参数作为可选降噪开关
  - skill 化：judge 质检 prompt 从代码硬编码抽取为独立文件 `prompts/judge-system.st` + `prompts/judge-user.st`（{{rubric}}/{{payload}} 占位符）；新增 `skills/judge/SKILL.md` + `skill.meta.yml` 质检技能规范（评分流程/分档判定/尺度一致性/Anti-Patterns）；`skill_registry` 新增 `get_skill_by_id` 支持跨题型通用技能按 id 读取；`quality.py` 经 prompt_loader 加载 .st + 注入 judge SKILL.md；59 测试通过

- [x] **J5. Trace 回放页面**
  - 依赖：P1
  - 内容：前端 Trace 页回放单次生成链路
  - 验收：可凭 trace_id 查看完整链路
  - 完成：后端新增 `GET /api/traces`（按 trace_id 去重聚合的链路摘要列表，含调用数/累计成本/耗时/时间范围）+ `GET /api/traces/{trace_id}`（按时间升序回放完整链路，推断 stage：generate/qc）；前端新增 TracePage（链路列表 + trace_id 输入查询 + 链路步骤时间轴卡片，可展开查看 input/output JSON，stage 标签区分生成/质检，汇总条显示调用数/累计耗时/成本）；复用现有单色调样式，新增 trace 专属 CSS；前端 tsc 类型检查通过、后端 59 测试通过

- [x] **J6. 指标看板**
  - 依赖：P1
  - 内容：生产周期/符合率/通过率/驳回率/成本指标展示
  - 验收：指标按 PRD 第 15 节口径展示
  - 完成：后端新增 `GET /api/dashboard`（口径对齐 PRD 15.1：内容产出量/已发布量/质检通过率（source=auto score≥threshold）/人工驳回率（manual score=0）/平均生产周期（任务 updated-created 秒）/单条平均成本 + 按题型产出通过率明细 + 近 10 条任务生产周期）；`case`/`extract` 聚合 SQL 编译验证通过；前端新增 DashboardPage（KPI 卡片含目标值与达标判定、汇总条、按题型产出表、任务周期表），导航新增「看板」；前端类型检查通过、后端 59 测试通过；容器端到端验证：修复 `settings` 未导入报错，并统一 by_template 通过率口径（改用 QualityRecord source=auto score≥threshold 与全局 KPI 一致，而非 ContentItem.status，因自动质检后状态仍为 pending_qc），实测产出 1/质检通过率 100%/周期 67.7s

---

## P3 平台完善（5 项）

- [ ] **K1. 对外化数据预留落地**
  - 依赖：P2
  - 内容：启用 tenant_id/权限字段，多租户隔离设计
  - 验收：数据结构支持多租户

- [x] **K2. 深度成本报表**
  - 依赖：P2
  - 内容：成本按 token 细分（生成/质检），多维聚合
  - 验收：成本可下钻到单条
  - 完成：`TraceLog` 新增 `stage`/`prompt_tokens`/`completion_tokens` 列（迁移 a7b8，index stage）+ `token_breakdown()` 拆分输入/输出 token 与成本；`structured_output`/`quality` 记录时注入 stage 与 token；新增 `GET /api/costs/deep`（按生成/质检阶段拆分 + 题型/模型/任务多维聚合 + 单条调用下钻）；前端成本页新增深度成本汇总 KPI、阶段拆分表、三维聚合表、单条下钻表；4 个新单测，全量 77 测试通过。详见 `docs/优化记录.md` OPT-010

- [ ] **K3. 多模态扩展点**
  - 依赖：P2
  - 内容：Schema 支持音频/图片字段，预留听力/图文题型
  - 验收：模板可声明多媒体字段

- [x] **K4. 权限与多角色**
  - 依赖：K1
  - 内容：拆分包管理员/教研员/质检员/查看者角色
  - 验收：角色权限按 PRD 第 5/13 节生效
  - 完成：User 模型 + Alembic 迁移（b1c2）+ security.py（密码哈希/JWT/权限矩阵/依赖注入 get_current_user/require_permission）+ 30+ 接口鉴权挂载 + 前端登录页/路由守卫/动态导航/用户管理页 + 种子管理员（seed.py 独立模块，环境变量可配，幂等写入不覆盖密码）+ 单元测试 101 全通过。Docker 端到端验证通过（backend/worker/frontend 镜像重建 + 迁移成功）；README 接口清单已补齐认证/校准端点
  - 详见 `docs/优化记录.md` OPT-013

- [ ] **K5. 部署与监控完善**
  - 依赖：P2
  - 内容：生产化 compose、日志、告警、vLLM 自部署评估
  - 验收：生产环境可滚动部署，有基础告警

---

## 依赖关系图

```
P0: A1→A2→A3→A4 ──→ B1→B2
     A3 ──→ C1→C2 ──→ D1→D2→D3→D4 ──→ E1→E2 ──→ F1→F2 ──→ G1 ──→ H1→H2→H3→H4
P1: P0 ──→ I1→I2→I3 , P0→I4/I5/I6/I7/I8
P2: P1 ──→ J1→J2→J3 , P1→J4/J5/J6
P3: P2 ──→ K1→K2→K3→K4→K5
```

---

## 优化技术设计 2.0（质量闭环合法化与可靠性加固）

> 设计文档：`docs/优化技术设计2.0.md`（2026-09-05，含问题清单 G1~G12 / 详细设计 / 指标口径字典 / 简历声明合法化对照表）

- [x] **P0-1. 结构化输出校验错误回注重试**（OPT-015）：多轮消息回注上次输出+字段级错误摘要，开关 `STRUCTURED_RETRY_FEEDBACK`；失败尝试落 TraceLog
- [x] **P0-2. 结构化符合率埋点**（OPT-016）：TraceLog 增 attempt/success（迁移 h7c8d9e0f1a2）；`compute_structured_stats` 纯函数；dashboard 4 项新 KPI + `/api/traces/structured-stats`
- [x] **P0-5. 依赖与声明清理**（OPT-017）：移除 outlines 死依赖、补声明 openai；Trace Sink 分发（db SSOT + langfuse 可选导出）
- [x] **P0-3. Checkpointer 持久化**（OPT-018）：MemorySaver → PostgresSaver（惰性单例 + 失败降级 + `CHECKPOINTER_BACKEND` 开关）
- [x] **P0-6. Celery 任务可靠性**（OPT-019）：acks_late + task_reject_on_worker_lost + 瞬态错误 autoretry + 僵尸任务恢复（依赖 P0-3 幂等）
- [x] **P0-4. Judge 一致性评估**（OPT-020）：`cohen_kappa` 纯函数 + `scripts/judge_agreement.py`（accuracy/kappa/混淆矩阵，n≥30 守卫）
- [x] **P1-2. Judge 独立模型配置**（OPT-021）：`JUDGE_MODEL_NAME` 全局 + 模板级 `judge_model` 覆盖（自偏好偏差治理）
- [x] **P1-3. 分模型价目表**（OPT-022）：`MODEL_PRICES` JSON 配置，`compute_cost` 按模型计价，未命中回退全局单价
- [x] **P1-1. LangGraph interrupt 人工卡点**（OPT-024，附 OPT-023 改版双生成 Bug 修复）：灰区转人工（模板级开关），`submit_review`+`human_review` 两节点 + `resume_human_review`（依赖 P0-3）
- [x] **P1-4. 集成测试 + CI**（OPT-025，CI 失败修复见 OPT-039/OPT-040）：可测性重构（`_get_openai_client` 缝）、`tests/integration/`（真 Postgres，5 用例）、GitHub Actions 双 job（覆盖率门槛 80%）

---

## 优化技术设计 3.0（生产一致性、安全边界与可运维性加固）

> 设计文档：`docs/优化技术设计3.0.md`（2026-09-07）。以下项目均为待实施设计，不得提前标记为完成。

- [x] **P0-1. 状态机终态与任务统计闭环**（OPT-028）：`reject_node()` 持久化拒绝内容/原因；worker 按 item result 统计；发布仅允许 `passed → published`；新增任务条目结果模型
- [x] **P0-2. 租户隔离与 viewer 可见性**（OPT-028）：认证上下文推导 tenant；Repository 强制 tenant scope；viewer 仅可见 published；RAG/Trace/通知/样本/成本统一过滤
- [x] **P0-3. Outbox、唯一幂等和死信**（OPT-028 部分：event_id 幂等键；待批次 B：投递逻辑）：任务与 outbox 同事务；relay 投递；active request_hash 部分唯一索引；dead/replay 管理接口
- [x] **P0-4. 生产安全基线与工具权限**（OPT-028 部分：生产 fail-fast；待批次 B：工具权限）：生产配置 fail-fast；移除可用默认密钥；工具 allowlist、超时、审计和脱敏
- [x] **P1-1. API/Service/Repository 分层**（OPT-028/029/030 完成）：拆分 `routes.py` 上帝模块（1257→1196 行），37/37 端点完成重构（100%）。Repository（8 文件）+ Service（8 服务）+ 统一参数命名（`_` → `current_user`）。复杂聚合端点（Cost/Dashboard/Trace）采用务实策略保留原逻辑，避免过度工程化
- [x] **P1-2. 模板输入与输出 Schema 真校验**（OPT-031）：按 `input_schema` 入口校验；保留数组及对象约束；错误不创建任务
- [x] **P1-3. 取消、暂停与批量 item 并发**（OPT-032）：取消 API、合作式退出、单 item Celery job、失败分类与指数退避；父任务按 item 结果聚合
- [x] **P1-4. 可观测性与健康检查**（OPT-032）：live/ready/dependencies 端点；embedding/queue/workflow Trace；递归敏感信息脱敏；生命周期事件可回放
- [x] **P1-5. 部署、迁移和恢复演练**：Langfuse 数据库初始化、迁移预检查、备份恢复、checkpoint/outbox 重放；脚本与验收步骤见 `docs/P1-5-部署迁移恢复演练.md`。Docker PostgreSQL/Redis、迁移、独立恢复库、PostgresSaver 跨连接恢复及 outbox dead/replay/relay 已实测；真实 LLM 业务 worker kill/restart 演练不作为本个人项目的完成前置条件。
- [x] **P2-1. 模型档案与模板引用一致性**：启动校验、模型健康、fallback/cooldown 和预算约束；模型档案 API 暴露治理字段
- [x] **P2-2. 质量闭环可复现性**：审核者身份、质量配置快照、按模板版本/租户/时间窗口统计；`GET /api/quality/stats`
- [x] **P2-3. 配置和版本治理**：模板/prompt/skill/model hash、任务版本快照、配置变更审计；`GET /api/config-audit`

---

## 自用生产 P0 修复（2026-10-04）

> 对应本次 [验证状态与风险](现状文档/验证状态与风险.md) 的七项，按原顺序执行；这里的完成勾选仅表示代码和纯单测交付。历史 P0/P1 验收不替代当前生产准入证据。用户要求暂不启动 Docker，数据库集成未执行。

- [x] **SELF-P0-1. Judge 生成目标上下文**（OPT-039）：独立传入 input_schema 声明参数，Prompt/Trace 留存目标；不混入 RAG、改版和凭据。
- [x] **SELF-P0-2. Judge 输出硬校验**（OPT-040）：动态 Pydantic 校验完整维度、范围和有限数；失败轮有限重试、记录成本；禁止缺维度归一化放行。
- [x] **SELF-P0-3. JSON Schema 错误反馈重试**（OPT-041）：精确异常捕获、字段反馈、失败 Trace 与耗尽回归；补直接 jsonschema 依赖声明。
- [x] **SELF-P0-4. 自动质检分母修复**（OPT-042）：草稿级 quality_evaluation；拒绝最终 auto 记录；首轮和最终终结分母分开；看板无样本 null；待真实迁移。
- [x] **SELF-P0-5. 有效阈值统一**（OPT-043）：路由、灰区、事件、auto/manual 快照一致；0 阈值和重放冻结值回归。
- [ ] **SELF-P0-6. 真实人工金标验收**（OPT-044）：候选导出、双人标注/第三人仲裁、hash/版本校验与离线报告工具已交付；真实教研人员标注未完成，不宣称 ≤5% 或答案正确率达标。
- [ ] **SELF-P0-7. 真实跨进程恢复验收**（OPT-045）：dict_row、生产禁内存降级、invoke(None)、终态/人工重放、thread 锁与唯一键、父子状态修复及 3 条真 Postgres 子进程测试已交付；真实数据库测试按用户要求暂缓。

本轮系统解释器回归：**287 passed, 8 skipped**（新增 47 条纯单测，新增 3 条集成用例未执行）；前端构建、修改文件 lint 和三模块 scoped mypy 通过。新 Alembic head：`p0_07_thread_unique`（仅离线 SQL 校验）。操作说明见 [P0-质量金标与恢复验收](P0-质量金标与恢复验收.md)。

---

## 自用生产 P1 修复（2026-10-04）

> 对应 [验证状态与风险](现状文档/验证状态与风险.md) 中 P1 九项，勾选仅表示代码与本地回归。仍遵守不启动 Docker、不执行真实数据库/付费模型测试；P0 的真实金标与跨进程验收保持未完成。

- [x] **SELF-P1-1. 租户上下文贯通**（OPT-046）：任务认证来源覆盖参数伪造；所有派生内容/评分/知识/样本/Trace 归属一致；默认租户不虚构为 default；迁移先审计非空冲突。
- [x] **SELF-P1-2. API 读写/运营 scope**（OPT-047）：非 admin 固定资源 scope，Null 显式 IS NULL；详情/取消/审核/发布/删除/导出同控；共享配置仅可读；viewer 发布内容规则、JWT/禁用账号及登录/用户/通知接口契约回归。
- [x] **SELF-P1-3. RAG 策略与来源证据**（OPT-048）：命中/无命中/降级、来源文本快照/hash、required 失败关闭、人工来源声明与发布门控、金标 RAG 分层；不冒充自动事实核验。
- [x] **SELF-P1-4. 知识服务边界统一**（OPT-049）：文件/文本共用索引、同一 scope 检索/删除；有限文件读取、输入/向量完整性校验；去除占位服务方法。
- [x] **SELF-P1-5. 结构化失败分类**（OPT-050）：状态码/SDK/连接异常解包；未知错误不看字符串数字；重试有上限，耗尽父子终态与通知。
- [x] **SELF-P1-6. 模型档案可配置差异**（OPT-051）：显式映射、实际字段 hash、难度中文键修正、独立 Judge endpoint/key 与可选启动策略；真实模型差异和收益仍未验。
- [x] **SELF-P1-7. 启动关键失败关闭**（OPT-052）：生产不 create_all、只接受迁移 head；部分模板/模型/管理员失败阻启动；worker/frontend 等待 backend ready。
- [x] **SELF-P1-8. CORS 与默认网络边界**（OPT-053）：origins 白名单、默认无凭据、禁生产/凭据通配符、回环端口；真实 TLS/LAN 配置待验。
- [x] **SELF-P1-9. Trace 持久化补偿与费用核对**（OPT-054）：稳定 ID、原子补偿/幂等 replay、健康与丢失告警、严格失败关闭、pending 预算、未知 usage 与冻结单价分量、规范化账单比对 CLI；不是事务/供应商金额证明。

本轮：**365 passed, 8 skipped**，P0 后 287→365（新增 78 条纯单测）；前端构建通过，六个 P1 模块 scoped mypy、修改文件 lint 通过。迁移新 head `p1_09_trace_ledger` 仅离线 SQL 校验。操作/环境边界见 [P1 修复与验收](P1-修复与验收.md)。

---

## 里程碑验收

| 里程碑 | 通过标准 | 状态 |
|---|---|---|
| P0 完成 | 单选可从工作台发起生成→自动质检→入库→人工质检，全链路可用 | ✅ 已通过 |
| P1 完成 | 3 类题型可生产，RAG 生效，任务稳定，成本可查，Prompt/Skill 工程化 | ✅ 已通过 |
| P2 功能交付 | 人工驳回率 ≤5%，质检稳定，Trace 可回放（微调评估 J3 暂缓，因云端 API 技术路线） | 机制已交付（J1/J2/J4/J5/J6）；驳回率与稳定性目标待真实标注样本验证，J3 暂缓 |
| P3 完成 | 多租户/多角色可用，成本可下钻，多模态预留可用 | 进行中 |

---

## 变更记录

| 版本 | 日期 | 变更内容 |
|---|---|---|
| V1.0 | 2026-08-11 | 基于 PRD 与技术架构文档生成任务清单 |
| V1.1 | 2026-08-11 | P0 全部 16 项标记完成；新增 I9 Prompt 工程化升级；里程碑状态更新；新增「已完成」列 |
| V1.2 | 2026-08-13 | J4 judge 稳定采样完成；judge 质检 prompt/skill 工程化（独立 .st + SKILL.md） |
| V1.3 | 2026-08-13 | J5 Trace 回放页面完成（后端 trace API + 前端链路回放页） |
| V1.4 | 2026-08-13 | J6 指标看板完成（后端 /api/dashboard + 前端看板页） |
| V1.5 | 2026-08-13 | J2 数据回流完成（sample_pool 表 + 样本库接口/前端 + JSONL 导出） |
| V1.6 | 2026-08-13 | J3 微调评估标记为暂缓（当前为云端 API 技术路线，微调需本地部署模型） |
| V1.7 | 2026-08-13 | K2 深度成本报表完成（token 细分 + 生成/质检阶段拆分 + 多维聚合 + 单条下钻） |
| V1.8 | 2026-08-13 | I3 增强：RAG 知识库前端文件上传（txt/md/docx/pdf，试卷/练习册等）+ 知识库管理页 |
| V1.9 | 2026-08-13 | K4 权限系统主体完成（JWT 认证 + 种子管理员 + 单元测试） |
| V2.0 | 2026-08-13 | J1 质检权重反向校准闭环完成（quality_calibration 表 + calibration.py + qc_node 覆盖权重 + 校准 API + 前端校准中心）；OPT-014 |
| V2.1 | 2026-09-05 | 优化技术设计 2.0 启动：P0-1 错误回注重试（OPT-015）、P0-2 符合率埋点（OPT-016）、P0-5 依赖清理+Langfuse Sink（OPT-017）完成 |
| V2.2 | 2026-09-05 | P0-3 Checkpointer 持久化（OPT-018）、P0-6 Celery 可靠性（OPT-019）、P0-4 judge 一致性 kappa（OPT-020）完成；全量 150 单测通过 |
| V2.3 | 2026-09-05 | P1-2 judge 独立模型（OPT-021）、P1-3 分模型价目表（OPT-022）完成；全量 161 单测通过 |
| V2.4 | 2026-09-05 | OPT-023 改版双生成 Bug 修复（每次改版 2 次 LLM 调用→1 次，质检反馈真实注入）+ OPT-024 灰区 interrupt 人工卡点；全量 175 单测通过 |
| V2.5 | 2026-09-05 | OPT-025 可测性重构 + 集成测试（真 Postgres）+ CI 覆盖率门槛完成：180 用例全过，覆盖率 81.44%；优化技术设计 2.0 的 P0/P1 全部交付 |
| V2.6 | 2026-09-06 | OPT-026 交付审查修复：CI 覆盖率门槛接线、GenState 三键显式声明 + 阈值测试盲区消除、温度配置化、lint（black/isort/flake8）全绿 + CI lint job；项目本体初始化独立 git 仓库并分批入库 |
| V3.0 | 2026-09-07 | 新增《优化技术设计3.0》：生产一致性、安全边界、Outbox/幂等、租户隔离、状态契约与可运维性设计；全部标记为待实施 |
| V3.1 | 2026-09-08 | 批次 A P0 完成（OPT-028）：状态终态一致性/租户隔离/Outbox幂等键/生产fail-fast；9文件+迁移脚本+19单测全绿 |
| V2.7 | 2026-09-08 | OPT-031 P1-2 Schema 真校验完成：输入 JSON Schema 校验（422 拦截）、输出约束保留（minItems/maxItems/enum/minLength/maxLength）、去重版本化、双重校验（Pydantic + jsonschema）；210 单测全过，覆盖率 51% |
| V3.2-P0 | 2026-10-04 | OPT-039～045：自用生产 P0 按序修复；287 纯单测通过、8 集成跳过；真实金标与跨进程验收未完成，Docker 测试按用户要求暂缓 |
| V3.3-P1 | 2026-10-04 | OPT-046～054：P1 九项代码修复；365 通过/8 跳过，新增 78 条纯单测；真实迁移、模型/来源效果、账单和 Docker 验收仍暂缓 |


## RAG P0 A/B/C（2026-10-04，OPT-055～057）

> 配套：`docs/现状文档/RAG优化计划.md`、`RAG-P0-ABC落地与验收.md`。以下完成代表代码及单测/离线验证，不代表真实部署通过；P1 D/E/F、P2 G 保持未完成。

- [x] **RAG-A：基线、许可与兼容迁移**（OPT-055）
  - legacy chunk/API/tenant/knowledge_point/RAG 模式回归保留；新增 `knowledge_document` + nullable chunk 来源与版本字段，不重嵌入老数据。
  - 单 head `rag_p0_abc`；离线 upgrade/downgrade SQL；MIT 版权许可保留，参考源码逐文件 SHA-256，上游 commit 不冒认父目录 Git。
- [x] **RAG-B：结构化解析与内容完整性**（OPT-056）
  - DOCX 正文/表格 body-order、空/合并/嵌套单元格；Markdown/HTML 表格；PDF 页面/空页/错误；XLSX/CSV sheet/列/真实行号/公式与合并告警。
  - ParsedDocument/Block 原始与规范化快照、源 hash、解析统计/warning，无模型调用预览。
- [x] **RAG-C：自适应切块与诊断**（OPT-057）
  - heading/heuristic/recursive/legacy + 验证 fallback，边界递减小块修复，ContextHeader/section_path、表头上下文、真实源区间、row_range、预算与覆盖率验证。
  - 分批 embedding 全部校验后一次事务写入；旧来源门控保留；前端新格式/知识点/预览/warning。
  - 回归：新增 **56 条**；非集成 **421 passed / 8 deselected**（最终复跑见验收文档）；修改模块静态检查与前端构建通过。
- [x] **RAG-P1 D/E/F**：Parent-Child/邻接上下文、混合召回/RRF/rerank、知识点别名/层级/query expansion（代码/离线验证，见下节；真实运行效果未验收）。
- [ ] **RAG-P2 G 与真实验收**：重建索引运营化、真实检索评测/压测、Postgres 迁移与供应商验证、OCR/PDF 布局后续版。


## RAG P1 D/E/F（2026-10-04，OPT-058～060）

- [x] **RAG-D**（OPT-058）：兼容父子/邻接 schema；parent 无 embedding；child 命中回溯 parent、范围校验、去重、source wrapper/字符/保守字节预算；来源坐标与 snapshot/hash 同步。
- [x] **RAG-E**（OPT-059）：候选 pool/top_k 分开；pgvector + PostgreSQL FTS/字面 keyword、RRF、provider JSON rerank off/optional/required、SQL savepoint 与降级；HNSW/GIN/pg_trgm 迁移及离线 SQL。
- [x] **RAG-F**（OPT-060）：YAML canonical/aliases/parents/related、多标签、exact/ancestor/descendant/related/semantic、bounded original/alias/LLM query expansion；LLM 独立 .st、Pydantic 与调用 Trace；API/React 对接。
- 验证：新增 **65 条**，全量非集成 **486 passed / 8 deselected**；130 条相关回归 scoped coverage **95%**；22 文件 scoped mypy、修改代码格式/lint、前端 build 通过；head `rag_p1_def`。
- 仍待：真实 schema/索引/计划/召回质量、供应商兼容性/价格/效果、浏览器端到端与 P2 G。不要把 SQLite reference 或 offline SQL 当作真实 PostgreSQL 证据。
- 交付文档：`docs/现状文档/RAG-P1-DEF落地与验收.md`。


## RAG 启动前修复与真实 PostgreSQL 验收（2026-10-05，OPT-061）

- [x] backend/worker 全 RAG 配置透传与 Settings 空 optional-limit 兼容；实际 Compose 渲染验证非默认参数。
- [x] 集成 fixture 实际 Alembic 升级和 head 检查；非零差异 mock 向量；不 create_all/stamp/清库。
- [x] 复用已有 english-edit-ci-postgres：实际迁移至 rag_p1_def、13 项 PostgreSQL RAG 专项、5 项 pipeline、3 项跨进程恢复。
- [x] 当前 506 非集成 + 21 集成 = 527 断言通过，新增33条；旧数据保留；修改代码格式/lint通过。
- [ ] 全 app/CI覆盖率门禁：本地约77.45%未到80%，历史备份/损坏副本仍在工作区；未调整门槛。
- [x] 原 Compose API/worker/frontend/Redis 运行验收与课程 PDF 实际资料预览（OPT-062）；教材导入、真实语义质量/P2运营与金标仍未完成。
- 验收文档：`docs/现状文档/RAG-部署与PostgreSQL验收.md`。此节替代旧文档“Postgres从未测试”的当前口径，不抹去2026-10-04历史证据。


## 原 Compose 运行态与课程资料预览（2026-10-05，OPT-062）

- [x] 根.env本机启动项补齐、开发JWT占位替换（不展示密钥、不改现有管理员密码）；显式 --env-file .env。
- [x] 原deploy_postgres-data复用、主库实际迁移rag_p1_def；Postgres/Redis/backend/worker/frontend运行，ready与Celery ping/空outbox实际任务回执。
- [x] 浏览器实际发现并修复Nginx1MiB/后端10MiB限制不一致；无文字PDF indexable false、OCR提示、禁用索引/超限提示；后端在embedding前明确拒绝。
- [x] 15份课程PDF341页全量预检：0文字页、12份超限；浏览器登录/8页PDF预览/MD正文预览/超限验证无pageerror。
- [x] 新增6条回归；512非集成通过、21真实集成复跑通过，Python修改文件/知识服务scoped mypy、前端镜像构建/nginx-t通过。
- [ ] 下一优先：扫描教材OCR和大文件页级批次导入；后续小量真实embedding+语义召回评测。资料尚未索引，不能把运行态ready当作知识库内容可用。
- 交付：docs/现状文档/运行态与教材预览验收.md；运行界面localhost:3000。


## 扫描教材本地 OCR 选型（2026-10-05，OPT-063）

- [x] 原资料4文件12页统一1800px PNG，PaddleOCR PPStructureV3/MinerU basic ONNX/Docling指定CPU配置全部成功；36个源页复核锚点、12个同行关系统一重评分。
- [x] 评测工具惰性依赖/失败可见/无付费或数据库调用；--score-only无需重新推理，gold hash可追溯；Paddle聚合重复修复回归。
- [x] 新增9条测试，项目venv非集成521通过/21排除，修改文件格式/lint通过；原服务ready、旧业务计数保留，知识表0。
- [x] 实测报告和RAG现状/计划/风险同步；条件选型优先MinerU，Docling章节阅读顺序/PaddleGPU未测/教材源错误/词边界盲区明确记录。
- [x] OCR-1/2（OPT-064）：可复用本地解析adapter/逐页路由、结构桥接/父子切块联测及显式CLI已验收；不是网页自动OCR/生产部署。
- [ ] OCR-3/4：后台页级批次/大文件可控导入/失败重试续跑，原页预览及跨页表格/词边界/源错误审核。
- [ ] 后续真实教材embedding与知识点召回/引用金标、GPU与规模性能、许可证分发审查；未付费、未导入不标完成。
- 报告：`docs\现状文档\OCR与文档解析选型实测.md`；私有样本/全文/模型保留于 `.local-eval/ocr-2026-10-05`，不做未经确认清场。


## MinerU 本地解析接入 OCR-1/2（2026-10-05，OPT-064）

- [x] 本地4.x V1 adapter/结构JSON校验、有界超时/失败/跨源/响应上限、无云回退。
- [x] 文字/扫描/空页/混合/扫描文字层/Form图像路由，原文件hash/物理页/引擎bbox/native快照/观测；文字页不调用OCR。
- [x] rowspan/colspan逻辑grid和源span/行映射、标题/章节页眉/脚注、答案角色与不连续页上下文；现有child/parent预览复用、partial入库前拒绝。
- [x] 真实原PDF3页本地API+最终native重放、12项表格同行关系、12页缓存结构重放；新增52单测，全量594通过/非集成573通过、限定coverage89.15%、mypy/格式/lint/Compose通过。
- [x] CLI可处理大文件中的少量页，无embedding/业务DB；原数据和服务不变，helper已关闭。当前主容器未重建、网页OCR仍不可用。
- [x] OCR-3（OPT-065）：上述后台API/独立队列/租约检查点/受控上传已部署原环境，真实大文件/取消续跑/缓存通过；网页交互尚待OCR-4。
- [ ] OCR-4：网页原页与结构预览/确认、跨页续表/题组/词边界/源疑点；随后真实embedding与召回/引用金标、GPU与许可审查。
- 报告：`docs\现状文档\MinerU-OCR接入与验收.md`。普通HTTP入口仍不自动OCR，不把选页预览成功算作已入库。


## OCR-3 后台解析与大文件（2026-10-05，OPT-065）

- [x] 原主库/CI备份与实际新增表迁移rag_ocr_jobs，旧业务/知识记录不变。
- [x] job/page持久化、scope/字节/路径校验、先提交后投递、周期补发/过期租约回收、每消息一页、旧owner fencing。
- [x] 独立ocr-worker并发1与beat，API/worker共享spool、真实版本/代码/tenant/hash缓存、取消/保留成功页/损坏恢复/续跑。
- [x] 本机已缓存MinerU服务隐藏启动/Bearer鉴权，Docker host访问；新后台上传代理129MiB/文件128MiB，旧10MiB入口不变。
- [x] 最新镜像部署、真实名词页取消续跑、87.3MB动词PDF代理202→completed/缓存命中；3个OCR任务/4个检查点有意保留，无knowledge/embedding。
- [x] 新增38项，632全量通过/含24真实集成，限定92.20% coverage、mypy/格式/lint/Compose/前端build/nginx-t/ready通过。
- [x] OCR-4（OPT-066）：上述工作台/原页与块/人工排除/审核付费门控已部署；完整8页36真实向量+2parent、8条页级检索与真实界面通过。复杂全量/事实金标仍待。
- [ ] 长期存储运营/完整故障演练/所有复杂布局与跨页表格/题组、GPU性能与许可分发仍待。
- 报告：`docs\现状文档\OCR-3后台任务与大文件验收.md`。


## OCR-4 与真实embedding（2026-10-05，OPT-066）

- [x] 现有ERP知识库接OCR工作台，文件/路径提交、进度/取消续跑、原页PNG、识别表格/定位、告警/问题块排除恢复、知识点与备注。
- [x] 审核/hash/config/费用/partial gate，稳定ID与知识+任务原子事务，失联付费不自动重试；source_reviewed不冒充事实认证。
- [x] 原主库/CI新head rag_ocr_review，备份/实际镜像部署、PDFium+Pillow容器真验、API/proxy ready。
- [x] 完整8页原并列句教材真实36×1024非零向量+2parent；1真实文档/38chunk保留；工作台审核→indexed→检索引用闭环，无pageerror。
- [x] 真实14次embedding/3642报告tokens；模型价格/实际账单未核验，trace仅配置估算，不报真实币种金额。
- [x] 单文档8条页级Hit@3=8/8/MRR1.0、canonical snapshot hashes/英文知识点alias scope通过，非全库事实质量证明。
- [x] 新增17项，649全量通过/25真实集成，限定89.96%coverage、mypy/格式/lint/前端build通过；原task/content/user不变。
- [ ] 其余教材批次及复杂跨页/题组/源冲突、全库检索金标、真实生成/judge/人工驳回率、批量/legacy重建与长期存储、许可/GPU验证仍待；OCR单文档重建/撤除已在OPT-067完成。
- 报告：`docs\现状文档\OCR-4工作台与真实RAG验收.md`。


## RAG P2：索引运营与多文档评测（2026-10-05，OPT-067）

- [x] G1有限范围：OCR来源同ID显式重建/expected revision、全向量校验后原子替换、失败保留旧索引、未知付费不自动重试。
- [x] 文档列表/资料撤除API及工作台、partial/stale/removed状态；禁止直接删parent，防删leaf后parent回注旧文，修复邻接链、并发/tenant校验及级联删除计数。
- [x] 旧审核标签/备注/排除恢复；checkbox费用声明不继承。名词源疑点block:15保留排除，不私自改事实。
- [x] 真实名词页2/16共12leaf、动词页1共4leaf；并列句完整8页同ID v1→v2/36leaf+2parent。主库3doc/54chunk=52真实向量+2parent，原业务计数不变。
- [x] G4有限范围：冻结18条多PDF gold与rag_eval.py；真实hybrid/top3最终页Hit17/18、MRR0.916667、去重NDCG0.923941、hash全通过；连系动词无scope漏召回/verbs对照保留，不报100%。
- [x] 本轮26次真实embedding、5146报告tokens；配置fallback估算0.010292/币种账单未验，无真实chat/judge/rerank。
- [x] 新增10条回归，最终659全量通过/26真实集成；6模块scoped strict mypy、修改Python格式/lint、TS/Vite/原镜像部署/ready/nginx/只读UI通过。本轮未重新认证全app覆盖率门槛。
- [x] 原无scope连系动词漏召回诊断及统一bigram策略A/B（OPT-068），原18条18命中；原失败保留历史。
- [ ] 跨页续例top5遗漏、多教材冲突/四类格式/复杂表格题组完整gold、真实生成事实质量。
- [ ] G1剩余：单租户有界批次/legacy与非OCR重建/历史回滚/长期存储；parser变化重OCR、规模性能/GPU/全面故障演练/分发许可。
- 报告：`docs\现状文档\RAG-P2索引运营与多文档评测.md`；私有材料留.local-eval，不自动撤除真实资料或清场。


## RAG P2：中文召回与结构化来源评测（2026-10-05，OPT-068）

- [x] 诊断正确动词表在vector第4、keyword0；有界中文bigram补召回，legacy回退/长问题采样/32项(2–64)配置，原scope/FTS/RRF不变；API/各worker/示例环境同源。
- [x] G3候选document/page/table身份与terms、不输出额外正文/密钥，rerank按实际ranked输出；v3页/精确块/同行/全部来源和逐单位失败，未知gold拒绝/run Trace限长/语料向量指纹漂移exit2。
- [x] 原18条同gold真实A/B17→18、新16条实现前冻结精确块14→16/上下文齐备13→16；表格10case/13tuple两策略均通过，不称表格准确率提高。
- [x] 两跨页探针检索前冻结：legacy0/2、bigram1/2上下文齐备；保留or选择/否则例句页3第11未进top5，修复partial命中被诊断为无失败的评测缺陷，原始报告保留/零付费离线重算。
- [x] 实际前端无scope两查询命中正确表，bigram/32/aliases默认/rerankoff，pageerror0；92真实embedding/944报告tokens，配置估算0.001888/币种账单未验，无chat/judge/rerank。
- [x] 新增23非集成+2真实PG，最终684通过/28集成；4模块scoped strict mypy、修改格式/lint、Compose透传/部署/ready通过；语料3doc/52向量+2parent及原业务计数不变/head不变。
- [x] 全app80%本地门槛在OPT-069达到80.57%，保留原阈值/准确app分母；config既有3处strict注解问题已修复，13模块检查不替代全appstrict或远端CI。
- [ ] 跨页复合问题的通用排序/上下文选择对照；独立专家/真实多教材冲突/四类格式完整集；单租户批次/legacy重建、存储/GPU/规模与真实生成事实质量。
- 报告：`docs\现状文档\RAG-P2中文召回修复与结构评测.md`。私有全文/日志/截图保留.local-eval，不自动清场或提交。


## RAG跨页/跨段/跨表/跨章方案（2026-10-05，STR-0）

- [x] STR-0：13份官方文档/论文/源码线索核验，策略比较、源/结构/索引/返回三层窗口、四类边界与负例、成本/许可/模型接口边界明确；这是设计，不是新代码发布。
- [x] 原主库只读拓扑审计：页2规则与页3续例直接邻接，但page/section不兼容；block:30 native header误提升heading使section丢失。本轮源/向量不变、embedding和DB写入0，未重跑684回归。
- [x] STR-1（OPT-069）：审核后blocks的文档级结构、重复页眉/真实标题区分、跨连续页小节继承、缺页/排除屏障、source_segments/逻辑unit关系纯函数和免费preview。
- [x] STR-2（OPT-069）：活动leaf上的accepted关系扩展/小块召回大块返回，seed与context预算/实际segment引用、API/前端/生成provenance/评测协议和旧路径回退；不能从原document正文回注已删内容。
- [x] STR-3（OPT-070）：正文-表-表注、逻辑续表/跨页cell/合并cell及审核；先复用页检查点，必要才2/3页局部重OCR，版本/缓存/全局页映射明确。
- [x] STR-4（OPT-070）：跨章explicit reference/概念导航和复合问题多证据覆盖，冲突来源分开；LLM子问题为后续有界选配，不自动扩大scope。
- [x] STR-5（OPT-071）：结构感知leaf/parent与多页source_segments、原生/OCR显式layout及选择性原子重建已落地；并列句v2→v3。默认legacy；44→43排序回归另列待修，不默认全目录重嵌入。
- [x] STR-6实验代码（OPT-071）：独立CLI/语义split/确定性及选配抽取式LLM上下文/真实token-pool本地适配器；三种真实embedding小实验完成且无收益，不接生产默认。
- [ ] STR-6真实效果补验：Late真实本地token权重forward、LLM-context真实chat/来源及费用兼容；现有Late dry-run/mock不等于效果通过。
- [ ] 拟建四类每类至少10条跨边界正/负例（40+不是已完成集），兼顾context_complete、FalseJoin/误扩率、provenance/表行映射、删除/权限0绕过、预算和性能。全app80%已在OPT-069通过，四类完整集与批量事实质量门槛仍待。
- 方案：`docs\现状文档\RAG跨边界知识组织与检索优化方案.md`。STR-1起实际实现依仓库规范追加OPT记录并回归；本轮纯文档设计不占代码OPT编号，私有审计保留.local-eval。


## STR-1/STR-2完整落地（OPT-069，2026-10-05 UTC）

- [x] 纯结构重建/版本policy/重复页眉与章编号别名、真实新章/缺页/排除/独立题目答案/工作表屏障，未知邻接proposed而非自动合并；63新非集成正反例覆盖。
- [x] 原文件/OCR免费结构预览、决定恢复/重置、计划hash费用前绑定；活动资料免费结构审核/角色tenant/expected revision/源签名与stale门控。
- [x] 活动leaf accepted关系扩展/主要单元选择/有界组件去重，seed优先/表头表行保护/多页分段hash与真实offset/bbox精度；不从原document补已删文字、不自动重嵌入。
- [x] v2 bundle/实际segments+v1回退、API/front/defaultrelation、生成参数副本/Trace/content provenance/人工审核逐段快照、v4实际引用评分同步。
- [x] 真PG新增4条/全量751(32集成)、app80.57%本地80%门槛、13模块strict mypy/21修改文件格式lint/TSVite/原镜像ready/nginx通过。
- [x] 36gold旧35/36→新36/36、新8未调参6/8→7/8；已带原页2+3续例，跨小节联合问题失败原样保留，不改gold或强制top_k。
- [x] 实际UI免费审核/旧策略切换/源段展示、名词block:15排除保留/pageerror0；source/leaf/indexrev/原业务不变，仅结构meta review rev2。
- [x] 181真实embedding/2037报告tokens，配置fallback估算0.004074/币种账单未验，无chat/judge/rerank/newOCR/index重嵌入。
- [x] STR-3续表/cell审核/多页复核和STR-4多证据在OPT-070交付；原for/because+so联合问题修复。
- [x] STR-5/STR-6实验代码在OPT-071后续交付（见末尾）；原始阶段未实施说明保留为历史。
- [ ] 真实跨页表格新教材金标、全库事实质量与长期规模运营仍待；Late/LLM真实效果另列待验。
- 报告：`docs\现状文档\RAG-STR1-STR2落地与验收.md`；私有现场.local-eval/str12有意保留，不自动提交或清场。


## STR-3/STR-4完整落地（OPT-070；记录2026-10-05 UTC）

- [x] 逻辑续表页序/几何frame/列数/确认表头/表号/原生hint多证据，未知proposed；逐cell列范围/spans审核、表先确认、不匹配rowspan保留unresolved、不自动整行merge。
- [x] 派生logical table header去重/每cell原来源parts和view hash，所有正文来自已送活动leaf；v5 required_logical_rows重算原cell内容防假ID/值；预算未送view不给支持分。
- [x] 新OcrBoundaryJob/独立队列任务+真实migration rag_str34_boundary；原主/CI已备份，2/3页连续范围/协议映射、缓存版本代码hash、fencing/取消续跑/租约回收/次数封顶、result鉴权hash源版本，原成功页/索引不写回。
- [x] 真实原并列句页2/3与2/3/4本地MinerU4.0.10完成，前端声明门控/result/结构页可看/pageerror0；窗口无表格，不以此认证真实续表效果。
- [x] 同文档明确章节引用有向定位、缺目标/多义unresolved、scope不能越界；rules互补问题/原query/alias概念导航、coverage seeds与公平预算、LLM选配.st/schema/Trace失败回规则，不默认chat。
- [x] 原36+旧8gold同指纹off/rules对照：36始终全齐备、8从7→8；原for/because+so两小节失败实修，无新独立专家集/全库正确率声明。
- [x] 新增56单测+4真PG，最终811/36集成、app7448/9124=81.63%/cov-fail-under80通过，20修改文件格式lint/14模块strict mypy/TSVite/原镜像ready/nginx。
- [x] 178真实embedding/2140 reported tokens，fallback估算0.00428/币种账单未核验，无chat/judge/rerank/重嵌入；Doc3/52leaf向量+2parent及原业务不变，新增2复核任务有意保留。
- [x] STR-5/STR-6实验代码已在OPT-071交付；详见下方最新记录。
- [ ] 更多真实跨页表/cell/合并cell与双盲事实集、全格式压力/恢复/GPU/存储治理/真实生成人工驳回率。旧独立8本轮为回归不继续称新盲测。
- 报告：`docs\现状文档\RAG-STR3-STR4落地与验收.md`；私有备份/原始全文/截图.local-eval/str34不提交、不自动清场。


## STR-5结构切分与STR-6独立实验（OPT-071；2026-10-06 Asia/Shanghai）

- [x] `rag-chunker-structure-v1`跨页/段落逻辑leaf，保护新章/缺页/排除/题组/表格/代码；表行超预算显式告警。父块段映射/overlap去重，不读回页眉。
- [x] `source_segments`源/leaf坐标及hash逐段校验，非连续正文顶层range=None、envelope只导航；eligible正文覆盖分母/导航排除公开，不冒充整源每字符都嵌入。
- [x] 原生upload/免费preview和OCRplan/approval/worker的layout透传与hash门控；失效计划付费前拒绝、原子失败保旧，前端选项与计划同步。
- [x] 原环境/备份/真实显式重建：并列句同ID v2→v3，54leaf+1parent/2跨页leaf；其他来源未重嵌入，主库3资料/70非零1024维向量+1parent。原源/hash及task3/content4/user1、OCRjob4/page12/boundaryjob2未改，head仍rag_str34_boundary。
- [x] 重建前冻结旧chunk gold到immutable physical source_block_ids，query/pages/terms不改；v6评测诊断身份与实际交付分开。真实44题来源齐备44→43，leaf/向量变化不称同向量A/B；误归召回的旧诊断离线重评分修正为context_or_top_k，零额外调用。
- [x] STR-6 CLI默认dry-run/显式费用/输入与句数/token预算、同scope读源、仅报告和Trace不改索引；模型/源/gold/输入hash可追踪。
- [x] 语义单prose实验/确定性上下文prefix/选配严格抽取式LLM独立prompt/schema/timeout/Trace；Late显式local-only safetensors/fast offset模型适配、token先forward后按精确源段pool，不模拟云API最终向量。
- [x] 自编prose同源3探针真实embedding：structure2chunk/2批、semantic4chunk/8句/3批、contextual2chunk/2批，均3/3来源支持，**无已测改善**；Late仅dry-run。
- [x] 最新全量871通过（新增57单元+3真PG；共39integration），app82.2946%门槛通过；26文件black/isort/flake8、18源文件scoped strict（follow-imports=silent）和前端tsc/Vite通过。展开依赖strict仍185处/25文件，不宣称全app通过。原环境部署/实际网页重建和最终查询/免费预览无pageerror。
- [x] 本轮103真实embedding/5532 reported tokens，fallback估算0.011064，币种及账单未核验；无真实chat/judge/rerank、新OCR或全库重嵌入。
- [x] `holdout-or-consequence`当前来源缺口已按用户预算决策在OPT-072交付：默认Top-5/可选8实际返回正确例句，44/44来源齐备。vector1/keyword13/RRF4未变，Top-3限制仍可复现；不称排序算法修复，默认layout保持legacy。
- [ ] Late真实本地token模型质量/延迟/长文能力、LLM上下文真实兼容/来源/费用；无真实模型forward不得标效果完成。
- [ ] 四类每类至少10条的新完整正反例/专家事实金标、跨页table/cell、全格式负载恢复及真实批量生成事实正确性。
- 验收：`docs\现状文档\RAG-STR5-STR6落地与验收.md`；私有证据`.local-eval/str56`保留，不自动清库/提交/清场。


## RAG返回与上下文预算（OPT-072；2026-10-06）

- [x] 按用户要求默认Top-5、网页可选Top-8；Python/env.example/Compose及本机私有env同源，省略API参数时请求期继承当前配置，不冻结路由导入默认。生成前上下文也继承默认5。
- [x] 按用户后续反馈将应用字符预算8192→32768；不是模型token窗口，不改模型名/档案。显式字节保护、来源/tenant/去重/segment/member/hop限制不放宽。网页显示实际请求/候选池/预算，reload默认5。
- [x] v7 evaluator显式`--top-k`控制运行/评分预算，原gold bytes/query/source/pages/terms不改；历史41×top3+3×top5与统一3/5/8及最终32K结果分开报告。
- [x] 同源/同向量/同lane及融合rank-score：统一top3=43/44，top5/top8在8192与最终32768预算均44/44来源齐备；正确Hurry up块仍融合4。扩大返回预算补回，不报算法优化。原44集最大4230字符，不能报扩大字符预算的质量收益。
- [x] 895完整回归通过（新增21单元+3真PG；共42integration），app82.3050%门禁；8选定文件black/isort/flake8、4源文件scoped strict和前端构建通过，非全app strict/远端CI认证。长正文单测/PG验证超过8192且不越32768，显式字节保护仍生效。
- [x] 原环境部署/最终网页API默认参数省略、Top-8选择/正确来源/预算诊断无pageerror；head仍rag_str34_boundary，主库3doc/70向量+1parent、业务及OCR计数不变，无重嵌入/OCR/迁移。
- [x] 本轮268真实embedding/3262reported tokens，fallback估算0.006524/币种账单未核验，无真实chat/judge/rerank/Late forward。当前模型档案仍deepseek-v4-flash；官方窗口 metadata核对不等于转发端点实际1M请求验证。
- [ ] Late真实本地token模型、LLM-context真实chat、完整新教材四类专家事实/表格cell金标、长上下文噪声与真实生成成本/质量、规模稳定性继续单列待验。当前原金标为回归，不冒充新盲测。
- 验收：`docs\现状文档\RAG-Top5-Top8与上下文预算验收.md`；私有证据`.local-eval/str57`保留，不自动提交/清库/清场。


## 主RAG链路切换与真实生成验收（OPT-073；2026-10-06）

- [x] 默认链路切换：新入库默认`RAG_CHUNK_LAYOUT=structure`、`RAG_CONTEXT_MODE=relation`；Top-5默认/Top-8可选/32768字符；legacy显式回退保留，读取旧OCR索引优先使用其保存layout。
- [x] 存量选择性迁移：名词同ID v1→v2，23leaf，仍第2/16页且`block:15`排除；动词同ID v1→v2，4leaf，仍第1页；并列句v3不动。3资料/82chunk=81非零leaf+1parent，源hash未变，无OCR/全书扩展。
- [x] 三内置题型version2配置`rag.mode=required`+`require_human_verification=true`；无来源反例chat调用0并以`RAG_CONTEXT_REQUIRED`失败；正向来源实际注入generate Trace。
- [x] 真实小批量：single_choice2/cloze2/reading2完成生成与自动QC，全部`pending_qc`；QC 87.70～91.75；1次JSON失败重试后成功；无人工通过/发布。
- [x] 人工质检页显示真实segment/page/block，segment ID唯一；未勾选来源核验时通过按钮禁用，后端核验返回409；pending内容发布返回409；预览去除重复选项标签但不改payload。
- [x] 开发复核发现完形样本正确项集中A/潜在多解，自动QC分不是发布许可；新增题型质量回归/人工金标待办。
- [x] 911完整回归通过（42真PG），app82.34%；前端构建、部署、真实默认预览/迁移/UI质检通过。
- [x] 118 Trace：99 embedding、7 generate（含1失败重试）、12 qc；47938 prompt+37200 completion tokens，估算0.170276未核账；无真实rerank/Late/LLM-context/OCR。
- [ ] 6条人工决定通过/驳回/修改；每题型扩至20～30条真实金标，统计人工驳回率、来源一致性、答案唯一性、选项同质性、成本/延迟。
- [ ] 完形候选质量规则/结构校验：避免正确项偏置、潜在多解、题干与选项不一致；不能仅靠LLM QC分数。
- [ ] Late真实模型、LLM context真实调用、专家table/cell金标、长上下文噪声与无人值守发布门槛继续单列。
- 验收：`docs\现状文档\RAG-主链路切换与真实生成验收.md`；证据`.local-eval/str58`保留，不自动清场/提交。


## 维护者文档体系（2026-10-06；纯文档，不新增OPT编号）

- [x] 在新目录`docs/现状文档2`完成可独立阅读的主文档：项目定位/全景架构/两条业务链路/页面API/数据状态/核心机制/部署维护/验证风险/扩展入口/源码与证据索引；本轮只读核对源码、运行态与测试收集，未重新生成或改业务代码。
- [x] 标明实际45条integration、866非集成的收集口径，纠正历史摘要42条沿用计数；说明模型档案同模型、样本尚未自动few-shot、状态命名债、Trace截断、Outbox补偿及网页数量口径静态风险。
- [x] 数量口径风险已在OPT-074修复：全链外层N/单item1、多输出拒绝；真页面拦截和真PG五项流程回归通过。
- [x] OPT-074完成Outbox父/子周期投递、fencing/恢复、显式死信目标投递、Redis命名卷/AOF与SQL消息丢失对账。
- [ ] 状态常量/worker/UI词汇统一、并发请求去重和题型质量仍待，不归入本次前三项修复。
- [x] 专题：[01-内容生成与质量闭环](现状文档2/01-内容生成与质量闭环.md)；源码、契约、失败路径、维护/验证入口已展开，本轮纯文档不改业务。
- [x] 专题：[02-文档处理与RAG](现状文档2/02-文档处理与RAG.md)；源码、契约、失败路径、维护/验证入口已展开，本轮纯文档不改业务。
- [x] 专题：[03-数据模型与状态机](现状文档2/03-数据模型与状态机.md)；源码、契约、失败路径、维护/验证入口已展开，本轮纯文档不改业务。
- [x] 专题：[04-任务执行与可靠性](现状文档2/04-任务执行与可靠性.md)；源码、契约、失败路径、维护/验证入口已展开，本轮纯文档不改业务。
- [x] 专题：[05-部署运维与验证](现状文档2/05-部署运维与验证.md)；源码、契约、失败路径、维护/验证入口已展开，本轮纯文档不改业务。
- 主文档：[英语内容生产工作台：当前实现与系统架构](现状文档2/英语内容生产工作台-当前实现与系统架构.md)。旧现状/验收和私有证据保留，不自动清场。

- [x] 五专题与主文档互链、绝对源码/证据链接和Markdown结构校验；原报告与私有证据保留，不新增OPT，不跑真实模型或变更数据。


### 历史与证据层（2026-10-06；纯文档，不新增OPT）

- [x] [历史与证据索引](现状文档2/历史与证据/00-历史与证据索引.md)：按材料类型/阶段/问题导航，区分公开说明、受限底稿和未定位原始日志。
- [x] [阶段演进与决策记录](现状文档2/历史与证据/01-阶段演进与决策记录.md)：OPT001～073原日期/源行索引、关键选择与后续变化；UTC/本机跨日日期并列，不倒签历史ADR。
- [x] [验收矩阵与失败追踪](现状文档2/历史与证据/02-验收矩阵与失败追踪.md)：限定/app coverage、真实PG/mock/provider/人工范围分开；保留44→43、Top-K预算补回、JSON重试、坏题和计数更正。
- [x] 证据资产清单（本机私有证据，不随公开仓库分发）：341原位文件（41仓库资料、300受限本机证据）及73OPT章节，路径/大小/SHA-256；未复制正文、未移动/删除旧文件、未重跑模型或业务。
- [x] 主文档/运维专题/README已链接历史层；仅文件可用性/hash核对不等于重新验收。数量/Outbox/Redis/状态/专家质量等风险仍未修复。
- [x] 历史层机械校验：73OPT源行、341文件SHA-256、现状文档2共9篇Markdown/388个绝对本地链接；交叉核对既有5阶段gate和44→43/Top-K/实验底稿，不重执行测试。


## 10月6日紧急优化1—2—3（OPT-074）

- [x] 顺序落地数量N/1与多对象输出防丢；父/子持久意图、PG有界并发领取/lease fencing、周期maintenance与显式死信投递；Redis无损命名卷迁移/AOF everysec及消息丢失SQL对账。
- [x] 955全量/50真PG、app82.6211%，21文件lint/4源scoped strict，前端构建、页面拦截、周期worker/Redis重启/专用消息丢失演练通过；无真实模型费用、历史业务计数保持。
- [x] main/CI迁移head opt074_delivery_leases，原卷/RDB与DB备份保留、未清真实队列、未重派历史失败；现役文档已同步。
- [ ] 更大真实provider批次质量/故障全覆盖、并发请求防重、状态词与题型确定性/语义校验另项推进。
- 验收：[1—2—3落地](现状文档/10-6优化1-2-3落地与验收.md)；原风险文档与历史hash保留，不自动commit/清场。


### 10-6优化第四点（OPT-075，2026-10-06）

- [x] 三题型v3 Schema＋配置驱动四选项/字母答案/选项非空不重复、完形索引与空数；Prompt/Skill区分篇数和空数。
- [x] 生成有限重试/反馈/Trace失败费用；Graph兜底与有界invalid改版，QC缓存/落库/直接人工恢复不绕过。
- [x] 人工通过/发布复查；独立JSONB诊断和API只读现役诊断、前端告警/硬失败按钮门禁，旧题不自动改写。
- [x] 新增89非集成＋5真PG测试，1049全量/app83.1018% gate；前端build和5实页场景；现有环境迁移/增量部署/源码hash核对。
- [x] 原6样本只读诊断、真实失败结构复现＋合成语义待核验反例；主库业务计数/旧正文状态分数指纹不变，0真实模型调用。
- [ ] 6条语义人工裁决、每题型20～30条专家金标及人工驳回率（真实待办，不能由格式通过或模型高分替代）。
- [ ] 镜像源403恢复后验证常规全新依赖安装构建；当前是复用现有依赖层的现役增量部署。
- 验收：[10-6优化第四点落地与验收](现状文档/10-6优化第四点落地与验收.md)；证据`.local-eval/opt075`保留，不自动清场。


### 第二轮P1与模型设置（OPT-076，2026-10-06）
- [x] 共享状态映射/父聚合、未知Graph失败关闭、待人工不当终态/不重派、取消不复活；SQL/API状态契约与前端0～1进度正确展示。
- [x] 活动request_hash SQL局部唯一，task＋Outbox事务，6真PG连接竞争只1成功，终态可重做、旧NULL兼容；重放同hash活动目标保护。
- [x] 管理员模型设置页：自定义OpenAI兼容Provider/模型、按题型生成和Judge覆盖、ENV兼容；配置独立持久化不被YAML同步抹掉。
- [x] 专用Fernet key/加密凭据只写不读，权限、审计/Trace安全身份、错误不回显；真实API/SDK MockTransport与UI拦截验证。
- [x] 1124全量（1065非集成＋59真PG；新增71＋4），app83.8342% gate，23格式/lint、3源码scoped strict、frontend build；现有环境迁移/部署/key与码hash一致。
- [ ] 用户配置真实独立模型/Judge候选后的同金标效果/成本比较（当前三档未自动替换）。
- [ ] 第三批：受限完整Trace快照、样本few-shot；专家语义裁决仍真实待办，不由自动高分替代。
- 验收：[第二轮与模型设置](现状文档/10-6优化第二轮与模型设置落地验收.md)，受限证据`.local-eval/opt076`保留，无自动commit/清场。


### 第三轮工程能力（OPT-077，2026-10-06）
- [x] 有界加密完整脱敏chat快照＋摘要分离，调用失败也封存，独立key/额度/TTL/scope/no-store/成功访问审计；旧Prompt不补造。
- [x] 安全few-shot实际生成接入，整例、scope/human/current source/hash/规则/精确标签/目标批次保护，reserved参数防伪，输出逐字复制拒绝；样本页用途切换。
- [x] 已配置模型plan与显式A/B CLI，共享输入、交替顺序、失败/费用/来源成本记录，不自动切模型/入库/伪造专家结论。
- [x] 1154全量（1091非集成/63真PG；新增26＋4），app84.0498%门槛通过，24格式/lint/3 scoped strict/front build/实页；现有环境迁移/开关/key/码一致。
- [x] 原计数/旧正文状态分数不变，当前model plan同实际模型未执行；准备3比较输入与10candidate/零人工标注。
- [ ] 真实不同候选配置后的收费A/B及专家裁决；当前Provider/样本库仍0，不能拿工程通过当模型/示例收益。
- [ ] 人工双标、争议仲裁、人工驳回率与few-shot真实收益，不自动代签human_attested。
- 验收：[第三轮](现状文档/10-6优化第三轮落地与验收.md)，受限证据`.local-eval/opt077`保留。


### 前端UI/UX首轮（OPT-078，2026-10-06）
- [x] 单色工作台外壳/4组权限导航/当前位置/用户角色、手机抽屉/焦点/Escape；token变更/401与角色落点反馈。
- [x] 登录与生成页重构：schema字段wire不变，中文/帮助/任务摘要/inline校验/加载/防重复，N/1契约保持。
- [x] 任务页状态/百分比/当前页过滤/空态/骨架/活动5秒刷新暂停，接已有合作式取消确认，不假标终态；任务dialog键盘/焦点/滚动。
- [x] tsc/Vite、14实页组/12移动路/0pageerror；请求写入mock，后端1091 passed/63 PG skipped。只frontend部署，原库和模型不变。
- [ ] 按真实使用反馈进一步优化质检、内容库、知识/OCR、模型设置等复杂业务流程；首轮不宣称全页面深度重做或大规模性能认证。
- 报告：[UIUX首轮](现状文档/10-6前端UIUX首轮优化与验收.md)；受限`.local-eval/opt078`保留，无自动commit/清场。


### 公开发布与远端整合（OPT-079，2026-10-06）
- [x] 公开候选排除env/独立密钥/备份/登录态/教材blobs与截图/私有资产清单，文档路径去个人化；真实凭据精确匹配与Gitleaks。
- [x] 保留远端4提交，现役迁移与m3_04_task_user_id合流；旧契约测试迁移，不恢复退役接口/权限或泄露异常正文。
- [x] 1173全量（1110非集成/63真PG）、app92.8945%发布口径、跟踪源码格式/lint/前端build；主库未迁移或部署。
- [ ] GitHub远端推送后确认提交和CI状态；公开文档不包含本机完整证据。
- [公开发布说明](公开发布-2026-10-06.md)，私有审计底稿留ignored目录。


### 前端CI安全审计修复（OPT-080，2026-10-07）
- [x] 复现Audit步骤axios/source-map-js两项high，最低声明与lock修到1.20.0/1.2.2，不force或降低audit级别。
- [x] 官方checkout/setup-node/setup-python v6=node24核对，项目Node22/Python3.12/安全与覆盖率门槛保持。
- [x] 干净npm ci、audit0漏洞、tsc/Vite、四新bundle浏览器mock、三新增契约测试；本地1113通过/63 PG跳过（Docker未运行）。
- [ ] 推送后核对GitHub本提交的实际四任务结果，不用本地build宣称远端audit已通过。
- 本轮不提交env/密钥/教材/本机证据、不启动或迁移部署、不调用真实provider。
