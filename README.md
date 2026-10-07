# 英语教研 AI 内容生成平台 2.0

维护者文档见 [当前实现与系统架构](docs/docs_public/英语内容生产工作台-当前实现与系统架构.md)，覆盖系统架构、两条业务链路、前后端能力、RAG、任务可靠性、部署维护与验证边界。

公开仓库的 `docs/` 只分发 `docs_public/`。其余设计稿、专题、优化记录、历史验收报告和私有证据仅在维护者本机保留，不随公开版本上传。

基于 LangGraph 编排的 AI 原生内容生产线，将大模型能力封装为**选题 → 生成 → 校验 → 质检 → 改版 → 入库 → 发布**的全链路，面向英语教研场景生成高可用的题目内容（单选 / 完形 / 阅读）。

题型以 Schema 配置接入，**新增题型不改代码**；全链路可观测、成本可核算；支持个人维护，并为对外化预留扩展点。

---

## 特性

- **题型配置化**：单选 / 完形 / 阅读三类题型以 YAML 模板接入，新增题型只需新增 `templates/*.yaml` + `.st` prompt + SKILL.md
- **LangGraph 状态机**：生成 → 校验 → 质检 → 改版 → 入库 全链路，checkpointer 断点续跑
- **结构化输出服务端校验**：Pydantic v2 二次校验 + 重试降级，不直接信任模型返回
- **模型路由降级**：按题型×难度选择主模型档案，失败时尝试默认档案
- **LLM-as-judge 质检**：按 quality_rules 逐维度加权打分，rubric 结构化注入；可配置多轮均值，默认 1 轮
- **RAG 知识库**：教材 / 课标 / 真题分块入 pgvector，按知识点检索注入生成上下文；支持前端上传教研文档（txt/md/docx/pdf，如试卷、练习册）自动解析索引
- **高质量样本数据回流**：人工通过 / 已发布内容沉淀为 few-shot / 微调语料，可检索、可 JSONL 导出
- **深度成本报表**：token 细分（输入 / 输出）+ 生成 / 质检阶段拆分 + 题型 / 模型 / 任务多维聚合 + 单条调用下钻
- **指标看板**：产出量、质检通过率、人工驳回率、生产周期、单条成本等 KPI
- **质检权重反向校准**：用自动高分但人工驳回的样本调整 judge 维度权重；质量收益需用独立标注集验证
- **Trace 链路回放**：凭 trace_id 查看已入库的调用与生命周期记录（输入 / 输出 / 耗时 / 成本）
- **JWT 认证与权限**：多角色（admin/researcher/reviewer/viewer）+ 种子管理员 + 路由守卫
- **任务队列**：Celery + Redis 批量生成、并发控制、进度与状态流转、站内通知

---

## 技术栈

| 层 | 技术 |
|---|---|
| 编排层 | LangGraph（生成→质检→入库状态机，checkpointer） |
| 后端 | Python 3.12 + FastAPI（异步 API） |
| 结构化输出 | OpenAI 兼容客户端 + Pydantic v2 强制 JSON 二次校验 |
| 任务队列 | Celery 5 + Redis |
| 模型推理 | 云端 OpenAI 兼容 API，主模型档案失败时尝试默认档案 |
| 数据库 | PostgreSQL 16 + pgvector（JSONB + 向量检索） |
| RAG | 阿里云百炼 text-embedding-v3 + pgvector |
| 可观测性 | 自研 TraceLog 表（trace_id / model / cost / token / latency），Langfuse 可选接入 |
| 前端 | React 18 + Vite + TypeScript |
| 部署 | Docker Compose |

---

## 目录结构

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
│   │   ├── models.py       # SQLAlchemy 模型
│   │   ├── schemas.py      # Pydantic 出入参
│   │   ├── calibration.py  # 质检权重反向校准（J1 质量闭环）
│   │   ├── sample_pool.py  # 高质量样本沉淀服务（数据回流）
│   │   ├── notification.py # 站内通知服务
│   │   ├── security.py     # JWT 认证 + 权限矩阵
│   │   ├── seed.py         # 种子管理员启动
│   │   ├── template_loader.py / prompt_loader.py / skill_registry.py
│   ├── prompts/            # 独立 prompt .st 文件（system/user 分离）
│   ├── skills/             # 出题技能 SKILL.md + skill.meta.yml
│   └── alembic/            # 数据库迁移
├── frontend/               # React 工作台（生成/任务/质检/内容库/样本库/看板/成本/链路/知识库/消息/用户管理/登录）
├── deploy/
│   └── docker-compose.yml  # 全栈编排
└── docs/docs_public/       # 公开维护者文档（其他 docs 仅本机保留）
```

---

## 快速开始（Docker）

### 1. 前置条件

- Docker Desktop（含 Docker Compose）
- 一个 OpenAI 兼容的云端 LLM API（如 DeepSeek / 通义 / GLM）
- Compose 启动需提供非占位 embedding API Key；实际 RAG 检索还需可用的 embedding 服务

### 2. 配置环境变量

在项目根目录创建 `.env`（建议从 `.env.example` 复制）。Compose 会对数据库密码、JWT、管理员密码、LLM/Embedding Key 和 Langfuse secret 做启动前必填校验；不要把示例占位值直接用于生产。

```env
# 必填：LLM API
LLM_API_BASE=https://api.deepseek.com/v1
LLM_API_KEY=你的密钥
LLM_MODEL_NAME=deepseek-v4-flash

