# RAG STR-1 / STR-2：文档级结构与关系上下文落地验收

> **后续状态**：OPT-070已交付STR-3/4，head更新rag_str34_boundary、811/81.63%、旧8由7→8齐备。本文保留OPT-069阶段证据，未完成描述为当时状态；当前见 `docs\现状文档\RAG-STR3-STR4落地与验收.md`。

**变更**：OPT-069
**记录日期**：2026-10-05（UTC；本机Asia/Shanghai执行跨午夜，以私有日志的时区时间戳复核）
**状态**：STR-1、STR-2已完整实现并部署原环境；STR-3～STR-6没有被标成完成。

## 1. 完成交付合同

| 范围 | 已实现 | 验证 |
|---|---|---|
| STR-1 文档级结构 | 纯函数章节/知识单元/来源块/关系计划、布局家具、连续页小节继承、章编号别名、屏障与版本/hash | 真实页2/3拓扑、模拟正反例、原环境免费预览 |
| 审核 | accepted/proposed/rejected关系，未知/越屏障ID拒绝、原源保留、OCR plan hash绑定、免费已索引资料结构审核 | hash/版本/费用前门控、权限、真实工作台保存 |
| STR-2 small-to-big | 从当前有效leaf按accepted关系有界扩展，跨页跨段、正文表格关联；不读取原snapshot正文补删块 | 原36gold、独立8gold、删除/tenant/范围/预算/真实PG |
| 协议 | rag-context-v2 bundle+实际source_segments，旧snippets/citations对齐保留；可选legacy | API/前端切换、生成节点/provenance/人工审核快照、评测v4 |
| 运行与变更追踪 | 原Compose部署、共享配置、policy入包、任务/规则/现状同步 | health/代理200、Nginx、前端/镜像、最终回归 |

“完整落地”指本阶段功能/边界/协议都已交付，不代表所有未知资料都能自动正确识别，或STR-4复合问题、全库事实质量已达标。

## 2. STR-1结构实现

