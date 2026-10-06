# RAG 优化计划：基于 WeKnora 能力移植的文档理解型 RAG

## 现役补充：OPT-074（2026-10-06）

已按顺序修复数量契约、父/子Outbox投递恢复和Redis持久化。外层quantity为批次N、每item归一1，多对象包装不静默截断；周期20秒maintenance＋SQL skip-locked/CAS lease fencing与item锁，bounded对账/重试/显式死信投递；Redis迁入命名卷+AOF everysec，原4236值核对一致/旧卷备份保留，增量写重启保留。

955完整测试通过（50集成/905非集成），app8120/9828=82.6211%；21文件lint及4源scoped strict通过，非全app/远端CI认证。main/CI head已为opt074_delivery_leases，已部署，原task8/content10/知识3doc82chunk及OCR/用户计数保持，无真实模型调用或课程重建。题型潜在多解、并发请求防重、状态词、独立Judge等不在本次前三项修复范围。

验收：[1—2—3落地与验收](10-6优化1-2-3落地与验收.md)。下方OPT-073/旧head/911/未开启AOF等为原阶段记录，不能当现役配置。

## 现役状态（2026-10-06，OPT-073）

OPT-073已将structure/relation设为主RAG链路：新资料默认structure，Top-5默认/Top-8可选、32768字符；名词和动词已同ID选择性迁移为structure v2，并列句structure v3保持。三内置题型version2均required来源+人工核验；6条真实正向生成/质检完成但全部pending_qc，发现完形样本需人工复核；无来源反例chat前失败。最新911回归/42真PG/app82.34%，主库3资料/82chunk=81leaf+1parent。下一步是6条人工标注、每题型20～30条真实金标、驳回率/来源一致性/答案唯一性统计，再考虑规则收紧；Late/LLM context和专家table/cell事实质量仍待。

完整现役验收见 `docs\现状文档\RAG-主链路切换与真实生成验收.md`。OPT-072及更早“默认legacy/895/70向量”等仅为历史阶段。

## 计划基线与历史演进（现役状态见上；历史快照以各自日期为准）

**初稿日期**：2026-10-04
**最后更新**：2026-10-06（部署基线OPT-072；Top-5/8与32K字符预算已生效，Late/LLM效果待验）
**适用项目**：英语教研 AI 内容生成平台 2.0
**基线文档**：`docs\现状文档\RAG现状.md`
**对标项目**：`<本地WeKnora源码目录>`
**当前状态**：P0 A/B/C + P1 D/E/F、OCR-1～4、STR-1～5已部署；STR-6独立实验代码完成/真实Late及LLM上下文效果待验。主库3资料/70真实leaf向量+1parent；最终32K预算下Top-5/8的44题标注来源均齐备，融合排序未变，默认layout仍legacy。完整P2、全量教材与生产事实质量仍待
**下一实施路线**：STR-1文档级结构与重复页眉 → STR-2关系感知small-to-big/分段引用 → STR-3续表/局部多页复核 → STR-4跨章节多证据 → STR-5结构切分与选择性重建 → STR-6语义/Late Chunking独立实验。STR-0及STR-1～5已完成并部署，STR-6实验入口已实现，真实Late/LLM效果未验收；设计见 `docs\现状文档\RAG跨边界知识组织与检索优化方案.md`。

**当前验证边界（2026-10-05）**：用户已允许复用现有 Docker；已有 PostgreSQL 实际迁移/专项测试通过。OPT-066/067 已按用户授权调用真实 embedding，未调用真实 chat/judge/rerank；OPT-062 已验收主全栈/Redis 空任务，OPT-063 已完成选型，OPT-064 已实现显式本地 OCR/结构预览；OPT-065 后台OCR已部署并验收；OPT-066 工作台与完整8页真实索引/8条页级检索已验收，批量/全库与生成质量仍待。OPT-067 新增索引运营，OPT-068修复原无scope漏召回；原18条18命中、新16条精确块16命中，但另2跨页仅1条上下文齐备，全app79.51%未过CI80%；2026-10-04 各阶段离线验证及更早单资料说明保留为历史。


> **历史阶段：2026-10-05，OPT-067（后续以OPT-068部署基线和STR-0设计为准）**：OCR 来源单文档显式重建/活动版本、失败保留旧索引、资料撤除及父子块删除一致性已部署；工作台恢复原审核排除/标签。并列句同 ID 已真实 v1→v2；新增名词仅页2/16、动词仅页1，主库当前3资料/52个真实1024维非零leaf向量+2parent，非三本整书。18条多文档冻结样本最终上下文PageHit@3=17/18、页MRR=0.916667；“连系动词为什么需要表语？”不限知识点漏召回，verbs scope对照命中，失败未被算作修复。最终659全量回归通过，含26真实PG集成；本轮26次真实embedding/5146报告tokens，金额仅未核验配置估算，无真实chat/judge/rerank。原task3/content4/user1不变，head仍rag_ocr_review。G1/G4为有限交付，完整P2/全库与批量生成事实质量仍未验收。详见 `docs\现状文档\RAG-P2索引运营与多文档评测.md`。下方649/1资料及更早的空知识库、未付费/未部署描述均为历史阶段，不代表当前运行态。


> **当前结论：2026-10-05，OPT-068（下方为历史阶段）**：已用可回退的有界中文bigram关键词通道修复无scope连系动词漏召回，不改知识点范围、不重嵌入语料。真实同语料A/B：原18条17→18命中；新16条精确块14→16、上下文齐备13→16；10条表格同行/13元组两策略均通过，没有新增表格准确率收益。另2条跨页探针仅1条带齐全部来源，页3续例仍在融合第11未进top5；36条仅35条标注上下文齐备，不称36/36完整回答。v3评测分开页/精确块/同行/全部来源与逐单位失败，并绑定语料/向量指纹。684回归通过/28真实PG；全app覆盖率79.51%仍未过CI80%，4模块scoped mypy不替代全app。92次真实embedding/944报告tokens，费用仅未核验配置估算；无chat/judge/rerank。原业务和3资料/52向量+2parent不变，head仍rag_ocr_review。当前部署bigram/32项、aliases默认、rerank off。详见 `docs\现状文档\RAG-P2中文召回修复与结构评测.md`；更早“原问题未修复/未重测覆盖率”等为历史，跨页和完整P2/批量生成质量仍待。