# 生产必填：数据库 / JWT / 管理员 / Langfuse secret
POSTGRES_PASSWORD=替换为随机数据库密码
JWT_SECRET=替换为至少 32 字节随机值
SEED_ADMIN_PASSWORD=替换为随机管理员密码
LANGFUSE_NEXTAUTH_SECRET=替换为随机值
LANGFUSE_SALT=替换为随机值
LANGFUSE_ENCRYPTION_KEY=替换为 32 字节密钥
ENVIRONMENT=production
CHECKPOINTER_BACKEND=postgres
ALLOW_MEMORY_CHECKPOINTER=false

# Compose 必填：RAG embedding（示例为阿里云百炼 text-embedding-v3）
EMBEDDING_API_BASE=https://ws-xxx.maas.aliyuncs.com/compatible-mode/v1
EMBEDDING_API_KEY=你的密钥

# 可选：Langfuse 可观测性
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
```

### 3. 启动全栈

```bash
docker compose --env-file .env -f deploy/docker-compose.yml up -d --build
```

服务启动后：

| 服务 | 地址 |
|---|---|
| 前端工作台 | http://localhost:3000 |
| 后端 API | http://localhost:8000 |
| Langfuse | http://localhost:3001 |

后端容器启动时自动执行 `alembic upgrade head` 迁移建表。

生产启动若缺少上述必填变量会在 Compose 插值阶段失败；即使配置了变量，`ENVIRONMENT=production` 仍会拒绝默认 JWT secret、默认管理员密码和缺失的 LLM/Embedding Key。

### 4. 常用命令

```bash
# 查看状态
docker compose --env-file .env -f deploy/docker-compose.yml ps

# 查看后端日志
docker compose --env-file .env -f deploy/docker-compose.yml logs -f backend

# 停止
docker compose --env-file .env -f deploy/docker-compose.yml down

# 停止并清理数据卷（重置数据）
docker compose --env-file .env -f deploy/docker-compose.yml down -v
```

---

## 本地开发

### 后端

```bash
cd backend
pip install -r requirements.txt
# 准备 PostgreSQL（本机或 Docker 起一个 pgvector 实例）
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

运行测试：

```bash
cd backend
python -m pytest -q -o addopts=""
```

> 说明：本机若未安装 pytest-cov，需用 `-o addopts=""` 跳过默认 coverage 选项。

### 前端

本地前端构建要求 Node.js 22+（Vite 8）。

```bash
cd frontend
npm install
npm run dev        # 默认 5173，经 Vite 代理转发 /api 到 8000
```

前端生产构建（含类型检查）：

```bash
cd frontend
npm run build
```

---

## 配置项（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg2://.../english_edit` | 数据库连接串 |
| `REDIS_URL` | `redis://localhost:6379/0` | 任务队列 / 缓存 |
| `LLM_API_BASE` | `https://api.openai.com/v1` | LLM API 地址 |
| `LLM_API_KEY` | 空 | LLM API 密钥 |
| `LLM_MODEL_NAME` | `deepseek-v4-flash` | 默认模型名 |
| `LLM_TIMEOUT` | `120` | 单次 LLM 请求超时（秒） |
| `COST_PER_1K_TOKENS` | `0.002` | 成本估算单价（每千 token） |
| `JUDGE_SAMPLE_ROUNDS` | `1` | judge 采样轮数（降噪可调高） |
| `QUALITY_THRESHOLD` | `70.0` | 质检通过阈值 |
| `EMBEDDING_API_BASE` / `_KEY` / `_MODEL_NAME` / `_DIM` | 阿里云百炼默认 | RAG embedding 配置 |
| `LANGFUSE_HOST` / `PUBLIC_KEY` / `SECRET_KEY` | 空 | Langfuse 可观测性（空则关闭） |

---

