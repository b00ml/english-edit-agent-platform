# 英语教研 AI 内容生成平台 2.0

维护者主文档见 [当前实现与系统架构](docs/现状文档2/英语内容生产工作台-当前实现与系统架构.md)：主文档及五份专题按业务流程解释架构、数据/状态、RAG、生成质量、可靠性与部署维护。原现状文档及验收报告作为阶段证据保留。 历史追溯见 [历史与证据索引](docs/现状文档2/历史与证据/00-历史与证据索引.md)，包含演进、验收矩阵、失败记录和原位文件hash清单。

当前代码、质量评测、任务恢复及已知风险见 [项目现状文档](docs/现状文档/系统现状.md)。

最新OPT-074（2026-10-06）：批次N/单item1、父/子Outbox租约与周期恢复、Redis命名卷+AOF everysec已落地；955回归/50真PG/app82.62%，原4236 Redis值保留、原业务计数不变，无真实模型调用。见 [紧急1—2—3验收](docs/现状文档/10-6优化1-2-3落地与验收.md)。

上轮OPT-073（2026-10-06）：structure/relation已成为主RAG链路；名词/动词存量已选择性迁移，三内置题型要求来源并人工核验。6条真实生成均保留待质检，发现完形质量风险不自动发布；见 [主链路切换与真实生成验收](docs/现状文档/RAG-主链路切换与真实生成验收.md)。

上轮OPT-072（2026-10-06）：默认RAG返回Top-5/网页可选Top-8，总上下文预算32768字符；候选池30、融合和scope不变。最终两档真实44题均来源齐备，统一Top-3仍43/44，不冒充排序算法优化；895回归/42真PG/app82.31%，原环境已部署且无语料重嵌入。见 [Top-5/8与上下文预算验收](docs/现状文档/RAG-Top5-Top8与上下文预算验收.md)。

上轮OPT-071（2026-10-06）：STR-5结构化跨页leaf/逐段来源/显式选择性重建与STR-6独立实验代码已部署；并列句同ID v2→v3，主库3资料/70真实leaf向量+1parent。871回归/39真PG、app82.29%；真实44题来源齐备44→43，已知RRF/top3回归保留，默认layout仍legacy、不全库推广。语义/确定性上下文真实小实验无增益，Late真实模型及LLM上下文效果仍待；见 [STR-5/6验收与限制](docs/现状文档/RAG-STR5-STR6落地与验收.md)。

上一阶段OPT-070：STR-3逻辑表格/局部2或3页OCR复核、STR-4跨章引用/多问题证据检索已部署；811回归/36真PG、app81.63%，原36+8有限gold来源齐备。未重嵌入，复核不自动改原源，真实跨页表格全量质量仍待；见 [STR-3/4验收](docs/现状文档/RAG-STR3-STR4落地与验收.md)。

上一阶段OPT-069：STR-1/2结构重建与关系感知small-to-big已部署，原36条齐备、新8条7齐备，751回归/32真PG，全app80.57%。旧页内模式可回退，不重嵌入；参见 [STR-1/2验收](docs/现状文档/RAG-STR1-STR2落地与验收.md)。

历史OPT-068（2026-10-05）：OCR工作台、真实索引/单文档重建撤除、中文自由问句补召回已部署；3资料/52真实leaf向量+2parent。原18+新16条有限样本带齐标注来源，但另2跨页仅1条完整；全app覆盖率79.51%未过CI80%。仅选页不代表整书，来源命中不代表生成事实正确；最新证据见 [中文召回与结构评测](docs/现状文档/RAG-P2中文召回修复与结构评测.md)，索引运营见 [上一阶段报告](docs/现状文档/RAG-P2索引运营与多文档评测.md)。

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
└── docs/                   # PRD / 技术架构 / 任务清单 / 优化记录
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

