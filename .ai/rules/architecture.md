# .ai/rules/architecture.md —— 架构设计


> 文档分发：当前实现入口为 `docs/docs_public/英语内容生产工作台-当前实现与系统架构.md`；下文旧现状/验收文档仅在维护者本机保留，不随公开clone提供，源码仍为核验实现的依据。
## 分层架构
```
React 工作台 ──REST──> FastAPI 薄接口 ──入队──> Celery Worker
                                                    │
                                          LangGraph 状态机（生成→校验→质检→改版loop→入库）
                                                    │
                               engine（结构化输出/模型路由/LLM-judge）＋ rag（向量检索）
                                                    │
                                   PostgreSQL16+pgvector · Redis · Langfuse（Docker Compose）
```
- **入口层（api/）**：生成、内容、质检等主要调用 Service；成本、看板、Trace 等聚合查询当前仍在路由中实现。
- **编排层（workflow/）**：LangGraph 状态机，负责流程控制与状态流转。
- **引擎层（engine/）**：结构化输出、模型路由降级、LLM-judge 质检。
- **知识层（rag/）**：embedding / indexer / retriever，供生成前注入上下文。
- **队列层（worker/）**：Celery 消费批量任务，数量切片、进度更新、断点续跑。

## 核心模式
- **配置驱动（Configuration-driven）**：题型模板 YAML（input/output_schema、quality_rules、gen_prompt、run_config）驱动生成与质检；新增题型需 YAML + .st Prompt + SKILL.md，核心代码不改。
- **Adapter 隔离外部依赖**：云模型/embedding 经统一客户端接入；`router.generate_with_fallback` 负责主模型档案→默认模型档案降级，便于替换与测试。
- **Repository 访问数据**：常见 CRUD 使用 `repositories/` 与 `services/`；工作流和部分 API 聚合仍直接使用 Session，后续改动应保持边界清晰。

## 数据模型约定
- `models.py` 当前声明 15 张业务表（新增 quality_evaluation 草稿级评分事件）：配置、任务/item/Outbox、内容质量、Trace、知识/样本、用户/通知等；LangGraph checkpoint 表另由 saver 初始化。完整清单见 `docs/现状文档/系统现状.md`。
- 通用预留字段：`tenant_id`（对外化）、`created_by`、`created_at`、`updated_at`；类型用 `String(36)` UUID 主键。
- 结构化/动态字段一律 JSONB（`payload / params / input_schema / output_schema / dimension_scores / meta`）。
- JSONB 字段名统一用 `meta`，禁止 `_meta`。
- 状态契约见 `domain/status.py`；`worker/tasks.py` 和工作流仍有局部状态常量，改动时须核对三处一致性。

## API 规范
- REST，前缀 `/api`；返回统一 Pydantic 响应模型（`schemas.py`）。
- 校验：入口用 Pydantic 请求模型；业务失败返回 4xx（404 不存在 / 400 参数错误）。
- 写操作幂等：服务端创建 `task_id`，活跃请求用 `request_hash` 去重；item 使用 `(task_id, item_index)` 唯一键，Outbox 使用稳定 `event_id`。
- 成本查询在租户 scope 下聚合实际记录的调用估算（含生成/质检/embedding 和重试），标记未知 usage/成本分量；补偿 pending 费用计入任务预算，金额仍需供应商账单核对。

## 配置管理
- 运行时配置（环境变量）统一 `app/config.py`，采用 Pydantic Settings 风格，集中声明。
- 禁止在业务代码里硬编码模型名 / 数据库串 / API Key；一律从 `settings` 读取。
- 类型化默认值 + 环境变量覆盖；敏感信息（Key）走项目根 `.env`，不入库不入代码。
- **配置源为项目根 `.env`**：docker-compose 从项目根读取（从项目根运行 `docker compose -f deploy/docker-compose.yml`）；本地直跑时 `.env` 相对运行目录，从项目根运行 uvicorn 即可命中。`.env` 已被 `.gitignore` 排除，不提交。

