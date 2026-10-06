# AGENTS.md —— 英语教研 AI 内容生成平台 2.0

## 约束权威来源（接手必读）
本项目的全部工程约束、架构约定、编码规范、LLM 调用标准与变更追踪要求，**以仓库内文档为唯一权威来源**：
- `AGENTS.md`（项目定位 / 技术栈 / 结构 / 核心规范 / 禁止事项）
- `CLAUDE.md`（行为准则：Think Before Coding / Simplicity / Surgical / Goal-Driven）
- `.ai/rules/coding.md`（编码规范）、`architecture.md`（架构设计）、`llm_calls.md`（LLM 调用标准化）、`change_tracking.md`（变更追踪）

外部共享记忆（如 project_memory 等）**仅为辅助索引，不作为约束依据**；一旦与仓库文档冲突，一律以仓库文档为准。接手本项目时，据此文档 + `.ai/rules/` 恢复上下文，不要引用任何外部记忆里的旧约束。

## 角色
你是一名英语教研 AI 内容生成平台的全栈开发工程师，负责基于 LangGraph 编排流水线的选题→生成→校验→质检→改版→入库→发布全链路开发与维护。

## 项目定位
把「大模型能力」封装为可编排、可评估、可持续优化的 AI 原生内容生产线。题型以 Schema 配置接入，新增题型不改代码；全链路可观测、成本可核算；个人可维护，为对外化预留扩展点。

## 技术栈
- 编排层：LangGraph（生成→校验→质检→改版→入库 状态机，checkpointer 断点续跑）
- 后端：Python 3.12 + FastAPI（异步 API）
- 结构化输出：OpenAI 兼容客户端 + Pydantic v2 强制 JSON 二次校验（等效 Outlines 约束解码）
- 任务队列：Celery 5 + Redis（并发/重试/断点续跑）
- 模型推理：云 API（Qwen/DeepSeek/GLM，OpenAI 兼容），主模型档案→默认模型档案降级
- 数据库：PostgreSQL 16 + pgvector（JSONB 字段 + 向量检索）
- RAG：阿里云百炼 text-embedding-v3 + pgvector 向量检索
- 可观测性：自研 TraceLog 表（trace_id/model/cost/latency 入库），Langfuse 可选接入
- 前端：React 18 + Vite + TypeScript
- 部署：Docker Compose

## 项目结构
```
english-edit/
├── backend/
│   ├── app/
│   │   ├── api/            # FastAPI 路由（生成/任务/质检/内容/成本/样本/知识库/看板/链路/通知/用户/认证/校准）
│   │   ├── workflow/       # LangGraph 图定义与状态（graph.py）
│   │   ├── engine/         # 结构化输出/模型路由/LLM-judge 质检/trace
│   │   ├── rag/            # 知识库 embedding/indexer/retriever/parser
│   │   ├── worker/         # Celery 任务定义
│   │   ├── templates/      # 题型模板 YAML（single_choice/cloze/reading）
│   │   ├── config.py       # 全局配置（Pydantic Settings）
│   │   ├── database.py     # 连接与会话
│   │   ├── models.py       # SQLAlchemy 模型
│   │   ├── schemas.py      # Pydantic 出入参
│   │   ├── calibration.py  # 质检权重反向校准（J1 质量闭环）
│   │   ├── sample_pool.py  # 高质量样本沉淀服务（数据回流）
│   │   ├── notification.py # 站内通知服务
│   │   ├── security.py     # JWT 认证 + 权限矩阵
│   │   ├── seed.py         # 种子管理员启动
│   │   ├── template_loader.py  # 模板加载入库
│   │   ├── prompt_loader.py    # .st prompt 加载渲染
│   │   ├── skill_registry.py   # SKILL.md 注册表
│   │   ├── dedup.py            # 重复提交去重
│   │   └── errors.py           # 统一异常体系
│   ├── prompts/            # 独立 prompt .st 文件（system/user 分离）
│   ├── skills/             # 出题技能 SKILL.md + skill.meta.yml
│   └── alembic/            # 数据库迁移
├── frontend/               # React 工作台（生成/任务/质检/内容库/样本库/看板/成本/链路/知识库/消息/用户管理/登录）
├── deploy/                 # docker-compose 编排
└── docs/                   # PRD/技术架构/任务清单/优化记录
```

## 核心规范
- **分层与解耦**：每层只负责自己的事；RAG 知识上下文与生成参数分开注入，不混塞。
- **配置驱动**：题型、质检规则、模型路由均为 YAML/Schema 配置，不硬编码；新增题型=新增 YAML + .st prompt + SKILL.md，不改代码。
- **结构化输出服务端校验**：模型输出一律经 Pydantic 二次校验，重试耗尽标记失败，不直接信任模型。
- **可观测性内建**：每次调用带 `trace_id`，记录 prompt 版本、模型、输入输出、耗时、成本。
- **渐进式披露**：顶层配置/规则简洁，细节放子文档（.ai/rules/）。
- **Prompt 独立文件 + Skill 架构**：提示词独立存 `backend/prompts/*.st`（system/user 分离），出题规范存 `backend/skills/<id>/SKILL.md`，版本化、可独立调试，不写死在代码里。
- **安全与校验用代码执行**：权限/脱敏/审计由代码与基础设施强制，不依赖 Prompt。
- **前端设计规范（单色调简约）**：浅灰背景 `#F7F8FA` + 纯白卡片 `#FFFFFF` + 唯一深青强调色 `#0A8F75`，无多色拼接；布局仿 ERP（左侧导航 + 右侧内容）；新增页面复用 `App.css` 既有设计令牌，不另起配色。
- **变更追踪**：每次代码修改（Bug 修复 / 优化 / 新功能 / 重构 / 配置）须在 `docs/优化记录.md` 追加 `OPT-0XX` 记录（含根因 / 改动 / 验证 / 量化对比），对应任务同步更新 `docs/tasks.md`；规范见 `.ai/rules/change_tracking.md`。
- **测试**：后端测试在 `backend/` 下运行。纯单测：`pytest -m "not integration" -o addopts="-q"`；全量（含集成测试，需真 Postgres：`TEST_DATABASE_URL=postgresql+psycopg2://... pytest`，本机可用 docker 跑 `pgvector/pgvector:pg16`）；CI 在全量运行上强制覆盖率门槛 80%（`pytest --cov-fail-under=80`，见 `.github/workflows/backend-ci.yml`）。未装 pytest-cov 的环境可用 `-o addopts=""` 跳过默认 coverage 选项。

## 禁止事项
- 禁止裸 `except:` —— 用自定义异常或精确异常类型捕获。
- 禁止硬编码模型名 / 数据库串 / API Key 于代码 —— 一律走 `config.py` 环境变量。
- 禁止 Pydantic 字段使用 `_meta`（与内部冲突）—— 用 `meta`。
- 禁止把结构化输出校验、权限控制只写在 Prompt 里 —— 必须由代码兜底校验。
- 禁止不经测试合并改动 —— 涉及工作流/结构化输出/质检逻辑的改动需单测。

## 成功指标
- 新增题型不改核心代码，需新增 YAML 模板及对应 .st Prompt、SKILL.md。
- 单次生成凭 `trace_id` 可完整回放（输入/输出/质检分/成本）。
- 自动质检通过率稳定，人工驳回率可控（P2 目标 ≤5%）。
- 后端单测覆盖率 ≥80%（重点：结构化输出、质检加权、路由降级、状态机）。

## 详细规范
见 `.ai/rules/coding.md`（编码规范）与 `.ai/rules/architecture.md`（架构设计），按需加载。
