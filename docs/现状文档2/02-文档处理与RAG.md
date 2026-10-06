# 专题02：文档处理与RAG

> **读者**：项目维护者；详细机制以当前源码为准。
>
> **核对日期与范围**：2026-10-06（Asia/Shanghai），当前实现基线 OPT-077。本次仅同步文档，依据现役源码、Docker状态、只读SQL及已保留的阶段验收；未重跑测试、部署、迁移、模型或人工审核。全局修复状态见[主文档](英语内容生产工作台-当前实现与系统架构.md)第1.4节，验收/历史边界见[现役修复与证据补充](历史与证据/03-现役修复验收与证据补充.md)。
>
> **总入口**：[英语内容生产工作台-当前实现与系统架构](英语内容生产工作台-当前实现与系统架构.md)。


## 1. 链路边界与当前策略

RAG解决的是“给生成提供可定位的资料证据”，不是自动保证教材正确、答案唯一或所有知识点都已索引。主链路当前为structure切分＋relation返回，hybrid召回、Top-5默认/Top-8可选、32768字符总预算；rerank关闭，rules规划和aliases扩展启用。

本文详解资料→文本→结构→leaf→向量→候选→上下文→引用。生成如何消费来源见[01-内容生成与质量闭环](01-内容生成与质量闭环.md)；表及版本见[03-数据模型与状态机](03-数据模型与状态机.md)；OCR调度和恢复见[04-任务执行与可靠性](04-任务执行与可靠性.md)。

## 2. 两个入库入口不能混为一谈

| 流程 | 适用 | 执行方式 | 人工/付费边界 |
|---|---|---|---|
| 原生preview/upload | txt/md/docx/html/csv/xlsx、可提取文字PDF | API调用解析/预览；普通索引同步执行 | preview免费；upload索引调用embedding，非OCR审批状态机 |
| OCR后台 | 扫描PDF/复杂版面、选页大文件 | OcrJob/OcrPage＋独立队列＋本地MinerU | 原页/告警/排除/布局预览后显式确认embedding与范围 |
| boundary局部复核 | 已完成范围内连续2/3页难边界 | 独立OcrBoundaryJob | 确认本地计算；结果不自动改原成功页/知识索引 |

普通文件10MiB；OCR文件128MiB、后台任务最多500页。`RAG_OCR_MAX_PAGES=3`对应小预览/同步路由边界，不是后台任务最多3页。OCR只选部分页时必须保留partial范围并显式接受，不能把选页成功写成整书完成。

入口：[knowledge_service.py](../../backend/app/services/knowledge_service.py)、[ocr_service.py](../../backend/app/services/ocr_service.py)、[ocr_review.py](../../backend/app/services/ocr_review.py)、[boundary_service.py](../../backend/app/services/boundary_service.py)。

## 3. 统一文档表示与格式差异

### 3.1 ParsedDocument

[document.py](../../backend/app/rag/document.py)定义：

| 部分 | 作用 |
|---|---|
| source_name/source_type/source_hash | 来源名称、类别和原始快照身份 |
| parser_version | 解析器实现版本 |
| blocks | heading/paragraph/table/table_row/list/code/image/page_break，带order/page/locator/meta |
| text | 按block以空行连接的规范化文本 |
| original_text | 适用路径保留的原文文本，不保证是原二进制文件内容 |
| warnings/stats | 丢失/降级/范围/统计，不是可忽略的装饰字段 |

`finalize_document`计算每块在规范化text的content_start/end；坐标是Python字符串字符偏移，不是PDF字节、页内OCR坐标或embedding tokenizer位置。

source_hash与normalized_content_hash用途不同：前者定位源快照，后者定位规范化文本。OCR审核排除构造审核文档后会重新finalize；必须使用该索引snapshot的坐标，不能套原PDF未排除全文的偏移。

### 3.2 各格式实际行为

| 格式 | 实现重点 | 典型限制 |
|---|---|---|
| txt/md | 文本/标题/列表/代码/GFM表格结构 | Markdown标题不一定代表教材真实章节 |
| docx | 段落和表格按body顺序处理 | 任意复杂图文布局不保证完整 |
| html | 抽取正文结构、规范化表格 | 不执行网页JS，不是浏览器完整页面渲染 |
| csv/xlsx | sheet/列说明/行级表单元，保存原行号 | XLSX公式保留文本未计算；合并空位不猜值 |
| 原生PDF | 页文字抽取、来源页和告警 | 多栏阅读顺序、扫描字、图表可能不可靠 |
| OCR PDF | MinerU结果桥接标题/段/表/cell/span/bbox | OCR值、标题级别、跨页语义仍需人工核验 |