- [项目现状](docs/现状文档/系统现状.md) · [质量与评测](docs/现状文档/质量与评测现状.md) · [验证与风险](docs/现状文档/验证状态与风险.md)
- [需求文档（PRD）](docs/AI内容生成平台2.0-PRD.md)
- [技术架构设计](docs/AI内容生成平台2.0-技术架构设计.md)
- [开发任务清单](docs/tasks.md)
- [优化记录与成果追踪](docs/优化记录.md)
- [P1-5 部署、迁移和恢复演练](docs/P1-5-部署迁移恢复演练.md)
- [优化技术设计 3.0](docs/优化技术设计3.0.md)
- [面试准备 - Agent 项目深度拷打与踩坑复盘](docs/面试准备-Agent项目深度拷打与踩坑复盘.md)
- [GitHub 开源发布检查清单](docs/开源发布检查清单.md)

## 历史验证记录（本次未复验）

- 非集成测试：`240 passed, 5 deselected`。
- Docker PostgreSQL/Redis 健康检查、Langfuse 独立数据库初始化、`alembic upgrade head`、`pg_dump/pg_restore` 独立恢复库已验证。
- 迁移当前 revision 与 head 均为 `m3_03_model_hash`；恢复库未覆盖源库。
- 恢复演练入口：`python scripts/recovery_drill.py --phase checkpoint-start|checkpoint-resume|outbox`（详见 P1-5 文档）。
- 模型路由具备 fallback、cooldown、失败计数和任务预算限制；任务通过 `trace_id` 关联 LLM、embedding、workflow、queue 生命周期。
- 尚未宣称的边界：运行中 worker kill/restart 后的真实 PostgresSaver checkpoint 恢复，以及 Celery dead outbox 在线重放，需在带 worker/checkpointer 的部署环境执行。

---

## 开发约定

- 配置一律走 `config.py` 环境变量，禁止硬编码模型名 / 数据库串 / API Key
- 题型扩展通过新增 `templates/*.yaml` + `.st` prompt + SKILL.md，不改代码
- 模型输出必须经 Pydantic 二次校验，不直接信任模型返回
- 每次 LLM 调用带 `trace_id`，记录模型 / 输入输出 / 耗时 / 成本 / token
- 每次代码改动在 `docs/优化记录.md` 追加 `OPT-0XX` 记录，并同步 `docs/tasks.md`

## 开源边界

- 这是一个个人实现的教学/求职项目，不代表生产可用 SaaS。
- 已验证内容以仓库文档和测试结果为准；未验证的生产语义会明确标注。
- 如果你复用本项目，请先检查模型 API、数据库和向量库配置。

## 已知发布前风险

- 前端依赖已升级至 Vite 8.2.2、React Router 7.18.3 和 Node 22 构建链；npm audit 结果为 0 vulnerabilities。
- 任务投递、request_hash 并发唯一性和全量多租户 scope 仍有边界，详见 Agent 项目深度拷打与踩坑复盘。

## License

MIT License，见 [LICENSE](LICENSE)。


### 扫描 PDF 的显式本地 OCR（2026-10-05，OPT-064）

本地 MinerU 4.x V1 → 逐页路由 → 结构化表格/标题/来源 → 父子切块预览已实现；使用 `backend\scripts\ocr_preview.py`，配置 `RAG_OCR_ENGINE=mineru` 和本地 `RAG_OCR_URL`。不安装 OCR 权重到 API/worker、不调用付费 embedding、不写知识表。

普通网页预览/上传仍不自动 OCR，10MiB 上传限制未变；本地显式 CLI 默认最多3页/原PDF128MiB。部分选页 indexable=false 且不可正式入库。后台批次、进度与网页操作待下一阶段，不应把代码工具交付当作生产页面已部署。

配置、启动和真实教材证据见 `docs\现状文档\MinerU-OCR接入与验收.md`。


### 后台 OCR API 与大文件（2026-10-05，OPT-065）

原环境已提供鉴权的OCR job/page API、独立ocr队列/worker与scheduler。启动使用原Compose的 `--profile ocr`，宿主缓存MinerU由 `deploy\start-local-ocr.ps1` 隐藏启动并要求私有Bearer key；Docker需能到达配置端点。