新增 `backend\app\rag\structure\`：

- `builder.py`纯函数，不调用embedding/OCR/chat，不修改输入blocks/text/原offset/hash。派生sections、units、blocks/source segments、edges、barriers。
- `policy.yml`配置章/节/子节、布局家具、例句/注意例外、题组/答案/代码、原生表注角色和表格引用；策略原文hash入签名，打包声明保留YAML。
- `runtime.py`只读绑定活动文档/leaf、源范围签名和结构审核版本；新索引保存结构版本/签名/决定，旧资料可以不重嵌入地在查询时派生。

### 2.1 修复实际边界

- Native header/footer/page_number保留为来源，不当正文标题；重复“第二章|并列句”不重置“(二)表选择、条件关系”。
- 中文/阿拉伯章编号可归一（第二章/第2章）；header明确指出不同章、未分类短章标记、真正正文新章都形成屏障，不能直接粘连。
- 连续物理页的小节/知识单元延续；缺页、不连续选页、被排除块的order空洞、空/未解析页、工作表变更及独立题目/答案/代码都是屏障。
- 未知无标题段落、没有证据的正文-表格邻接只生成proposed，不自动当同一知识点；确认/拒绝可人工保存，越屏障连接根本不是可审核候选。
- `shared_structural_section`、`continuous_physical_pages`、明确表格引用和原生table relationship证据分开，不把几何邻接/人工声明伪装成事实验证。

真实并列句原资料派生22节/54单元/125来源块，修复后页2规则与页3续例同逻辑单元。数字只是该资料结构计划规模，不是质量分数。

### 2.2 审核与免费接口

- 原文件预览 `/api/knowledge/preview` 增加结构计划，仍零付费/不入库。
- OCR `/review-plan` 接受 `accepted_edge_ids` / `rejected_edge_ids`，派生签名进入plan_hash，费用前/worker领取后都重新核对。排除变化导致无效旧关系时明确拒绝，不静默丢审核决定。
- `GET /api/knowledge/documents/{id}/structure`：活动索引结构免费预览。
- `POST /api/knowledge/documents/{id}/structure-review`：requires ops:write、tenant归属、source_reviewed=true、review_signature与expected_index_revision；pending/indexing或partial_index拒改。
- 只更新KnowledgeDocument.meta结构审核rev，不改变源/leaf向量/index rev。存活审核决定会在OCR工作台重新打开时恢复。
- 沿用原权限矩阵：viewer/reviewer原本无ops权限，仍被拒绝；未增加“只读知识库”新角色或扩大权限。

## 3. STR-2关系感知上下文

新增 `rag/retrieval/relation.py`：

1. 原hybrid/RRF/rerank候选和权重不变。
2. 识别seed对应逻辑单元；同组件种子去重，按accepted边双向有界收集成员。
3. 正文仅来自当前活动、同document/tenant/knowledge/source/section scope的leaf，原文snapshot只用于分类/校验/位置，不用于body补齐。
4. partial/stale文档、旧审核签名/策略变化、无结构/legacy来源采用不扩展的安全退路并标原因；过期结构不能把已删除文本恢复。
5. seed正文优先获得预算，再补关系上下文；最终按原source order发布，每段记录实际内容/hash/page/block/Unicode坐标/原bbox精度。
6. 字符、UTF8字节、max member/hop/segment上限全部代码约束，图循环不无限走。低预算不输出仅标题导航冒充知识正文。
7. 表头优先，完整表行受保护；按实际来源行分段，渲染时恢复连续Markdown表，而不是在每一行中插来源标签破坏表格。超长硬切行/未送成员显式partial/incomplete。

### 3.1 兼容与引用

- `RAG_CONTEXT_MODE=legacy`：v1页内行为仍可使用；代码/示例环境默认为legacy以保持接入兼容。
- 原运行根`.env`已显式`RAG_CONTEXT_MODE=relation`；前端默认结构关联并可切回旧版。其他provider/JWT/admin等配置没改。
- v2保留一条snippet配一条bundle citation，返回数≤top_k；新增`protocol_version`、`bundles`、seed/bundle/segment计数。
- 一个bundle的`source_segments`可超过top_k。多页bundle用pages集合，但每段有自己的真实page；组合体content_start/end为None，不伪造跨页单跨度。
- 仅实际发出的leaf片段计matched IDs；纯导航标题、布局家具不算正文证据。旧leaf横跨多个逻辑单元时只选择主要单元并标原因，不直接聚合其包含的独立答案。
- `complete`是已确认结构成员的materialization状态，不是“问题回答正确”。未知/proposed关系不因总召回高就自动变accepted。

### 3.2 生成/质检/provenance同步

- graph.generate_node使用模板rag.context_mode或运行配置，RAG上下文仍独立注入参数副本，不修改用户原params。
- `build_rag_context`将实际source segments平铺到内容/Trace provenance，保留bundle列表，不能只记录seed。
- 新 `rag/provenance.py` 的人工核验快照去重chunk ID，但保留每个segment ID及内容hash/文档/页/坐标/结构签名/index rev；相同leaf的多段表行不能互相覆盖。
- 每段仍`verification=unverified`；源/结构核对并非机器事实认证。真实生成/judge未调用，生成节点和人工快照集成以mock provider+真数据库测试验证，不宣称题目事实正确率已实测。

## 4. 配置与运营安全

| 配置 | 本轮默认/限制 |
|---|---|
| RAG_CONTEXT_MODE | 代码legacy；原部署relation |
| RAG_STRUCTURE_ENABLED | true |
| RAG_STRUCTURE_MAX_BLOCKS | 10000，1–20000 |
| RAG_STRUCTURE_MAX_LEAFS | 2000，1–10000 |
| RAG_CONTEXT_BUNDLE_MAX_MEMBERS | 24，1–128 |
| RAG_CONTEXT_BUNDLE_MAX_HOPS | 32，1–128 |
| RAG_CONTEXT_MAX_SEGMENTS | 128，1–512 |
| 原上下文字符/字节预算 | 继续沿用8192与可选保守UTF8上限，不冒充真实token计数 |

API/生成worker/OCR worker/scheduler共享Compose变量，反例和非默认透传回归保留。root只添加relation模式开关；无大模型权重/新推理服务安装。

查询使用短源文档share锁；重建仅在新向量齐备后的提交阶段锁原document再替换，避免查询期间结构与源半更新，也不在embedding期间堵住旧索引查询。原owner/expected revision/原子提交仍保留，无新迁移；head仍rag_ocr_review。

## 5. 真实检索A/B

### 方法

- 复用完整并列句8页与名词页2/16、动词页1，3doc/52非零1024维leaf向量+2parent；不重嵌入。
- 原18+旧holdout16+跨页2共36条合并成回归集，旧相关单位和词项不改。
- 新增8条在检索前冻结，未用来调节策略，涵盖跨页续例、跨段注意事项、同行、正文/例句及多小节/多文档联合问题。
- 比较唯一开关legacy_context vs relation_context，hybrid/pool30/RRF60/top_k按同gold，query expansion进程off/rerankoff，所有原文/向量指纹相同，无fallback。运行服务aliases默认保持。
- `rag_eval.py` v4按实际segments评分，不再把bundle的claimed child IDs或第一页当全部证据，不将segment列表再裁到top_k。MRR以bundle/seed排序位置计算，统计实际segment数。

| 指标 | legacy | relation |
|---|---:|---:|
| 36回归上下文齐备 | 35/36 | **36/36** |
| 36回归精确单位平均recall | 0.986111 | **1.000000** |
| 36回归精确单位NDCG | 0.960234 | 0.973210 |
| 原2跨页全部来源 | 1/2 | **2/2** |
| 新8上下文齐备 | 6/8 | **7/8** |
| 新8精确单位平均recall | 0.875000 | 0.937500 |
| 新8精确单位NDCG | 0.903287 | 0.951643 |
| 36回归同行case/tuple | 10/10、13/13 | 相同 |
| 新8同行case/tuple | 2/2、4/4 | 相同 |

以上是标注支持/结构上下文，不是答案正确率、原页OCR逐字准确率、规模性能或专家双盲事实金标。

### 5.1 原失败实修

“or表示选择关系和表示否则时，分别有哪些例句？”：同一问题/预算/候选池下，命中页2规则seed后，沿确认知识单元返回页3Hurry up续例；不用把第11位续例强行调进top5，也没给单题增加scope/路由。

### 5.2 新失败保留

“for和because表示原因时句子位置，以及so表因果的例句是什么？”仍缺一个独立小节的证据，relation新集只报7/8。不是连续页续例，而是跨小节复合问题覆盖；STR-4的多种子/子问题/明确引用导航仍待。未改gold、强制增top_k或用人工拼文本冒充已修复。

### 5.3 实际界面发现与回归

初次UI看到参考答案段被同一节聚合，1.A/2.A等旧格式没有被角色规则识别。本轮修正配置，使编号答案逐题形成屏障，补2个回归；不聚合不相关答案凑上下文。

初次A/B原报告留`initial-ab/`；修正后原数据/同gold再次真实全量对照，结果仍为36/36与7/8。此修正来自安全/结构反例，不是依据新8问题调参。已有结构审核签名因policy hash改变而失效，工作台免费重新保存了同默认决定；没有静默自动再批准过期结构。

## 6. 测试/部署/账目

- 最新全量 **751 passed / 32真实PG / 719非集成**，新增63非集成+4真实PG，共67条；18既有warning保留。
- 全app精确覆盖率 **6822/8467 = 80.5716%**，从79.51%提高并越过80%数值门槛；另跑仅`--cov=app --cov-fail-under=80`同CI口径，不混入评测脚本、不放宽阈值。远端CI/PR没有自动提交或触发。
- 13个本轮RAG/服务/评测/config模块scoped strict mypy通过；config原三处既有未注解泛型/构造调用通过类型注解修正，未改价格语义。不是全app所有模块strict检查通过声明。
- 21个修改Python文件black/isort、后端flake8、TS/Vite和真实镜像部署通过。
- 真实Postgres同源跨页/hash/free review/no paid reindex/删除阻断/旧路径通过；只清理测试自有UUID事务，无业务清库。
- Playwright真实前端免费结构预览/核对声明门控/保存成功，原index rev2保持、结构rev独立为2；新策略实际返回页2/3来源，旧策略切换成功；OCR名词block:15仍勾选排除，pageerror0。
- 本轮真实embedding **181次**，供应商报告prompt tokens **2037**，全部成功；含初次/最终两轮A/B、pilot和两次UI。Trace配置fallback估算 **0.004074**，币种/实际供应商账单未核验；无chat/judge/rerank、新OCR或知识重嵌入。
- 主库原task3/content4/user1、OCRjob4/page12、Doc3/chunk54保持，source/content hash、leaf IDs/向量/index revision未变。仅并列句meta保存结构审核；A/B在各次审核完成后运行，相同模式对照内没有修改数据。

## 7. 代码与证据

核心路径：
- `backend\app\rag\structure\builder.py`、`policy.yml`、`runtime.py`
- `backend\app\rag\retrieval\relation.py`
- `backend\app\rag\preview.py`、`indexer.py`、`retriever.py`、`provenance.py`
- `backend\app\services\knowledge_service.py`、`ocr_review.py`、`ocr_service.py`
- `backend\app\api\routes.py`、`ocr_routes.py`、`worker\ocr_index.py`
- `backend\app\workflow\graph.py`、`services\quality_service.py`
- `frontend\src\pages\StructurePreview.tsx`及知识库/资料/OCR工作台、API类型
- `backend\scripts\rag_eval.py`
- `backend\tests\test_rag_str12.py`、`tests\integration\test_rag_str12_postgres.py`

私有证据：`.local-eval\str12`，全文/截图/认证/环境备份不提交、不自动删除。

- `before-db.json`、`final-db-and-ledger.json`：不变语料/主业务、实际账目。
- `gold-freeze.json`、36+8 gold、四组最终报告/ab-summary与initial-ab：原结果和重测不覆盖唯一证据。
- `real-structure-preview.json`、`pilot-query.json`、`live-ui-report.json`、两张截图：结构和实际来源交互。
- `full-regression.log`、`coverage.json`、`full-app-gate.log`、`app-coverage.json`、`verification-summary.json`：测试/覆盖率与准确分母。

回退可在前端选legacy或API传context_mode=legacy；全运行回退根环境RAG_CONTEXT_MODE=legacy后重启API/worker。结构开关/策略变化会明确影响签名，旧审核不会被静默当新审核；无需重嵌入才可回退。

## 8. 遗留边界

- STR-3逻辑续表/跨页cell合并、2/3页窗口OCR尚未实现，本轮仅保护原物理表/行和有原生证据的正文/表注关联。
- STR-4复合跨章节/小节证据覆盖尚未完成，新8中1条失败保留。
- STR-5物理页软边界的leaf重切/新的embedding前缀与选择性重建尚未执行。当前直接复用原leaf，主要单元不确定时标记限制，不改旧向量含义。
- STR-6语义/LLM背景/Late Chunking实验未启用。
- 数据集只有3份有限英语资料；不得宣传全教材/所有复杂布局或无人值守事实质量已认证。性能、故障演练、GPU/分发许可及真实生成/judge/人工质量仍待。

| 收尾面 | 状态 |
|---|---|
| STR-1/2代码与测试 | changed-and-verified |
| 原部署/真实界面 | changed-and-verified |
| docs/tasks/OPT/规则 | changed-and-verified |
| 外部记忆 | out-of-scope，不写入 |
| 工作区 | 原未提交P0/P1/OCR变更和私有现场保留，未清场/提交 |
| STR-3～6/全库生产质量 | pending |