[parser.py](../../backend/app/rag/parser.py)还有解压尺寸、block数、sheet网格等保护。支持某后缀不是承诺所有该格式文件都可以无损RAG。

## 4. OCR输出如何保持可恢复与可核验

`OcrStorage`保存source.pdf、页结果、预览和cache；所有文件键相对配置root、resolve后不得越界。JSON输入大小/hash验证，临时文件原子replace；cache身份包含tenant、源hash、页和版本，避免跨tenant复用。

每次页attempt与租约有token；旧attempt不能越过fencing提交新的活动结果。OCR本地推理失败不自动用云OCR兜底。页面成功可复用，整任务取消/续跑不要求所有已成功页重识别。

原页QA的bbox需对应render frame/尺寸；不匹配时不画“看起来准确”的框。框位置匹配不代表文字识别或事实正确。

多页复核只检查已完成范围内连续2/3页，不跨缺页。输出以独立snapshot和global page mapping保存；程序不直接用新窗口覆盖成功页，必须区分“得到新建议”与“已审核重建知识索引”。

源码：[backend/app/rag/ocr](../../backend/app/rag/ocr)、[storage.py](../../backend/app/rag/ocr/storage.py)、[ocr_runner.py](../../backend/app/worker/ocr_runner.py)、[boundary.py](../../backend/app/worker/boundary.py)。

## 5. 文档级结构：关系而不是自由改写

[backend/app/rag/structure](../../backend/app/rag/structure)和[policy.yml](../../backend/app/rag/structure/policy.yml)派生逻辑章节、单元、角色和关系。结构签名绑定policy/源/审核决定，不覆盖教材事实。

| 边界/关系 | 如何处理 |
|---|---|
| 重复页眉页脚 | 家具识别，不无条件重置章节 |
| 自然段/续页 | 有证据的accepted关系可连；未知邻接proposed |
| 新章节 | 保护屏障，不因同标题文字就跨章合并 |
| 缺页/不连续选页/审核排除 | 不从不存在的中间内容推测续接 |
| 独立题组/答案 | 防止把相邻独立问题答案混入规则 |
| 定义→例句/例外/表格 | 关系层恢复证据，不必全部放一个向量 |
| 明确章节引用 | 找唯一目标；不存在/多义/越scope不扩 |
| 续表/cell | 需表身份/布局/结构等多证据；未知值保留待审 |

[tables.py](../../backend/app/rag/structure/tables.py)处理逻辑表与cell候选；确认table续接不等于每个合并cell已确认。重复表头可在派生视图去重，原row/column/segment仍保留。相同列数或词义相近都不足以让两个表自动合并。

## 6. 切块、上下文前缀与source_segments

### 6.1 layout与strategy是两维

- layout=structure：默认，走[structural.py](../../backend/app/rag/chunking/structural.py)，基于文档关系生成leaf。
- layout=legacy：连续源跨度兼容，strategy=auto时按heading→heuristic→recursive→legacy降级。
- strategy名字不意味着启用了实验semantic/Late算法；那些在独立CLI。

`configured_chunking`从Settings取size512/overlap80等配置。结构/表格屏障优先，实际overlap、小块和长度分布不必等于机械固定512+80滑窗。纯导航标题/家具保留源，不纳入eligible正文embedding覆盖分母。

### 6.2 正文与embedding输入分开

leaf.content是可引用正文，context_header可含来源/章节路径，embedding_content=上下文前缀＋正文。不能把派生前缀当成原文事实。输入hash用于确定“向量实际表示哪段输入”；只看leaf body hash无法发现前缀变化。

parent是组织/回传单元，不embedding。[parents.py](../../backend/app/rag/chunking/parents.py)在结构布局保留片段来源并去掉child overlap，不能用parent所属身份声称所有child已交付。

### 6.3 非连续源段的坐标示例

一个leaf可由页2定义与页3例句组成，中间页眉被排除：