> **STR-1/2已部署：OPT-069（2026-10-05 UTC）**：文档级结构/重复页眉及连续小节、缺页/排除/题组/新章屏障、免费结构预览审核及hash/版本门控已实现；关系感知small-to-big仅从活动同scope leaf返回逐段真实引用，v2 bundle/segments协议已接API/前端/生成provenance/人工审核和v4评测，legacy可回退。相同原文/leaf向量36条回归35→36齐备，新8条6→7齐备（跨小节复合问题仍1条未完整，属STR-4）。751回归/32真PG通过，全app80.57%越过80%门槛，13模块scoped mypy不等于全app。运行relation/aliases/rerankoff，未重嵌入或新OCR；Doc3/52向量+2parent、原业务及head不变，只有并列句结构审核meta更新。181真实embedding/2037报告tokens，费用仅未核验配置估算，无chat/judge/rerank。详见 `docs\现状文档\RAG-STR1-STR2落地与验收.md`。STR-3～6及批量事实质量仍待；下方STR-0“未实现”和OPT-068“79.51%未达门槛”为历史。


> **STR-3/4已部署：OPT-070（记录2026-10-05 UTC；本机日志2026-10-06）**：逻辑续表多证据/未知待审、逐cell确认与原row/column映射、重复表头派生去重、局部2/3页后台复核（独立snapshot，不改成功页/索引）及跨章明确引用/原query+有界互补问题/多seed公平预算已实现。真实MinerU4.0.10原页2/3、2/3/4完成；相同36+8gold planning off/rules：36仍齐备、8由7→8齐备，无真实chat/judge/rerank或语料重嵌入。811回归/36真PG，app81.63%门槛通过，14模块scoped mypy；head为rag_str34_boundary，主/CI迁移前已备份。原Doc3/52向量+2parent、业务/OCR成功页不变，新增2个复核job；结构审核v2显式更新。178真实embedding/2140报告tokens，金额仅未核验配置估算。真实窗口不含表格，跨页table/cell规则用fixture/真PG验证，不冒充全教材OCR准确率；STR-5/6、专家金标/生产事实质量仍待。详见 `docs\现状文档\RAG-STR3-STR4落地与验收.md`。下方STR-3/4待实施及head旧值为历史阶段。

---

## OCR-4工作台与真实embedding（2026-10-05，OPT-066）

完整8页资料、36非零向量+2parent、真实14次embedding与最终649全量回归已验收，详情 `docs\现状文档\OCR-4工作台与真实RAG验收.md`。不自动索引其余文件，不把8样本命中当全库质量。

## 后台OCR实施记录（2026-10-05，OPT-065）

已部署与测试的范围见 `docs\现状文档\OCR-3后台任务与大文件验收.md`。新head rag_ocr_jobs；632全量通过，知识表0；网页原页/结构确认与真实索引仍待。

## OCR 选型后追加实施顺序（2026-10-05，OPT-063）

OPT-064 已落地显式本地adapter与结构联测，见 `docs\现状文档\MinerU-OCR接入与验收.md`。三套本地 CPU 配置已在相同 12 页扫描教材上实测；选型候选优先 MinerU basic/ONNX，不是默认拷贝 WeKnora 的云 VLM 链路。详见 `docs\现状文档\OCR与文档解析选型实测.md`。

- [x] 本地同样本评测、共同原页金标重评分和结构抽查；9 条评测工具回归，项目非集成 521 通过。
- [x] OCR-1（OPT-064）：本地 MinerU 4.x V1 adapter、显式 PDF 逐页路由与受限 CLI；超时/失败/版本/来源可见。不是网页自动 OCR。
- [x] OCR-2（OPT-064）：原生元素/HTML 合并格→DocumentBlock/Table，章/节/答案与gap上下文、原生bbox、脚注/快照和父子切块联测；真实3页/12个同行检查通过。复杂阅读顺序/跨页表格仍需核验。
- [x] OCR-3（OPT-065）：后台文档/页任务、受控本地批次/128MiB上传、独立队列/scheduler、进度/重试/缓存/取消/租约续跑；原Compose实际部署、真实87.3MB上传与取消续跑通过。未知识入库。
- [x] OCR-4（OPT-066）：工作台原页/结构核对、块排除/恢复、审核付费门控与原子索引；完整8页真实embedding与8条小样本检索通过。复杂跨页/全部题组/事实质量仍须核验。

这些是本轮新增的后续验收事项，**OCR-1/2/3已完成解析与后台API，不把这些结果标成已完成网页确认与知识生产入库**。GPU 吞吐、整书跨页语义、召回质量和许可证分发审查尚未验证。

## 原环境运行态与教材验证（2026-10-05，OPT-062）

原Compose五项核心服务已up，ready/真实Redis空任务/浏览器通过。课程15份扫描PDF341页无文字层，12份超10MiB。已修正代理上传上限、无文字提示/空覆盖率/索引门控，**尚未OCR或入库**。后续资料链路应优先接OCR和页级批次，再谈真实embedding召回效果；这不自动完成P2运营/金标。详情 `运行态与教材预览验收.md`。

## 启动前两项修复与数据库验证（2026-10-05）

- Compose backend/worker 透传全部 RAG_*、EMBEDDING_DIM/TIMEOUT，空 token limit 按 None处理。
- 集成 fixture 改为实际 Alembic/head 校验、非零差异向量和 PostgreSQL专项，不create_all/stamp/TRUNCATE。
- 复用现有 english-edit-ci-postgres，无新建环境；迁移至 rag_p1_def、21项真实集成通过，506项非集成通过，全量527断言通过；80%覆盖率门禁未过（本地约77.45%）。
- 详见 `RAG-部署与PostgreSQL验收.md`；主服务全栈随后在 OPT-062 验收；教材语义效果仍待，P2 G不因此自动完成。

## P1 D/E/F 实施记录（2026-10-04）

D → E → F 已落地：父子块/邻接/预算/引用；向量 + PostgreSQL FTS/字面关键词、RRF、可选/必需 rerank；canonical/别名/多标签/层级/关联与有界 query expansion。**不是完整 WeKnora 等价移植**。

- 新单 head `rag_p1_def`，旧正文/向量保留；索引与 keyword 回填只验证离线 SQL，未执行真实迁移。
- 默认 hybrid、exact、aliases；rerank 默认 off，LLM expansion 需显式配置才开启。
- 65 条新增测试；486 passed / 8 deselected；P1 限定模块覆盖率 95%，22 文件 scoped mypy，前端构建通过。
- 真实 ANN 查询计划、检索效果、供应商兼容性、账单、浏览器端到端和 P2 G 均未验证/未交付。
- PostgreSQL 关键词为 FTS + literal，不称 BM25；token/context 字节预算继续按保守方法；来源仍必须人工核验。
- 详情：`RAG-P1-DEF落地与验收.md`。