## 可观测性
- 每次 LLM 调用记录：`trace_id`、prompt 版本、模型、输入输出、`latency_ms`、`cost`。
- Trace 默认写自研 `trace_log`；写库失败可落已脱敏补偿文件（稳定事件 ID、原子写入、幂等 replay）。启用 `TRACE_REQUIRE_DURABILITY` 时无任何持久化通道则失败关闭；不可恢复丢失保留告警。Langfuse 为可选导出，失败必须可诊断，不伪装成功。
- 质量与成本口径（阈值 70 / 采样轮数 / 成本聚合）从配置读取，便于调优。

## 断点续跑
- LangGraph 默认使用 `PostgresSaver`（dict_row）持久化断点；开发环境初始化失败可降级 `MemorySaver`。staging/production 默认在任务执行与 ready 两处禁止内存降级，除非显式设置 `ALLOW_MEMORY_CHECKPOINTER=true`。
- 任务失败从已有 checkpoint 用 `invoke(None)` 续跑，终态与人工 interrupt 重放不重新生成；同一 thread 使用 Postgres advisory lock 串行化，内容 thread_id 唯一，落库节点重复执行复用已提交内容。真实跨进程验收仍按 `docs/现状文档/验证状态与风险.md` 的证据状态解读。


## 租户与生产启动（P1 修复）
- worker 以任务行的 tenant_id 注入可信上下文，覆盖请求参数中的同名值。内容/评分/知识/样本/Trace 必须沿资源归属写入，不能以跨租户管理员账号的租户替代内容归属。
- 非 admin 的 None tenant 是默认租户 `IS NULL`，不能省略过滤；详情、读写、导出和聚合都按同一 scope。admin 为明确全局角色；不提供 tenant-admin 语义。共享模板/模型的 Null owner 可读，不意味着共享知识资料。
- 生产/staging 不执行 create_all，必须 Alembic 迁移并校验 head；关键初始化错误中止启动。CORS origins 明确配置，默认无 cookies credentials，禁止生产/凭据通配符。Compose 默认回环端口，worker/frontend 依赖 backend ready。
- RAG 支持 off/optional/required；命中与降级、来源快照/hash 可追踪。来源必审配置强制有效来源和人工核验门控，不以 Prompt 声明代替代码权限，不声称自动证明答案/事实正确。


## RAG P0 A/B/C（2026-10-04）
- parser 产出 ParsedDocument/DocumentBlock（`parse_file` 保留 string 兼容入口）；文档规范化快照、warning 和 locator 保存在 KnowledgeDocument；新 chunk 有可空 document/位置/版本字段，旧记录按 legacy 读取，不自动重嵌入。
- chunking 包只处理结构/预算/源区间，验证失败进入 fallback、耗尽抛 ChunkingError；context_header 与原文 content 分离，embedding/rendered context 可补标题和表头，坐标必须能回放规范化原文 slice。
- token_limit 是完整输入 UTF-8 字节保守预算，不是精确供应商 tokenizer。表格/代码/公式超过预算时仍可受控拆分并告警；PDF 暂无布局/OCR 保证。
- indexer 分批调用 embedding、完整验证后一次事务保存文档和 chunks；知识服务处理权限/输入/预览与响应；preview 不调用模型且不写知识表，diagnostics 按资源 scope。
- 迁移新 head `rag_p0_abc`；仅离线 SQL 和单元回归通过。真实 Postgres/Redis/供应商及浏览器端到端待验收。Parent-Child、混合检索/RRF/rerank、知识点层级/别名仍属后续 P1。