```text
leaf.body = 定义 + 拼接空白 + 例句
source_segments[0]: 源snapshot[100:140] → leaf[0:40]，page2 / blockA
source_segments[1]: 源snapshot[200:230] → leaf[42:72]，page3 / blockB
顶层leaf.content_start/end = None
source_envelope = 100..230，只作导航，不是正文范围
```

偏移为示意，不是本库真实数据。structure validator校验每个片段文本、hash、范围/覆盖/重复归属、未归属非空白内容和输入预算。重新finalize后偏移可能变，必须按snapshot/version解释。

### 6.4 表格会不会被切断

通常保护完整行/题组，拆大表时可重复表头保上下文；单行/单cell超预算仍可能拆并输出warning/partial标记。结构良好时减少被切断，不是“所有表格永远无损”。rowspan/colspan的空位不应脑补，续表/单元格未确定时仍不完整。

## 7. 索引落库与选择性重建

[indexer.py](../../backend/app/rag/indexer.py)先解析/切块/校验计划，嵌入leaf批次，检查返回数量/维度/finite（embedding模块还处理向量有效性），完成后再写同一批文档与块。batch_size为应用与provider上限的较小值。

| 保存对象 | 保存内容 |
|---|---|
| KnowledgeDocument | normalized snapshot、blocks、hash、warnings/stats、layout/index_revision/结构决定 |
| leaf | body、context、vector、模型/维度、input hash、source_segments、标签/邻接 |
| parent | 组织正文/映射，vector=NULL |

OCR批准须来源/告警/费用确认、partial范围确认、重建expected_revision与有效plan_hash；worker再次算计划，失效则拒绝。审批状态indexed且无重建请求可幂等返回，不因为双击再付费。

同ID重建增加revision、替换chunk IDs，完成源段校验/向量校验后原子切换；异常保留旧索引。旧向量不是一个完整可随时查询的多版本仓库，恢复旧布局需显式重建。

免费结构审核可以改变派生关系；若结构化leaf已经固定，旧embedding输入/片段不能仅靠审核就“自动变新”。关注stale/hash门控，必要时新计划重建。删块/撤除后的关系扩展不能通过Document.blocks补回被删除内容。

## 8. 知识点召回与授权范围

[knowledge_points.yml](../../backend/app/rag/knowledge_points.yml)定义稳定ID、canonical name、aliases、parent/related。labels会规范化，查询值先解析；未知标签仍需看scope/diagnostics，不能假定taxonomy自动知道所有细分语法。

| scope mode | 含义 |
|---|---|
| exact | 指定知识点精确范围，当前默认 |
| ancestor | 可包含祖先标签的范围 |
| descendant | 可包含子孙标签的范围 |
| related | 已声明关联知识点 |
| semantic | 不按该知识点标签过滤，保留tenant/document等授权 |

filter还包括tenant、document IDs、来源名与章节路径；空document_ids是空范围，不是全库。admin业务查询可跨tenant，但内部RAG生成仍受调用传入的scope。query扩展只改变搜索表达式，不允许改tenant/doc范围。

## 9. 向量/关键词召回与融合

[retriever.py](../../backend/app/rag/retriever.py)先保留原query，再加入aliases和rules互补子问题；总query/part数量有上限，每个query两lane。一次复合查询可能多次embedding输入，不能按HTTP请求数估成本。

- [vector.py](../../backend/app/rag/retrieval/vector.py)：PG余弦候选，leaf predicate排parent，scope过滤；默认最低相似度0.3。SQLite仅参考路径，不是真ANN。
- [keyword.py](../../backend/app/rag/retrieval/keyword.py)：English FTS＋literal与有界中文bigram；不是BM25/完整中文分词器。文字匹配过多会让通用题解排到规则前。
- [fusion.py](../../backend/app/rag/retrieval/fusion.py)：按稳定chunk ID去重，每lane最多一次贡献；RRF=Σ1/(60+rank)。忽略余弦幅度差异，因此vector1不保证fusion1。
- [rerank.py](../../backend/app/rag/retrieval/rerank.py)：off/optional/required适配接口。现在off，没有真实rerank收益；required失败与optional降级语义不同。
- [planning.py](../../backend/app/rag/retrieval/planning.py)：互补问题seed覆盖分配，不是证明两个概念已回答；语义冲突资料应独立返回，不能自动统一成一个事实。