## P0 A/B/C 实施记录（2026-10-04）

**结论**：A/B/C 代码级落地，P1 D/E/F 与 P2 G 未实施。真实部署准入仍待验收。

- A：旧 API/租户/知识点/RAG 模式基线保留，完成 MIT 声明与 SHA-256 源码清单；新增 `knowledge_document` 和可空 chunk 字段，迁移 head 为 `rag_p0_abc`。
- B：结构化 Block；DOCX body-order 与表格、Markdown 表格、HTML 表格、PDF 逐页文本、XLSX/CSV 逐行字段；原文/规范化快照、来源定位、解析 warning。
- C：auto → heading/heuristic/recursive/legacy 验证与 fallback；句段边界、表头上下文、标题路径、源坐标、最小长度护栏、完整输入预算与诊断。
- 新增无模型调用的 `/api/knowledge/preview`，以及带租户 scope 的 chunk diagnostics；前端开放新格式、知识点标签、预览和 warning 展示。
- 本轮新增 **56 条单测**；全量非集成 **421 passed / 8 deselected**，RAG P0 相关 86 条回归通过；修改模块 scoped mypy 和格式检查通过，前端构建通过。详细证据见 `RAG-P0-ABC落地与验收.md`。

### 必须保留的验收边界

1. 不启动 Docker，不连接真实 Postgres/Redis，不调用真实 embedding/LLM。迁移只验证单 head、离线升降级 SQL，**部署前仍须执行 Alembic 迁移**。
2. PDF 当前仅逐页文本和警告；布局/OCR 属于后续版。复杂 HTML 合并单元格保留原始 HTML/span metadata 并告警，不声称视觉还原。
3. token_limit 使用完整 embedding 输入的 **UTF-8 字节保守预算**，不是供应商精确 tokenizer；预算过小时失败关闭或显式诊断，不偷偷截断来源。
4. 后续表格块表头在 `context_header` 补回，原始 `content` 不重复插入表头，保证规范化源坐标可核对。
5. 章节、页、表格边界可能产生合法短块并告警；超大表格行/代码/公式仍可能受控拆分，不能承诺永不切断语义。
6. 不复制整个 WeKnora，不宣称其全量行为等价；本地 VERSION=0.8.0，上游 commit 未确认，用逐文件 SHA-256 固定参考快照。

## 0. 文档定位与边界

`RAG现状.md` 是本计划的**现状基线和问题清单**，不是新的代码规范，也不替代仓库中的 `AGENTS.md`、`CLAUDE.md` 和 `.ai/rules/`。本文件是实施顺序、技术方案和验收标准，不应被解释为允许绕过项目既有的安全、测试、变更追踪或分层约束。

本计划的目标不是把 WeKnora 整个项目搬进英语教研平台，而是：

> **最大限度复用 WeKnora 源码中的 RAG 算法、数据结构、测试思路和工程经验，将适合本项目的能力移植到现有 Python + FastAPI + PostgreSQL/pgvector + Celery + LangGraph 架构中。**

具体原则：

1. **能直接复用算法就不重新设计**：尤其是自适应切块、标题上下文、表格保护、父子块、混合召回和 RRF。
2. **能移植实现就不做简化替代**：优先按 WeKnora 的行为和边界条件移植，而不是只复制函数名。
3. **不能直接复制运行时就复制设计契约**：WeKnora 的 Go 服务、依赖注入、存储抽象和完整 Agent 系统不直接搬入本项目。
4. **先解决内容完整性，再优化召回排序**：解析丢失和切块破坏语义未解决前，rerank 不能弥补基础数据损失。
5. **增量兼容现有 RAG**：已有 `knowledge_chunk`、知识点过滤、租户隔离、provenance 和 RAG `off/optional/required` 保留，逐步增强。
6. **所有外部代码先过许可证和依赖审查**：WeKnora 主体为 MIT，但其第三方组件存在不同许可证，不能将第三方代码、模型或依赖无条件复制。

---

# 1. 目标状态

## 1.1 目标 RAG 链路

### 入库链路

```text
文件上传
  ↓
文档类型识别与解析
  ↓
保留结构的中间文档模型
  ↓
段落/标题/表格/代码/列表等 Block 标准化
  ↓
文档 profile 分析
  ↓
自适应切块
  ↓
ContextHeader / section_path / provenance 注入
  ↓
Parent-Child 构建
  ↓
批量 Embedding
  ↓
向量索引 + 关键词索引
  ↓
文档、Block、Chunk、索引状态入库
```

### 检索链路

```text
用户请求 / knowledge_point
  ↓
查询归一化与知识点别名扩展
  ↓
向量召回
  + 关键词/BM25 召回
  + 可选 query expansion
  ↓
候选集去重
  ↓
RRF 融合
  ↓
可选 rerank
  ↓
相似度/相关性阈值
  ↓
Parent 或相邻 Chunk 上下文扩展
  ↓
上下文预算裁剪
  ↓
带来源、章节、页码和 chunk 坐标的 Prompt 上下文
  ↓
生成、质检、人工核验和 provenance 保存
```

## 1.2 目标能力分级

| 等级 | 目标能力 | 达标含义 |
|---|---|---|
| RAG-1 | 基础文本 RAG | TXT/Markdown 可稳定解析、切块和向量召回 |
| RAG-2 | 结构化文档 RAG | DOCX 表格、标题、PDF 页码和来源位置不丢失 |
| RAG-3 | 语义上下文 RAG | 自适应切块、ContextHeader、Parent-Child、邻接扩展 |
| RAG-4 | 高质量检索 RAG | 向量+关键词、RRF、rerank、query expansion |
| RAG-5 | 生产可运营 RAG | 重建索引、版本治理、诊断、评测、成本和失败恢复完整 |

当前项目约为 **RAG-1 的增强版**。本计划的阶段目标是先达到 RAG-2，再达到 RAG-3，最后按成本和实际收益选择是否完成 RAG-4/RAG-5。

---

# 2. WeKnora 能力复用策略

## 2.1 可以直接复用或高度参考的部分