## RAG P1 D/E/F（2026-10-04）
- 新大文档从 P0 child 源跨度聚合兼容 parent，parent embedding=NULL/type=parent，不进初始向量/关键词召回；child/parent/prev/next 关系一次事务落库。旧 NULL type/model 的叶子兼容，不自动重嵌入。
- retrieval 分层负责 vector/keyword、fusion、rerank、expansion、context；每路和父/邻查询都保持 tenant/document/source/section/point scope。分类 YAML 是公共 taxonomy，不是授权或共享知识库。
- 标签 canonical/aliases/multi/层级由 YAML 配置；exact 默认，ancestor/descendant/related/semantic 显式。LLM query variants 不能修改 scope；prompt 独立 .st、Pydantic 校验、输出/重试/超时/数量有界。
- Rerank off/optional/required；required 失败即使 optional RAG 也关闭，Trace durability 失败不吞。关键词可独立满足来源 required，但不等于事实已核验；未知 rerank 费用标识 unknown。
- Context 包括 wrapper/标题/表头/分隔符预算，源区间去重和截断必须同步 snapshot/hash/位置；parent/neighbor 分数标明来自 matched child，不伪装为扩展对象独立分数。
- 迁移 head rag_p1_def，HNSW/FTS/trigram/tag GIN 仅离线 SQL/单测证据，SQLite 为 reference；真实数据库/计划/召回/供应商/生产准入与 P2 仍待。


## 部署/集成验证补充（2026-10-05）
- Compose API/worker 共同引用 x-rag-environment，RAG settings + embedding shape/timeout 必须一致；Optional[int]空环境值由 Settings before validator 转为 None。
- 集成初始化执行真实 Alembic/head校验，不以 create_all/stamp遮蔽迁移；fixtures 通过同库transaction/savepoint或UUID范围清理，不TRUNCATE复用数据库。mock向量必须非零且有差异，但不是语义质量证明。
- 当前已有 english-edit-ci-postgres 实际迁移至 rag_p1_def，13项PG RAG + 5pipeline + 3跨进程恢复通过；索引计划可用测试force seqscan off，不等于默认性能证据。全栈/真实Redis/供应商/语义评测未验收；覆盖率门禁未通过，见最新验收文档。


## 原环境运行态验证（2026-10-05，OPT-062）
- 原Compose显式 --env-file .env，从项目根运行；backend/worker/frontend及原Postgres/Redis当前up，主库迁移到rag_p1_def。Langfuse可选sidecar本轮未启动。
- Nginx body11MiB给后端10MiB文件留multipart空间；不能用上调代理限额代替后端大小校验。无文字预览返回indexable false/专用原因，上传必须在embedding前拒绝，UI不能把空覆盖率当作教材处理成功。
- 实际扫描教材15文件341页无可抽取文字，OCR与大文件批次仍待；浏览器/队列基本连通健康不是语义检索质量或全面故障恢复证明。


## 显式本地 OCR 结构桥接（2026-10-05，OPT-064）
- rag/ocr 负责本地 MinerU V1、逐页路由和原生JSON到DocumentBlock/Table；preview是纯结构/切块函数，不访问知识DB或embedding。生产API/worker不装大模型权重。
- 普通HTTP预览/上传仍不自动OCR。RAG_OCR_*仅为显式CLI/后续后台复用配置，默认off，API/worker必须一致透传；网页异步文档/页任务尚未实施，不将同步预览改为整书推理。
- 原物理页/源hash/引擎版本/native snapshot可追踪；bbox保留引擎实际normalized坐标帧，缺失为None，禁止伪造子块/单元格框。合并表格展开必须保留原始HTML/grid/spans/源行映射。
- partial_document在预览不可索引且知识服务embedding前拒绝；不连续选页重置section上下文；OCR失败/协议不兼容不伪装为部分成功。结构覆盖率不代表OCR准确率/资料正确率。
- 本轮594全量通过（含原PG21项），主容器未重建，资料未入库；当前证据与后续任务见MinerU-OCR接入与验收文档。