## 核心接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/login` | 登录获取 JWT |
| POST | `/api/generate` | 发起生成任务 |
| GET | `/api/tasks` / `/api/tasks/{id}` | 任务列表 / 进度 |
| GET | `/api/templates` | 题型模板列表 |
| GET | `/api/contents` | 内容库检索 |
| GET | `/api/quality` / POST `/api/quality/{id}/review` | 质检记录 / 人工标注 |
| POST | `/api/quality/calibrate` | 触发质检权重校准（J1 闭环） |
| GET | `/api/quality/calibration` | 查询校准记录 |
| GET | `/api/quality/stats` | 按模板版本 / 租户 / 审核者 / 时间窗口统计质量 |
| GET | `/api/costs` / `/api/costs/deep` | 成本聚合 / 深度成本报表 |
| POST | `/api/samples` `/api/samples/sync` | 样本沉淀 / 自动同步 |
| GET | `/api/samples` / `/api/samples/export` | 样本检索 / JSONL 导出 |
| GET | `/api/dashboard` | 指标看板 |
| GET | `/api/traces` / `/api/traces/{trace_id}` | 链路列表 / 回放 |
| POST | `/api/knowledge` | 上传文本资料（JSON） |
| POST | `/api/knowledge/upload` | 上传教研文档（multipart，txt/md/docx/pdf） |
| GET | `/api/knowledge` | 知识分块列表 |
| DELETE | `/api/knowledge/{id}` | 删除知识分块 |
| GET | `/api/knowledge/retrieve` | 检索知识片段 |
| GET | `/api/notifications` | 站内通知 |
| GET | `/api/config-audit` | 查询模板 / 模型配置变更审计 |
| GET/POST/PATCH | `/api/users` | 用户管理（admin） |
| GET | `/api/health` / `/api/health/ready` | 存活与依赖就绪检查 |

---

## 文档

- [当前实现与系统架构](docs/docs_public/英语内容生产工作台-当前实现与系统架构.md)：项目定位、架构、生成/RAG链路、数据状态、维护入口与已知限制。
- [工程约束](AGENTS.md)、[行为准则](CLAUDE.md)、[详细规则](.ai/rules)：开发时的权威规范。
- [贡献说明](CONTRIBUTING.md)、[安全说明](SECURITY.md)、[第三方声明](THIRD_PARTY_NOTICES.md)。

公开文档不包含完整历史资料或本机原始证据；历史验证数字按发生日期和范围引用，不能当成所有环境均已通过的承诺。

---

## 开发约定

- 配置一律走 `config.py` 环境变量，禁止硬编码模型名 / 数据库串 / API Key
- 题型扩展通过新增 `templates/*.yaml` + `.st` prompt + SKILL.md，不改代码
- 模型输出必须经 Pydantic 二次校验，不直接信任模型返回
- 每次 LLM 调用带 `trace_id`，记录模型 / 输入输出 / 耗时 / 成本 / token
- 维护者本机仍在 `docs/优化记录.md` 与 `docs/tasks.md` 追踪变更（不公开分发）；公开贡献请在提交/PR中记录动机、改动和验证结果，见 [贡献说明](CONTRIBUTING.md)。

## 开源边界

- 这是一个个人实现的教学/求职项目，不代表生产可用 SaaS。
- 已验证内容以仓库文档和测试结果为准；未验证的生产语义会明确标注。
- 如果你复用本项目，请先检查模型 API、数据库和向量库配置。

## 已知发布前风险

- 前端依赖已升级至 Vite 8.2.2、React Router 7.18.3 和 Node 22 构建链；npm audit 结果为 0 vulnerabilities。
- 任务投递补偿与活动 request_hash SQL 唯一约束已实现；云调用不保证 exactly-once，专家答案质量、长期规模和权限范围仍须按场景核验。详见 [当前实现与系统架构](docs/docs_public/英语内容生产工作台-当前实现与系统架构.md)。

## License

MIT License，见 [LICENSE](LICENSE)。


## 当前能力与验证边界

- **OCR与RAG**：提供后台OCR任务、逐页/表格审核、显式付费确认、真实embedding索引、结构感知跨边界切分及关系感知small-to-big返回；默认Top-5，可选Top-8，RAG总上下文预算32768字符。OCR需另外配置服务，审核/来源命中不等于内容事实正确。
- **模型设置**：管理员可配置OpenAI兼容Provider、只写不读的加密API Key、模型档案和题型生成/Judge路由。需要独立 `PROVIDER_SECRET_KEY`；Embedding/OCR继续使用各自环境配置，探测模型列表不等于生成质量验收。
- **Trace与样本**：新chat可使用独立密钥的受限脱敏快照（默认1MiB/总密文128MiB/7天），不能补造旧Trace全文。few-shot只消费同范围、来源有效且真实人审通过的样本，最多2例/8192额外字符；工具接通不代表已有真实效果收益。
- **工程验证**：2026-10-06合流版本全量1173条通过（含63条真实PG）；2026-10-07依赖/CI修复后本地1113条通过、63条PG跳过（Docker未运行），前端安装、安全审计和构建通过。不同窗口的数字不合并为新的全量验收。
- **生产边界**：这是个人开发、学习与小批量人工审核项目。自动高分、合法Schema、来源和回放不能证明答案唯一/正确、独立模型收益或无人值守生产能力。源码发布不等于本机业务服务已升级。

详细机制和带日期的历史运行快照见 [当前实现与系统架构](docs/docs_public/英语内容生产工作台-当前实现与系统架构.md)。真实 `.env`、API Key、教材、dump、登录态、私有截图和 `.local-eval` 底稿不进入公开仓库。