| 能力 | WeKnora 参考位置 | 本项目处理方式 |
|---|---|---|
| 自适应切块策略 | `internal/infrastructure/chunker/strategy.go` | 移植策略契约和 fallback 顺序到 Python |
| 标题切块 | `internal/infrastructure/chunker/heading_splitter.go` | 移植标题层级识别和 breadcrumb 生成 |
| 启发式切块 | `internal/infrastructure/chunker/heuristic_splitter.go` | 移植句子/段落/分隔符优先级 |
| 递归切块 | `internal/infrastructure/chunker/splitter.go` | 移植递归分隔和 overlap 约束 |
| 表格保护 | `internal/infrastructure/chunker/header_tracker.go`、`patterns.go` | 移植表格检测、表头跟踪、后续块补表头 |
| Chunk 元数据 | `internal/infrastructure/chunker/splitter.go` | 增加 `context_header`、位置、序号、父子关系 |
| 父子 Chunk | `strategy.go`、`knowledge_process.go` | 在现有 `knowledge_chunk` 上增加 self-reference |
| DOCX 解析 | `docreader/parser/docx_parser.py`、相关测试 | 用 Python 现有依赖重新实现 body-order 解析 |
| Excel 解析 | `docreader/parser/excel_parser.py` | 参考行级文本、sheet metadata 和表格摘要设计 |
| HTML 表格 | `internal/infrastructure/docparser/html_table_normalizer.go` | 移植标准化原则；第一阶段可先转 Markdown |
| RRF 融合 | `knowledgebase_search_fusion.go` | 移植公式、去重和候选池处理 |
| rerank fallback | `internal/models/rerank`、搜索服务 | 设计为可选能力，失败时回退融合结果 |
| query expansion | `knowledgebase_search*` | 先定义接口，后接模型；不可用时保持原查询 |
| 诊断与验证 | `chunker/validator.go`、测试文件 | 移植 chunk 覆盖率、边界和表格完整性检查 |

## 2.2 不直接复制的部分

以下内容不属于本项目 RAG 优化范围，不直接搬入：

- WeKnora 的完整 Agent/Chat Pipeline
- 多租户 Workspace、复杂 API Key 和权限体系
- WeKnora 前端工作台
- Go 服务整体目录和依赖注入框架
- 多种向量数据库适配层
- 与本项目无关的连接器、MCP、Bot、Wiki、RSS 等能力
- 为通用对话产品设计的所有上下文和会话逻辑

本项目仍以现有架构为准：

```text
FastAPI → Service → RAG module → SQLAlchemy/PostgreSQL
                                  ↓
                         LangGraph 生成工作流
```

## 2.3 许可证和归属要求

WeKnora 根目录 `LICENSE` 声明主体为 MIT，但同时明确第三方组件受各自许可证约束。实施时必须：

1. 复制 WeKnora 主体代码片段时保留原版权和 MIT 许可声明。
2. 在本项目新增 `THIRD_PARTY_NOTICES` 或等价文档中记录来源、文件范围和许可证。
3. 不复制许可证未知的第三方代码、模型文件或生成资源。
4. 复用算法思想、数据结构和测试场景时，仍保留来源说明，便于后续审计。
5. 任何代码复制前先确认源文件所属许可证，不以根目录 MIT 自动覆盖所有子依赖。

---

# 3. 总体实施顺序

## 阶段总览

| 阶段 | 优先级 | 主题 | 主要结果 |
|---|---|---|---|
| A | P0 | 现有数据安全与兼容层 | 保护旧数据、固定行为基线、完成许可证审查 |
| B | P0 | 文档解析和内容完整性 | DOCX 表格、Markdown 表格、PDF 页码等不再静默丢失 |
| C | P0 | 自适应切块基础 | 修复病态切块、标题/句子/表格边界可识别 |
| D | P1 | 语义上下文和父子块 | ContextHeader、Parent-Child、邻接上下文 |
| E | P1 | 混合召回和排序 | 向量+关键词、RRF、候选池和可选 rerank |
| F | P1 | 知识点智能召回 | 别名、层级、多标签、query expansion |
| G | P2 | 生产化治理与评测 | 重建索引、版本、诊断、评测和运营指标 |

原则上先完成 A/B/C，再做 D/E/F。不能跳过 B/C 直接上 rerank。

---

# 4. 阶段 A：现有数据安全与兼容层（P0）

## A1. 固定当前行为基线

在改动前先补充纯内存单测和固定 fixture，冻结当前已知行为：

- TXT/Markdown 正常解析
- DOCX 段落解析
- PDF 页面拼接
- 当前 `knowledge_point` 精确过滤
- tenant scope
- `RAG_MODE=off/optional/required`
- provenance 生成
- 当前旧 `knowledge_chunk` 数据仍可读取

这些测试不是为了证明当前实现正确，而是为了防止移植 WeKnora 时无意破坏现有 P1 能力。

## A2. 许可证和来源清单

新增或维护一份来源清单，至少记录：

| 项目 | 内容 |
|---|---|
| 来源仓库 | `<本地WeKnora源码目录>` |
| 版本 | 以实际采用的 commit/tag 为准，不只写目录名 |
| 复制文件 | 逐文件列出 |
| 移植文件 | 记录原始参考文件 |
| 许可证 | MIT 或源文件实际许可证 |
| 本项目修改 | 记录 Python 化、架构适配和业务裁剪 |

## A3. 兼容策略

- 保留现有 `KnowledgeChunk` 查询入口。
- 新字段全部允许旧记录为空。
- 旧记录没有 `document_id`、`context_header`、`parent_chunk_id` 时，按单层 legacy chunk 处理。
- 旧数据不强制立即重嵌入。
- 新旧 chunk 通过 `chunker_version`、`embedding_model`、`embedding_dimension` 区分。
- 只有完成迁移和回归评测后，才切换默认索引策略。

### 阶段 A 验收

- 现有非集成测试通过。
- 旧知识库记录可查询、可删除、可生成引用。
- 新增字段不影响现有 API 响应。
- WeKnora 参考代码的来源和许可证记录完成。
- 不启动 Docker，不宣称真实数据库兼容已验证。

---

# 5. 阶段 B：文档解析和内容完整性（P0）

这是最优先的实际改造。解析阶段丢掉的内容，后续切块和检索都无法恢复。

## B1. 建立结构化中间文档模型

当前 `parse_file()` 直接返回字符串，需要升级为内部结构模型，同时保留旧字符串接口作为兼容层。

建议结构：

```python
class DocumentBlock(TypedDict):
    block_id: str
    block_type: Literal[
        "paragraph",
        "heading",
        "table",
        "table_row",
        "list",
        "code",
        "image",
        "page_break",
    ]
    text: str
    level: int | None
    page_no: int | None
    order: int
    source_locator: dict[str, Any]
    meta: dict[str, Any]

class ParsedDocument(TypedDict):
    source_name: str
    source_type: str
    parser_version: str
    blocks: list[DocumentBlock]
    warnings: list[str]
    stats: dict[str, Any]
```