## OCR-3 后台解析（2026-10-05，OPT-065）
- ocr_job/page是工作状态，不是knowledge索引；API接收/校验/建任务，独立ocr队列每消息一页，beat补发/重试/回收过期租约。生成worker只消费celery，OCR worker不重放生成任务。
- 成功页原子文件+hash与SQL租约token共同提交；cancel失效旧owner、resume保留有效成功页/修复损坏检查点，不能靠broker结果冒充持久化进度或严格exactly-once。
- 缓存tenant/hash/页/服务版本/代码签名隔离；输入/输出有界并校验，文件源可回放。共享spool是volume，资料导入是配置根目录只读mount；路径/鉴权/scope用代码强制。
- 后台upload128MiB与原知识upload10MiB分离，不同步HTTP跑整书，不自动embedding或入知识表。原生坐标/事实/warning仍需OCR-4界面和人工确认。
- 原Compose与宿主鉴权CPU服务现已实际部署，head rag_ocr_jobs，全量632通过；网页交互/真实语义/运营仍待。以OCR-3后台任务与大文件验收为当前运行态来源，早期CLI/未部署描述保留历史。


## OCR-4 审核与真实索引（2026-10-05，OPT-066）
- 知识库OCR工作台现已部署；review-plan免费本地，原图鉴权/范围/hash核对，PDFium有界串行渲染，坐标缺失或尺寸不匹配不伪造定位。
- preview_hash/plan_hash绑定实际过滤块、切块/父块配置、模型/维度和taxonomy；原页/告警/费用声明、partial范围与费用重试授权由后端强制。声明不自动证明真实阅读或事实正确。
- 后台付费index稳定文档ID+owner+同一事务写知识与indexed状态；未知已付调用不自动重放，进入needs_attention。provider batch cap走config，不硬编码模型；Trace明确配置估算/账单未验。
- partial真实状态不改false，另记reviewed_index_scope；普通上传门控保留。引用source_reviewed与verification=factual-unverified分离，不能把OCR审核宣传成事实认证。
- 当前真实8页1文档/36非零leaf向量+2parent与单文档8条页级检索通过，head rag_ocr_review；完整649回归。全库/生成质量/P2运营仍待，旧未入库/未界面记录为历史。


## RAG P2 单文档索引运营（2026-10-05，OPT-067）
- OCR来源重建必须显式授权并校验expected revision；先生成/验证全部新向量，再同事务替换chunk和任务状态，保持原Document ID/FK。失败保留旧索引，未知付费不自动重放。
- 资料撤除保留OCR原文件/成功页/审核预览，置removed；部分删除置partial_index/stale。禁止单块API直接删parent；删child需避免旧parent回注已删内容并修复邻接。重建pending/indexing拒并行删除。
- 工作台恢复旧标签/排除/备注，不继承费用/核对checkbox。当前重建复用OCR结构快照，parser升级另走OCR；未实现租户批量/legacy通用重建或任意历史向量回滚。
- 当前3资料/52真实向量+2parent、多PDF18条最终上下文页Hit17/18，659回归/26真实PG；无scope连系动词漏召回未修复。完整G/全库/生成质量不因局部验收完成。
- 现役证据：`docs\现状文档\RAG-P2索引运营与多文档评测.md`；上方阶段数字/未部署描述为历史。


## 中文自由问句与结构评测（2026-10-05，OPT-068）
- keyword保留English FTS/完整短语，新增有界CJK二字片段，不自动重写scope/知识点，不变RRF/向量模型；RAG_CJK_KEYWORD_MODE=bigram/legacy、RAG_KEYWORD_MAX_TERMS默认32(2–64)，Compose和示例环境同源。
- lane/fusion/实际ranked候选返回来源身份与词项，不附额外候选全文/密钥；tenant/document/parent过滤与autoescape照旧强制。
- 评测分开页命中、精确块、源表同行、全部相关单位/词项；parent凭matched_child_ids，部分跨页命中不能算context_complete。逐单位阶段失败与语料/向量指纹绑定，漂移报告但exit2。
- 原连系动词漏召回已修复；原18+新16有限集通过，另2跨页仅1完整，第3页续例仍融合11未进top5。全app79.51%未过CI80%，不能把终端四舍五入或scoped覆盖率称全项目通过。
- 现役证据：`docs\现状文档\RAG-P2中文召回修复与结构评测.md`；更早原问题未修复/未认证覆盖率为历史。