SQL有HNSW/FTS/trigram等索引。强制planner测试只证明索引可用，不代表默认执行计划/延迟/真实语义召回质量已认证。候选pool30按lane，不是最终5包。

## 10. relation返回与provenance

[relation.py](../../backend/app/rag/retrieval/relation.py)：从活动同scope leaf映射建立结构状态，选择seed逻辑单元，沿accepted关系恢复关联段。source签名/审核过期关闭扩展，保留精确seed或明确降级；不读整个normalized_text包络回注资料。

返回：snippets（渲染文本）、bundle_citations/context_bundles、实际source_segments和diagnostics。body、source locator、page/block、原行列/hash/snapshot版本分别保留；bundle.complete通常是结构材料送达范围，不是回答事实正确。

Top5/8约束包数，成员/跳数/segment/字符/可选字节保护同时生效。长单元可能耗尽预算，整个响应会少于Top-K，须看incomplete_reasons/truncated/partial_row，而不是硬补足8个ID。

[provenance.py](../../backend/app/rag/provenance.py)把实际交付segments转成内容引用；候选summary里diagnostic_candidate=true、source_block_ids/pages只供定位，不能计入已给模型的证据。生成required检查有效片段，不对用户承诺每个来源都自动正确。

## 11. 评测、诊断与故障定位

| 症状 | 定位层 | 要读的证据 |
|---|---|---|
| 资料文字本身不正确/丢表 | 解析/OCR | 原PDF、原页、blocks、warnings |
| 分段把源合错 | structure/chunker | accepted/proposed、屏障、segments/cell来源 |
| 正确leaf根本无向量/范围排除 | 索引/scope | active revision、model/dim、predicate |
| 向量lane有但fusion不进返回 | hybrid排序/预算 | 各rank、融合、seed/Top-K |
| parent/page命中却无例句 | 上下文组织 | 实际segments及incomplete理由 |
| 旧块删除后仍被带回 | source安全 | active leaf source_scope/signature，不能看Document全源猜 |

[rag_eval.py](../../backend/scripts/rag_eval.py) v7支持原gold不变、显式`--top-k`、effective_top_k和scope/语料指纹。指纹包含实际向量/输入hash/meta/search/邻接等；改向量或structure meta也会变，不是只看原文没改就声称同向量A/B。

指标分开：pageHit/MRR、精确源单位、required terms、表格同一行tuple、logical row、context_complete、失败阶段。它们不是模型答案正确率。原44gold是有限回归，不能当新教材四类专家集。

本机三资料全部structure，81leaf+1parent；名词仅页2/16且block:15排除，动词仅页1，并列句8页。Top5/8为44/44来源齐备；曾经RRF4漏出Top3通过扩大返回预算补回，融合算法未由此变好。

## 12. 实验方法与维护变更

[backend/app/rag/experiments](../../backend/app/rag/experiments)/[rag_experiment.py](../../backend/scripts/rag_experiment.py)提供structure/semantic/contextual/late独立实验，不改活动索引。

- semantic只对无歧义单prose块作相邻句向量边界，保护结构优先；小实验真实embedding无已测改善。
- contextual确定性前缀与源分离；选配LLM仅抽取源中真实片段、严格Schema/Trace；真实LLM效果待验。
- Late须真实token hidden states先全文forward，再按源段pool。显式本地safetensors/fast offsets、禁remote code/下载/截断；真实模型forward未验收，云最终向量不可冒充。

维护清单：

1. 修改解析器先固定原源和页/block映射，别直接覆盖用户资料；
2. 修改结构先测新章/缺页/排除/独立答案/同列独立表负例；
3. 改切块或embedding输入就新计划hash与显式重建，保旧失败路径；
4. 改检索先固定gold/scope/预算，分召回与交付指标；
5. 改引用检查真实片段、删除/stale和表格同行，不只验证claimed IDs；
6. 同步API/worker/env/preview版本，不允许各进程不同默认；
7. 保留legacy明确回退，但不要自动用legacy绕过结构错误且仍宣称完整。

测试入口[backend/tests](../../backend/tests)中的RAG P0/P1、STR1/2、STR3/4、STR5/6、primary chain；[backend/tests/integration](../../backend/tests/integration)验证迁移/索引可用、事务保旧、scope。OCR benchmark的anchor CER只是抽样锚点指标，不是整页/整书正确率。