不要求第一版一次支持所有 block 类型，但模型必须允许增量增加。

## B2. DOCX 按 body order 解析

参考 WeKnora 的 DOCX 解析思路，实现：

- 段落和表格按 Word 文档实际顺序交错输出。
- 表格转换为稳定的 Markdown/GFM 或结构化文本。
- 单元格中的 `|` 必须转义。
- 空单元格保持列位置。
- 合并单元格至少保留可解释文本，不允许静默丢失。
- 每个表格记录 `table_index`、`row_index`、`column_index`。
- 解析 warning 中记录复杂合并单元格、图片和无法还原的布局。

第一版不要求完美还原 Word 的视觉排版，但必须满足：

> 表格中的文字、行列关系和表头信息不能静默消失。

## B3. Markdown/HTML 表格标准化

- 识别 Markdown table。
- 规范化表头和分隔线。
- 处理单元格换行和 `|` 转义。
- HTML table 第一阶段可以转换为 GFM；复杂 `rowspan/colspan` 必须保留 warning 和结构化 metadata。
- 标准化结果同时保留原文快照，便于 provenance。

## B4. PDF 解析增强

按投入分两步：

### 第一版

- 保留 `page_no`。
- 每页单独形成 block，不在 parser 阶段直接无标记拼接。
- 记录空页、抽取失败页和抽取 warning。
- 对普通 PDF 做页面级文本抽取。

### 后续版

- 增加布局感知解析器。
- 对无文本页面接入 OCR adapter。
- 对表格尝试结构化提取。
- 不把 OCR 结果伪装成高可信原文，必须标记 `parser_quality=ocr`。

## B5. Excel/CSV 支持

第一版支持：

- `.xlsx`
- `.csv`

建议按以下方式生成 block：

```text
Workbook
  └── Sheet
        ├── table_summary block
        ├── table_column block
        └── row block × N
```

每行可以转为：

```text
表名：词汇表
Sheet：Unit 1
行号：12
词汇：abandon
词性：verb
释义：放弃
例句：...
```

不要只把 Excel 直接转换成无表头的 CSV 字符流，否则召回后仍然会丢失列语义。

### 阶段 B 验收

- DOCX 段落、表格、段落顺序测试通过。
- DOCX 表格内容完整性测试通过。
- Markdown 长表格原始行内容不丢失。
- PDF 每个 chunk 可以追溯到页码。
- Excel sheet、列名和行号可以追溯。
- 解析失败或降级时有结构化 warning，不静默成功。
- 纯单测完成；不启动 Docker。

---

# 6. 阶段 C：自适应切块基础（P0）

## C1. 先修复现有 `chunk_text()` 的硬伤

在引入完整策略前，先修复：

1. 换行边界导致的递减小 chunk。
2. `overlap >= max_chars` 的非法配置。
3. 空 chunk 和重复 chunk。
4. 单个超长段落的硬切死循环或低质量切分。
5. chunk 数量异常增长。
6. 没有最小 chunk 长度保护。

每轮切分必须满足：

```text
start 单调前进
每个非末尾 chunk 有效长度大于最小阈值
不产生无限重复
原文非空字符覆盖率可计算
```

## C2. 移植 WeKnora 的分层策略

建议 Python 侧 API：

```python
class ChunkingConfig(BaseModel):
    strategy: Literal["auto", "heading", "heuristic", "recursive", "legacy"] = "auto"
    chunk_size: int = 512
    chunk_overlap: int = 80
    token_limit: int | None = None
    separators: list[str] = ["\n\n", "\n", "。", ". ", ";", ",", " "]
    preserve_tables: bool = True
    preserve_code_blocks: bool = True
    min_chunk_chars: int = 80
```

`auto` 的建议顺序：

```text
heading → heuristic → recursive → legacy
```

每层失败或违反验证规则时进入下一层，不允许无诊断地返回异常 chunk。

## C3. 标题和章节上下文

每个 chunk 增加：

```text
context_header
section_path
heading_level
```

例如：

```text
英语语法 > 时态 > 一般现在时 > 第三人称单数
```

原文 `content` 保持纯正文，不将标题强行写回原文坐标；embedding 使用：

```text
context_header + "\n\n" + content
```

生成上下文可以根据配置决定是否展示标题路径。

## C4. 表格保护和表头跟踪

移植 WeKnora 的 protected pattern/header tracker 思路：

- 表格开始时识别 header 和 separator。
- 表格内部优先按完整行切分。
- 不在单元格中间切分，除非单行超过绝对上限。
- 后续 chunk 自动补回表头。
- 表格切换时重置 header。
- 保留表格 `table_id` 和 `row_range`。
- 超大行仍允许硬切，但必须设置 warning。

目标不是“表格永远不拆”，而是：

> 表格即使被拆分，后续 chunk 也不丢列语义，且能够追溯到原始表格和行范围。

## C5. Chunk 验证和诊断

每次切块输出诊断信息：

```python
ChunkDiagnostics(
    strategy_used="heuristic",
    chunk_count=12,
    min_length=102,
    max_length=508,
    overlap_actual=76,
    table_chunks=3,
    hard_splits=1,
    coverage_ratio=1.0,
    warnings=[],
)
```

必须检查：

- 原文覆盖率
- chunk 顺序
- start/end 是否越界
- 是否有重复或近重复
- 表格表头是否存在
- heading/context header 是否正确
- chunk 长度是否超过 token limit
- 是否出现异常小 chunk

### 阶段 C 验收

- 现有病态换行样例不再生成递减小 chunk。
- 长句、长段落、中文和英文混合文本均有边界测试。
- Markdown 表格后续块携带表头。
- 代码块、公式或特殊文本不被普通分隔符错误切断，至少有保护或 warning。
- `auto` 策略失败时有明确 fallback 和诊断。
- 完成纯内存单测和 fixture 测试，不启动 Docker。

---

# 7. 阶段 D：语义上下文和 Parent-Child（P1）

## D1. 数据模型增量设计

建议新增 `knowledge_document`，并扩展 `knowledge_chunk`。不立即删除旧字段。

### `knowledge_document`

建议字段：

```text
id
tenant_id
source_name
source_type
content_hash
parser_version
parser_quality
status
block_count
chunk_count
warnings JSONB
meta JSONB
created_at
updated_at
```