`POST /api/knowledge/ocr/jobs/upload` 是独立的128MiB后台接收路径；原知识上传/同步预览仍10MiB。API支持根目录内批次导入、进度、取消、续跑及JSON预览，没有自动embedding/索引。网页工作台的OCR操作界面仍待下一阶段，不因后台API已部署就称页面已实现。

实际大文件/检查点/缓存与恢复证据见 `docs\现状文档\OCR-3后台任务与大文件验收.md`；旧OPT-064“主镜像未更新”是当时记录。


### OCR工作台、审核与真实索引（2026-10-05，OPT-066）

知识库页面现已提供完整OCR工作台：原页/识别表格/告警核对、问题块排除与恢复、审核后后台真实embedding入库。费用声明与预览/hash/config绑定、partial选择范围、幂等和不确定费用手动重试由后端强制；原文已核对不代表事实已认证。

完整8页真实教材已形成36个1024维非零向量+2父块，单文档8条页级检索命中通过。不要把小样本当全库/生成质量100%；其余教材未自动索引。费用Trace当前为未核验的全局fallback估算，实际账单以供应商为准。

当前运行态与验证以 `docs\现状文档\OCR-4工作台与真实RAG验收.md` 为准；前面“界面待下一阶段”段为历史记录。


## 管理员模型设置（OPT-076）

工作台左侧“模型设置”（`/model-settings`）可配置OpenAI兼容Provider地址/API Key、模型档案、默认生成降级档案及各题型生成/Judge覆盖。先Provider→档案→题型路由；未绑定/留空保留ENV/YAML，重启不清用户route，不自动替换现有模型。

密钥只写不读，保存需服务端独立`PROVIDER_SECRET_KEY`（Fernet key），各Python服务保持一致并与DB备份配套保留；不能用JWT key替代。编辑留空保留密钥，非管理员API/页面无管理权限。Embedding/OCR仍原环境配置；模型价格仍后端单价估算，探测/models不代表生成/专家质量验收。详细说明见[第二轮与模型设置验收](docs/现状文档/10-6优化第二轮与模型设置落地验收.md)。


## 受限回放与few-shot（OPT-077）

链路展开可主动查看新chat的受限脱敏请求/响应；默认1MiB/密文总128MiB/7天，独立TRACE_SNAPSHOT_SECRET_KEY，与Provider/JWT key分离，读取须ops/归属且审计。旧Trace不能补出全文，Embedding/lifecycle仍摘要，模型重跑不保证一致。

样本页可将人审通过样本标为fewshot；生成按同tenant/题型/规范化知识点、真实人审/源当前状态/hash/规则/来源核验选最多2个完整示例。8192为额外示例字符预算，不是总LLM窗口。当前样本库空，启用消费不宣称真实质量收益。

模型比较工具`backend/scripts/model_compare.py`默认plan-only，--execute才调用已配置的不同候选；同端点/模型拒执行，产物不自动入库或变金标。真实不同候选和人工裁决仍需要用户。详见[第三轮验收](docs/现状文档/10-6优化第三轮落地与验收.md)。


## Current release baseline (2026-10-06)

The current public baseline includes the RAG structure/relation retrieval pipeline, OCR task/review workflow, durable generation delivery, deterministic question-output validation, state-contract and concurrency safeguards, admin-configurable OpenAI-compatible providers/models, bounded encrypted Trace snapshots, guarded human-reviewed few-shot consumption, and the first responsive workspace UI pass.

The current engineering gate is **1,173 tests passed** in the merged local/remote working tree; this includes the latest repository/CI contract tests and the existing project regression suite. The public repository does not include real `.env` files, API keys, database dumps, course-material blobs, login sessions, `.local-eval` evidence, or private screenshots.

This is still a personal development and learning project. Automated scores, valid schemas, Trace snapshots, few-shot wiring, and model-comparison tooling do not prove expert answer correctness, unique answers, independent-model quality gains, or unattended production readiness. Configure your own provider credentials from `.env.example` or the administrator model-settings page before using external models.

For the exact current implementation and known boundaries, start with [the maintainer architecture document](docs/现状文档2/英语内容生产工作台-当前实现与系统架构.md) and [the public release audit](docs/公开发布-2026-10-06.md).