## STR-1/2现役约束（OPT-069）
- 结构是版本化派生层，不覆盖原文；布局家具不重置同章小节，缺页/排除/新章/独立题答案是屏障，未知关系proposed且越屏障ID不可确认。
- relation只从当前同scope活动leaf取body，stale/过期审核不扩展；不可从document snapshot回注已删文字。source segments保真实页/块/坐标/hash，组合体不伪造单跨度/bbox。
- top_k仍约束bundle/snippet返回数，实际segments可更多；生成/评测/人工快照计实际送达内容，不按claimed IDs或citations[:top_k]计全部证据。
- 免费结构预览/审核不embedding，OCR计划包含structure/policy签名；new索引必要时才显式费控。运行relation、legacy可回退，预算/member/hop/segment硬上限不依靠prompt。
- 当前751/32真PG、app80.57%，原36齐备/新8仅7齐备，事实状态unverified；STR-3～6及真实生成质量未完成。181本轮embedding/2037报告tokens，配置估算0.004074/账单未验，无真实chat/judge/rerank。
- 现役证据：`docs\现状文档\RAG-STR1-STR2落地与验收.md`。更早STR-0仅调研、79.51%未过门槛为历史，原未提交变更/私有现场有意保留，不自动清场。


## STR-3/4现役边界（OPT-070）
- table续接不能只靠表头/列数；几何需同normalized frame，跨新章/缺页/排除/未知正文不自动合。table先确认、cell按原column/span范围逐项审核，未证明rowspan保留unresolved。派生header/视图不覆盖原源。
- 只用已送活动leaf文本构建logical view，每cell保原page/block/source row/column/segment。未送view/假ID/改写值不能给v5支持分，删除/stale不从grid补回。
- 2/3页复核是独立OcrBoundaryJob，显式本地计算/全局映射/缓存版本/SQL token/取消续跑/lease回收，原成功页/preview/index不自动修改；迁移head rag_str34_boundary，原库备份保留。
- STR-4原query保留、有界互补问题/概念导航/唯一明确章节引用；scope/tenant/document不得扩大，冲突源独立unverified。rules默认无chat，LLM分解仅显式配置与Trace/schema门控。
- 最新811/36PG、app81.63%，原36+旧8标注齐备不是全库质量；STR-5/6、真实新教材cell/专家金标/生产事实仍待。现役证据：`docs\现状文档\RAG-STR3-STR4落地与验收.md`。


## STR-5结构布局与STR-6实验边界（OPT-071）
- `RAG_CHUNK_LAYOUT`默认legacy；structure是原生upload/preview及OCR审批/worker显式选项。结构变更/embedding输入变化绑定计划hash和费用确认，不默认全库重建；同ID原子升级/失败保旧。当前运行预算/质量限制查最新Top-5/8验收，STR-5/6报告保留阶段证据；不能靠开关冒充旧索引回滚。
- structure leaf/parent以活动结构关系和保护边界为依据，物理页为软边界。非连续正文使用逐段source_segments；顶层content_start/end=None，source_envelope只导航，不可用于读回正文。source/text/hash不可变；furniture/纯导航排除的覆盖分母须公开。
- relation扩展只从活动同tenant/document leaf已索引片段加载，不能用整源包络补入未索引/被排除资料；stale结构关闭扩展，仍保留精确seed。候选block/page诊断不等于最终正文引用。
- `rag/experiments`/`scripts/rag_experiment.py`独立只读实验，默认dry-run、不写知识版本；Late只有显式本地token hidden states模型先forward后精确源段pool，不模拟最终向量API。不自动安装/下载或运行remote code，不静默截断。
- 工程测试与效果验收分开：真实模型未执行不得把mock/dry-run算效果；重建改变leaf/向量不能称同向量单因果A/B，gold应在交换前固定原始来源相关性并保留失败。
- 验收：`docs\现状文档\RAG-STR5-STR6落地与验收.md`；旧阶段未实施说明是历史，不代表现役代码。