### `knowledge_chunk` 增量字段

```text
document_id
parent_chunk_id
chunk_index
chunk_type
content_hash
context_header
section_path JSONB
content_start
content_end
page_no
block_ids JSONB
prev_chunk_id
next_chunk_id
chunker_version
embedding_model
embedding_dimension
embedding_content_hash
meta JSONB
```

实际字段以迁移设计为准，但必须覆盖：

- 文档归属
- 父子关系
- 原文位置
- 章节上下文
- 解析/切块/embedding 版本
- 前后文关系

## D2. Parent-Child 策略

建议默认：

```text
Parent：约 2,000～4,000 字符，保持章节或表格上下文
Child：约 384～512 字符，用于 embedding 和初始召回
```

召回流程：

```text
召回 child
  ↓
按 parent_chunk_id 聚合
  ↓
对 parent 去重
  ↓
可选加入 child 前后邻居
  ↓
按上下文预算截断
```

注意：不是每种资料都强制 parent-child。短文档、表格摘要和单个完整段落可以只使用单层 chunk。

## D3. 邻接上下文

当命中 child 位于一个连续章节中时，可以按配置扩展：

- 前 1 个 chunk
- 后 1 个 chunk
- 同一 parent
- 同一 table_id 的表头

扩展必须受到：

- 总字符/token 预算
- 租户 scope
- 同一 document 限制
- 去重规则

约束，不能无限拼接整个文档。

### 阶段 D 验收

- 命中 child 可以稳定返回 parent。
- parent/child citation 关系可追溯。
- 同一请求不会重复注入相同内容。
- 旧单层 chunk 查询行为不被破坏。
- 章节标题和表头在返回上下文中保留。

---

# 8. 阶段 E：混合召回与排序（P1）

## E1. 候选池扩大

当前是数据库取 `top_k` 后才做相似度阈值过滤。需要改为：

```text
初始候选池 top_n = 20～50
  ↓
向量阈值/关键词过滤
  ↓
融合和去重
  ↓
rerank
  ↓
最终返回 top_k = 3～8
```

`top_n`、`top_k`、阈值必须配置化，不能硬编码。

## E2. 向量召回

保留现有 pgvector，但增加：

- embedding 模型和版本校验
- 维度校验
- ANN 索引迁移脚本
- tenant/document/knowledge scope 过滤
- 可选 parent/child 过滤
- 候选池参数

优先确认 PostgreSQL 的 HNSW 或 IVFFlat 索引方案，并通过 explain/基准测试确认实际使用情况。

## E3. 关键词召回

第一阶段建议使用 PostgreSQL 原生全文检索或可维护的关键词字段，不立即引入额外搜索集群。

需要支持：

- 英文词项
- 中文知识点
- 精确语法术语
- 数字和符号
- 题号/选项/缩写
- source_name 和 section_path 过滤

如果 PostgreSQL 全文检索对中英文混合教材效果不足，再评估独立 BM25 服务。

## E4. RRF 融合

移植 WeKnora 的 RRF 思路：

```text
score(d) = Σ 1 / (k + rank_i(d))
```

其中：

- `d` 是候选 chunk
- `rank_i` 是 chunk 在第 i 路召回中的排名
- `k` 是可配置常数，建议默认 60

融合时必须：

- 使用稳定 chunk ID 去重
- 记录每路 rank 和分数
- 保留 vector/keyword 命中来源
- 支持某一路失败时降级

## E5. Rerank

rerank 作为可选能力：

```text
RAG_RERANK_MODE=off|optional|required
```

要求：

- 接口与 embedding/LLM provider 解耦。
- 调用失败时 `optional` 回退 RRF 结果。
- `required` 模式下失败关闭，不伪装为高质量命中。
- 记录模型、耗时、候选数、阈值和降级原因。
- 不将未经验证的 rerank 分数当作事实正确性证明。

### 阶段 E 验收

- 精确术语查询能够被关键词召回补足。
- 同义表达能够由向量召回覆盖。
- 单一路由失败时系统有明确降级状态。
- RRF 去重和排序有纯单测。
- top_k 过滤不会因为候选池过小而提前丢弃有效结果。
- rerank 无真实供应商时使用 mock，不能宣称真实模型效果。

---

# 9. 阶段 F：知识点智能召回（P1）

当前知识点是字符串精确匹配，需要逐步升级为配置和 metadata 驱动的知识点体系。

## F1. 知识点标准化

建立知识点目录或配置表：

```text
canonical_name
aliases
parent_id
level
subject
stage
language
status
```

例如：

```text
canonical_name: 一般现在时
aliases: [现在时, present simple, simple present]
parent: 时态
```

上传和生成请求先标准化到 canonical knowledge point，再参与过滤和召回。

## F2. 多标签和层级召回

`knowledge_chunk` 不应只保留单个 `knowledge_point` 字符串。建议逐步增加：

- canonical knowledge point
- knowledge point IDs
- ancestor IDs
- aliases
- tags JSONB 或关系表

召回策略可以配置为：

```text
exact：只召回同一知识点
ancestor：召回父级知识点
related：召回关联知识点
semantic：不做标签硬过滤，仅使用语义召回
```

默认仍保持安全的 exact 模式，避免跨知识点污染；只有评测证明收益后才放宽。

## F3. Query expansion

第一阶段不强依赖 LLM，可先做：

- canonical name
- alias
- 英文名
- 常见缩写
- 课程/教材中的同义表达

第二阶段再接可选 LLM query expansion，必须限制：

- 最大扩展数量
- 词数和 token 预算
- 租户和知识点 scope
- 超时和失败回退

### 阶段 F 验收

- “现在时”可以配置性映射到“一般现在时”。
- 英文知识点别名可以命中中文标签对应的资料。
- exact 模式不会扩大到未授权知识点。
- query expansion 失败时原查询仍可用。
- 召回日志能区分 exact、alias、semantic、expanded 命中来源。

---

# 10. 阶段 G：生产化治理和评测（P2）

## G1. 索引和版本治理

每个 chunk 必须能知道：

```text
parser_version
chunker_version
embedding_model
embedding_dimension
embedding_content_hash
indexed_at
```

当以下内容变化时，应支持重建索引：

- parser 版本
- chunker 版本
- embedding 模型
- embedding 维度
- 文档原文
- 知识点标签

重建必须支持：

- 单文档重建
- 单租户重建
- 失败重试
- 并发控制
- 新旧索引切换
- 旧索引清理
- 进度和错误记录

## G2. 解析和切块预览

新增知识库调试能力：

- 原始文件解析预览
- block 列表预览
- chunk 预览
- parent-child 关系预览
- 表格识别结果
- source locator
- embedding 输入预览
- parser/chunker warning

这是生产调试必需能力，不能只依赖数据库人工查看。

## G3. 检索调试接口

提供内部或管理员接口，返回：

```json
{
  "query": "一般现在时",
  "normalized_query": "一般现在时",
  "knowledge_point": "一般现在时",
  "vector_candidates": [],
  "keyword_candidates": [],
  "fusion_candidates": [],
  "rerank_candidates": [],
  "final_context": [],
  "expansion_terms": [],
  "fallbacks": [],
  "latency_ms": {}
}
```

该接口只用于诊断，必须继续遵守 tenant scope 和权限控制。

## G4. 评测集

建立至少四类固定数据集：

1. 纯文本语法规则。
2. Word 段落+表格混合资料。
3. PDF 教材和试卷。
4. Excel 词汇/语法知识表。

每条样本至少标注：

```text
query
knowledge_point
relevant_document
relevant_chunk/row/page
expected_context
answer_constraints
```

核心指标：

| 指标 | 说明 |
|---|---|
| Recall@K | 相关 chunk 是否进入候选集 |
| MRR | 第一个相关 chunk 的排名 |
| NDCG | 多个相关 chunk 的排序质量 |
| Knowledge Point Accuracy | 知识点范围是否正确 |
| Table Row/Column Recall | 表格行列语义是否完整 |
| Citation Accuracy | 引用是否真实支持生成内容 |
| Context Sufficiency | 上下文是否足够回答问题 |
| No-context Rate | 需要来源却无有效来源的比例 |
| RAG Degraded Rate | embedding/检索/rerank 降级比例 |
| Indexing Failure Rate | 文档解析和入库失败率 |

## G5. 生产准入门槛

在没有真实 PostgreSQL、真实 embedding 和真实文档批量测试之前，只能标记为：

```text
代码级通过 / 纯单测通过 / 未完成部署验证
```

不能宣称：

- 召回率达标
- 大规模性能达标
- 真实 OCR 效果达标
- 生产级高可用达标
- 完整 WeKnora 等价

---

# 11. 建议的文件和模块落点

## 11.1 保留并增强的模块

```text
backend/app/rag/parser.py
backend/app/rag/indexer.py
backend/app/rag/retriever.py
backend/app/rag/embedding.py
```

建议逐步拆分为：

```text
backend/app/rag/
├── parser.py                 # 兼容入口
├── parsers/
│   ├── base.py
│   ├── text_parser.py
│   ├── docx_parser.py
│   ├── pdf_parser.py
│   ├── spreadsheet_parser.py
│   └── html_parser.py
├── document.py                # ParsedDocument/DocumentBlock
├── chunking/
│   ├── models.py
│   ├── strategy.py
│   ├── heading.py
│   ├── heuristic.py
│   ├── recursive.py
│   ├── table_tracker.py
│   ├── header.py
│   └── validator.py
├── indexing/
│   ├── indexer.py
│   ├── reindex.py
│   └── versioning.py
├── retrieval/
│   ├── vector.py
│   ├── keyword.py
│   ├── fusion.py
│   ├── rerank.py
│   ├── expansion.py
│   └── context.py
├── provenance.py
└── diagnostics.py
```

第一阶段不需要一次建立全部目录；应以可测试的垂直切片逐步增加。

## 11.2 建议新增服务

```text
backend/app/services/knowledge_ingestion_service.py
backend/app/services/knowledge_reindex_service.py
backend/app/services/knowledge_retrieval_service.py
```

职责：

- parser 只负责解析
- chunker 只负责切块
- indexer 只负责 embedding 和持久化
- retriever 只负责召回和上下文组装
- service 负责事务、权限、任务状态和业务编排

不得把解析、切块、embedding、数据库写入全部继续堆在一个函数中。

---

# 12. 具体执行批次

## 批次 RAG-A：P0 基线和导入安全

任务：

- [x] 固定现有 RAG 单测基线。
- [x] 建立 WeKnora 来源和许可证清单。
- [x] 设计并实现 `knowledge_document` + `knowledge_chunk` 可空增量字段迁移（`rag_p0_abc`）；旧 content/vector 不回填、不重嵌入，Parent-Child 仍留待 P1。
- [x] 写入 parser/chunker 版本、embedding 模型/维度/input hash、文档 source hash 与规范化 content hash；旧记录相关字段为空。
- [x] 兼容旧 `KnowledgeChunk` 的列表、检索、来源引用、诊断和删除；不自动重嵌入旧向量。

完成标志：

- 方案和迁移设计评审通过。
- 不改变现有生产行为。

## 批次 RAG-B：P0 文档解析

任务：

- [x] DOCX body-order 段落+表格解析。
- [x] Markdown 表格标准化。
- [x] PDF page block 和 source locator。
- [x] Excel/CSV 基础解析。
- [x] parser warning 和统计信息。
- [x] 原始内容 hash。

完成标志：

- 表格内容不再静默丢失。
- 所有解析结果可预览、可诊断、可追溯。

## 批次 RAG-C：P0 自适应切块

任务：

- [x] 修复现有固定切块病态循环。
- [x] 移植 auto/heading/heuristic/recursive 策略。
- [x] 移植表格 protected pattern 和 header tracker。
- [x] 增加 ContextHeader 和 section_path。
- [x] 增加 chunk validator 和 diagnostics。

完成标志：

- 长文本、标题、表格、超长行均有稳定结果。
- chunk 诊断可以解释为什么这样切。

## 批次 RAG-D：P1 Parent-Child 和上下文扩展

任务：

- [x] parent/child 数据结构。
- [x] child 召回、parent 返回。
- [x] 相邻 chunk 扩展。
- [x] 表头/标题/页码上下文拼装。
- [x] context budget。

完成标志：

- 召回结果比当前单层 chunk 更完整，但不会无限扩大 Prompt。

## 批次 RAG-E：P1 混合召回

任务：

- [x] 扩大候选池。
- [x] pgvector ANN 索引迁移与离线 SQL 验证（真实执行计划/压测仍待验收）。
- [x] PostgreSQL 关键词召回。
- [x] RRF 融合。
- [x] 去重和稳定排序。
- [x] 可选 rerank adapter。

完成标志：

- 精确术语和语义表达均有覆盖。
- 任何单一路径失败均有可观察降级。

## 批次 RAG-F：P1 知识点增强