## RAG返回预算契约（OPT-072）
- 默认`RAG_TOP_K=5`，知识库网页可选8；候选池仍30。Top-K约束返回包上限，不保证数量；生成与API省略参数都应请求期继承配置，不在路由导入时冻结默认。
- 默认`RAG_CONTEXT_MAX_CHARS=32768`是应用侧字符上限，不是模型token窗口；可选`RAG_CONTEXT_TOKEN_LIMIT`当前实现为UTF-8字节保守限制，默认None。调整预算不放宽来源/tenant/member/hop/segment边界，也不隐式重嵌入。
- 评测预算变动用显式override和effective_top_k报告，原gold相关性/hash不改；来源补回不等于同预算排名算法改进。现役结果见 `docs\现状文档\RAG-Top5-Top8与上下文预算验收.md`。


## 主RAG与生成来源门禁（OPT-073）
- 当前默认主链路是`RAG_CHUNK_LAYOUT=structure` + `RAG_CONTEXT_MODE=relation`；Top-5默认/Top-8可选、总字符32768；legacy只作显式兼容/回退。存量索引切换必须沿用选页、排除、结构审核并显式重建，不能默认改读或自动全库重嵌入。
- 内置知识题型模板必须显式`rag.mode=required`并要求来源人工核验；无有效实际segments在chat前失败关闭。`optional`只适用于明确允许无资料创作的模板，不能用全局optional覆盖模板required。
- `ContentItem`的RAG provenance只承载实际来源segments；人工通过需要代码验证`reference_verified`与citations，前端通过按钮也必须由后端再拦，pending_qc不得发布。
- 自动QC高分不等于事实/答案正确；真实批量闭环必须人工核对来源、题干、选项、答案、解析。当前OPT-073真实小批次发现完形潜在多解，必须进入质量回归而不是自动发布。
- 现役报告：`docs\现状文档\RAG-主链路切换与真实生成验收.md`。


## 数量与投递恢复契约（OPT-074）
- 外层quantity是总执行项数，每项params.quantity归一1；后端/worker/Graph兼容旧params，禁止多对象静默取首项。去重需含总批次，不仅归一化params。
- 父/子item投递均Outbox，PG skip-locked+CAS lease token/expiry fencing，发送快照不得占DB事务；item整worker非阻塞锁与Graph锁namespace分开。
- shared beat（ocr-scheduler常驻）周期maintenance，活动pending/dispatched/running有界对账；终态/取消/人工/持锁/未到retry_after不可抢跑；耗尽failed/dead，显式目标重放保累计attempts和新周期。
- Redis命名卷+AOF everysec，迁移已有数据先停写/备份/RDB bootstrap再启AOF；禁止删旧卷/flush真队列来验收。仍非云侧exactly-once或零丢失，备份/SQL对账必要。


## 用户模型配置边界（OPT-076）
- Provider/ModelProfile/ModelRoute为管理员配置，配置驱动而非代码硬编码；Route独立于题型YAML同步，不能在启动时抹掉用户生成/Judge覆盖。
- Provider凭据由独立环境加密key服务端解密，API只写不读；未绑定Provider的旧档案才使用ENV兼容路径，已绑定者不得暗中借ENV密钥访问别的端点。
- task/item/Graph/content按各自对象定义；共享映射与SQL契约为权威，不将Graph中间态写进item。人工等待是活动防重/投递保护，不是最终成功。
- 活动请求SQL唯一是并发裁决，终态可重做；旧指纹格式在途升级先排空/核对，不能删除约束绕过冲突。


## Trace载荷与few-shot消费（OPT-077）
- 完整脱敏chat载荷独立于Trace摘要、外部sink与成本行，独立key/事务额度/TTL/ops scope审计；回收密文不得抹除历史费用。旧摘要不补造全文，回放不等于确定性重跑。
- 样本用途fewshot不是专家证明；消费重新验证实际人工/源状态/tenant及任务归属/hash/现役Schema与来源核验，整例超预算跳过，不能把示例当RAG事实。
- 模型对比入口和真实质量/成本验证分开；缺少不同真实候选或人工标签标pending，不捏造验证完成。