任务：

- [x] 知识点 canonical/alias/parent 配置。
- [x] 多标签和层级过滤。
- [x] query normalization。
- [x] query expansion adapter。
- [x] exact/ancestor/descendant/related/semantic 模式。

完成标志：

- 知识点召回不再依赖字符串完全一致。
- 默认策略仍不会无控制地跨知识点污染。

## 批次 RAG-G：P2 生产化与评测

任务：

- [x] OCR 来源单文档显式重建/活动版本/撤除及失败保留旧索引（OPT-067）。
- [ ] 全量单租户/legacy/非 OCR 文档重建、历史回滚与长期存储治理。
- [x] chunk/source/scope/candidate/fusion/fallback 调试基础入口（P0/P1、OCR工作台）；规模运营增强仍待。
- [x] 真实三资料36条有限集与rag_eval.py v3、legacy/bigram统一A/B（OPT-068）：原18+新16上下文齐备，另2跨页仅1齐备；页/块/同行/全部单位与失败分开，不是答案正确率。
- [ ] 纯文本/DOCX/PDF/XLSX 四类完整固定集及 chunk/表行/事实正确率。
- [ ] 真实 PostgreSQL/embedding 压测计划。
- [ ] 成本、延迟、失败率看板。
- [ ] 生产准入报告。

完成标志：

- 能够用数据证明优化是否有效，而不是只凭主观判断。

---

# 13. 测试和验证策略

## 13.1 历史阶段：2026-10-04 不启动 Docker（已被用户后续授权更新）

遵守当前约束，前几个批次只做：

- 纯 Python 单测
- 内存 fixture
- mock embedding
- mock reranker
- mock parser provider
- SQL 语句结构检查
- chunk diagnostics 验证
- 静态类型和 lint 检查

不宣称：

- 真实 PostgreSQL 向量查询性能已验证
- HNSW/IVFFlat 真实执行计划已验证
- Redis/Celery 断点续跑已验证
- OCR/真实文档服务效果已验证

## 13.2 必须补充的回归样例

### DOCX

- 段落-表格-段落交错。
- 空单元格。
- 合并单元格。
- 单元格中包含 `|`。
- 长表格。

### Markdown

- 多列表格。
- 长表格。
- 表格前后标题。
- 单元格换行。
- 普通文本和表格混合。

### PDF

- 多页文本。
- 空页。
- 双栏文本。
- 页面内表格。
- 无文本扫描页的降级 warning。

### Chunk

- 无换行超长段落。
- 换行紧邻边界。
- 中文、英文和数字混合。
- 标题层级。
- 代码块和公式。
- 表格超长行。
- overlap 大于 chunk size。

### Retrieval

- 精确术语。
- 同义知识点。
- 中文查询召回英文资料。
- 关键词命中但向量分数较低。
- 向量命中但关键词不一致。
- 一路召回失败。
- rerank 失败。
- required RAG 无有效来源时失败关闭。
- tenant scope 和 admin scope。

---

# 14. 风险和取舍

## 14.1 “直接抄 WeKnora”不等于直接复制整个项目

最大的风险不是算法，而是架构错配：

- WeKnora 主要是 Go 服务，不能直接放入 Python backend。
- WeKnora 面向通用知识库和 Agent，不完全等同于英语题目生产。
- 其依赖的解析器、OCR、表格库可能引入较重的部署成本。
- 直接复制大块代码会破坏当前项目分层、测试和迁移策略。

因此执行标准是：

> **算法行为尽量对齐，代码实现按当前技术栈重写，来源和许可证完整保留。**

## 14.2 表格摘要使用 LLM 的风险

WeKnora 的表格摘要/列描述能力可以参考，但不要一开始就强依赖 LLM：

- 会增加成本和延迟。
- 可能产生摘要幻觉。
- 原始表格仍必须保留，摘要不能替代原文。
- 摘要应标记为派生内容，不能和原始事实混同。

建议先完成原始表格行级召回，再以可选能力增加表格摘要。

## 14.3 混合检索的复杂度

混合召回会增加：

- 索引维护成本
- 查询延迟
- 参数调优工作
- 调试复杂度

因此先使用 PostgreSQL 内置能力，只有评测证明不足时再引入外部搜索引擎。

## 14.4 迁移成本

新增 document/block/parent-child/version 字段会涉及：

- Alembic 迁移
- 旧记录兼容
- API 兼容
- 前端知识库页面调整
- 删除和重建索引逻辑
- provenance 格式演进

必须按阶段提交，不能把所有 WeKnora 能力一次性混入一个大 PR。

---

# 15. 最终实施建议

## 第一阶段不要做的事

暂时不要：

- 直接复制整个 WeKnora 仓库。
- 一开始就接入完整 OCR/多向量数据库。
- 一开始就强制 LLM 表格摘要。
- 一开始就把 rerank 设为必需依赖。
- 删除或重写现有 `knowledge_chunk` API。
- 在没有评测集时盲目调 chunk size、overlap 和阈值。

## 第一阶段必须做的事

优先完成：

1. DOCX 表格按 body order 保留。
2. Markdown 表格保护和表头补全。
3. 修复现有切块病态循环。
4. 增加标题上下文和 source locator。
5. 增加 chunk validator/diagnostics。
6. 建立 document/chunk versioning。
7. 用固定 fixture 证明内容没有丢失。

## 推荐目标

短期目标不是“复制 WeKnora 的全部功能”，而是达到：

> **英语教研常见教材、试卷和语法资料进入知识库后，原始内容不静默丢失，表格可解释，chunk 可追溯，召回上下文不残缺，且现有租户、来源核验和生成链路不被破坏。**

达到该目标后，再根据评测结果决定是否投入 rerank、query expansion、OCR 和外部搜索引擎。

---

# 16. 方案结论

可以抄，可以移植，也应该大量借鉴 WeKnora；但应当抄它的：

```text
解析策略
切块策略
表格保护
标题上下文
父子块
混合检索
RRF
rerank fallback
诊断和测试边界
```

不应直接抄它的：

```text
完整服务架构
Agent/Chat 系统
无关业务模块
多租户产品层
全部第三方依赖
未知许可证代码
```

最终路线是：

```text
内容不丢
  ↓
结构不坏
  ↓
上下文完整
  ↓
向量+关键词召回
  ↓
RRF/rerank
  ↓
知识点智能扩展
  ↓
评测和生产治理
```

这条路线比直接修改几个参数更重要，也比一次性复制 WeKnora 全部代码更安全、更容易验证和维护。
