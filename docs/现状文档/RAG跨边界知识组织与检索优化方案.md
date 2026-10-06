# RAG 跨边界知识组织与检索优化方案

> **现役进展：2026-10-06，OPT-073**。structure/relation已成为主RAG链路；Top-5默认/Top-8可选、32768字符；名词/动词选择性迁移完成，三内置题型required来源+人工核验，6条真实生成/质检完成但均待人工，发现完形质量风险不自动发布。44条source gold迁移后Top5/8齐备，来源支持不等于答案质量。现役验收：`docs\现状文档\RAG-主链路切换与真实生成验收.md`。

> **历史进展：2026-10-06，OPT-072**。STR-1～5工程与STR-6实验代码照旧；用户授权提高默认返回到Top-5/可选Top-8，应用上下文预算32768字符。候选池30、融合与scope未改；同源/向量统一top3仍43/44，最终32K下top5/top8均44/44，原排序缺口由预算补回而非算法修复。895回归/42真PG、app82.31%，原环境已部署；默认layout仍legacy，不自动全库重嵌入，Late/LLM真实效果及事实质量仍待。现役证据见 `docs\现状文档\RAG-Top5-Top8与上下文预算验收.md`。

> **历史进展：2026-10-06，OPT-071**。STR-1～5工程落地；STR-5结构化leaf/parent与逐段真实来源、原生/OCR显式layout及选择性原子重建已部署，并列句同ID v2→v3/54leaf+1parent/2跨页leaf，其他资料未重嵌入。主库3资料/70非零向量+1parent，head仍rag_str34_boundary。STR-6独立实验入口/语义/确定性及选配LLM上下文/真实token-pool本地适配器代码完成；真实structure/semantic/确定性contextual自编prose均3/3来源支持，未测得收益；Late真实权重forward/LLM真实chat未运行。871回归/39真PG、app82.29%；18源文件scoped strict非全app认证。冻结44题重建前44→重建后43齐备，正确向量rank1/RRF4未进top3，已知回归保留。默认layout保持legacy，structure仅显式选项，不自动全库重建；103真实embedding/5532tokens，费用仅未核验fallback估算。完整证据见 `docs\现状文档\RAG-STR5-STR6落地与验收.md`。

> **历史阅读约定**：下方OPT-069/070日期、52向量、44齐备、STR-5/6尚待等为各阶段历史，不代表OPT-071运行态。STR-0正文为原设计，不是新一次调研/未实施结论；每类至少10条的新四类金标仍未达成。

> **STR-3/4已部署：OPT-070（记录2026-10-05 UTC；本机日志2026-10-06）**：逻辑续表多证据/未知待审、逐cell确认与原row/column映射、重复表头派生去重、局部2/3页后台复核（独立snapshot，不改成功页/索引）及跨章明确引用/原query+有界互补问题/多seed公平预算已实现。真实MinerU4.0.10原页2/3、2/3/4完成；相同36+8gold planning off/rules：36仍齐备、8由7→8齐备，无真实chat/judge/rerank或语料重嵌入。811回归/36真PG，app81.63%门槛通过，14模块scoped mypy；head为rag_str34_boundary，主/CI迁移前已备份。原Doc3/52向量+2parent、业务/OCR成功页不变，新增2个复核job；结构审核v2显式更新。178真实embedding/2140报告tokens，金额仅未核验配置估算。真实窗口不含表格，跨页table/cell规则用fixture/真PG验证，不冒充全教材OCR准确率；STR-5/6、专家金标/生产事实质量仍待。详见 `docs\现状文档\RAG-STR3-STR4落地与验收.md`。下方STR-3/4待实施及head旧值为历史阶段。

> **STR-1/2已部署：OPT-069（2026-10-05 UTC）**：文档级结构/重复页眉及连续小节、缺页/排除/题组/新章屏障、免费结构预览审核及hash/版本门控已实现；关系感知small-to-big仅从活动同scope leaf返回逐段真实引用，v2 bundle/segments协议已接API/前端/生成provenance/人工审核和v4评测，legacy可回退。相同原文/leaf向量36条回归35→36齐备，新8条6→7齐备（跨小节复合问题仍1条未完整，属STR-4）。751回归/32真PG通过，全app80.57%越过80%门槛，13模块scoped mypy不等于全app。运行relation/aliases/rerankoff，未重嵌入或新OCR；Doc3/52向量+2parent、原业务及head不变，只有并列句结构审核meta更新。181真实embedding/2037报告tokens，费用仅未核验配置估算，无chat/judge/rerank。详见 `docs\现状文档\RAG-STR1-STR2落地与验收.md`。STR-3～6及批量事实质量仍待；下方STR-0“未实现”和OPT-068“79.51%未达门槛”为历史。

**日期**：2026-10-05
**决策编号**：STR-0（调研与方案，不是代码发布）
**覆盖范围**：跨页、跨段落、跨表格、跨章节及组合问题
**设计基线**：OPT-068；STR-0仅调研。
**当前实施**：OPT-069完成STR-1/2、OPT-070完成STR-3/4、OPT-071完成STR-5部署/选择性重建和STR-6独立实验代码；Late/LLM真实效果仍待；OPT-072默认Top-5/8已补回标注来源，Top-3排序限制未消失。正文保留STR-0原设计/验收边界，以下“本轮”指STR-0调研阶段。

## 0. 结论与本轮边界

推荐主路线：

> 版面/结构识别 → 文档级结构重建 → 有来源证据的知识单元/关系 → 小块召回 → 按关系恢复完整上下文 → 多章节问题收集多个独立证据 → 逐片段引用。

**物理页是定位与断点处理单位，不是默认知识单元。** 同一知识单元可以跨多页；同一页也可含几个不同知识单元。章节路径不是可靠关系的唯一来源，尤其 OCR 的重复页眉/标题等级有误时。

采用组合方案，不选择一个“万能 splitter”：

- 主干：结构感知 + 小块检索/大块返回 + 证据关系扩展。
- 辅助：局部句段窗口、边界处多页结构复核、确定性上下文前缀。
- 选配实验：结构不可靠时的语义切分、LLM 上下文说明/查询分解/跨页单元格判定。
- 延后：Late Chunking、RAPTOR/全局摘要树、完整 GraphRAG。不是没有价值，而是当前接口/事实核验/维护成本不适合先做。

本轮完成官方文档/原论文/本地源码对照和原知识库只读拓扑审计；**没有实现新结构层，没有开启新OCR/LLM功能、调用embedding、修改主库、下载模型或重启服务**。684测试/28集成与79.51%全app覆盖率是OPT-068的上轮证据，不是本轮重新运行结果。下一实施从STR-1开始。

## 1. 当前项目确实把哪些边界卡死了

### 1.1 四处限制

| 位置 | 当前行为 | 影响 |
|---|---|---|
| chunking/strategy.py `_run_tier` | `different_page` 会 flush；空page_break也flush；标题变更flush | 同知识点的自然段/句子跨页后分开；overlap不会自动越过这个边界 |
| chunking/parents.py `parent_plans` | parent兼容键含 page_no、section_path、table_id | parent不能跨页；正文→表格也不容易属于同一个parent |
| retrieval/context.py `_compatible` | 要求同document/tenant/page/section/知识点标签 | 已有prev/next关系也无法跨页，错章节路径会继续挡住邻接 |
| ocr/mineru.py `NativeDocument.pages` | 原客户端每次只接受一页结果 | 引擎没有同时看到相邻页，不能假定它的跨页结构算法已被调用 |

现有能力不是“完全没有结构”，已经有block/table/grid/span/bbox、heading path、上下文前缀、父子/邻接、来源hash、审核排除；缺的是**页之上的连续结构与关系**，不是重写整个RAG。

### 1.2 真实失败比“第11位排序”更深一层

只读主库核对原始块和索引链：

- 页2规则chunk `c1b12ac9-461d-4cab-95e5-fd198224f30b`：section_path为 `[第二章, (二)表选择、条件关系]`。
- 页3续例chunk `4b5f902b-68f3-4c4e-8097-892cf592017b`：section_path变成 `[第二章|并列句]`。
- 二者互为直接相邻chunk，链不是断的；但page和section不等，被旧扩展规则拒绝。
- 页3 `block:30` 的原生类型是 `header`，带 `chapter_header_candidate=true`，文本“第二章|并列句”被提升为一级heading。
- 页1原始章标题又被拆成“第二章”“并列句”；assemble按完整章标题文本去重不足以识别这种别名/拆分，重复章页眉重置了当前小节。

因此“改大top_k”只是可能碰到续例，不解决结构错误。**修复页眉/章节继承后，页2规则命中应能按连续知识单元主动带上页3续例，不必等页3独立排进top5。** 此为设计目标，本轮没有跑新策略来证明它已实现。

私有只读证据：`.local-eval\ocr-2026-10-05\structure-design\audit-summary.json`、`readonly-corpus.json`。3资料/52leaf/2parent未变；本轮数据库写入0、embedding调用0。

## 2. 一手资料对比与选型

以下来源访问日期均为2026-10-05。在线文档/main分支与本机已安装版本不是同一个东西；实施前固定版本/源码摘要并核验协议、许可证，不因在线新增功能自动升级原环境。

| 机制/工具 | 查到的实际能力 | 对本项目的取舍 |
|---|---|---|
| Docling DoclingDocument [S1] | 主体/页眉页脚分离，文档层级、阅读序、表格/图、bbox与provenance | 借鉴中间表示；别把它导出Markdown后把结构丢光 |
| Docling HybridChunker [S2] | 在层级块之上按token预算拆/合，同标题与caption的相邻小块可合并；表格支持重复表头 | 移植“先结构后预算”策略，不以某个模型token数直接代替本项目字符/UTF8预算 |
| Unstructured by_title [S3] | 可保持章节边界；`multipage_sections=True`不因翻页开启新块；orig_elements保留位置 | 证明页边界可以是软边界；不声称它自带可靠跨页续表识别 |
| LlamaIndex sentence window [S4] | 检索小sentence，再以metadata窗口替换供阅读内容 | 适合作局部段落扩展，不能无条件越过新章、答案组或资料空洞 |
| LlamaIndex auto merging [S5] | 小节点召回后依据层级覆盖合并到parent | 借鉴small-to-big与聚合，parent应按知识结构建，而非整页/整书 |
| Anthropic Contextual Retrieval [S6] | 在块前添加文档相关背景，再用于embedding/关键词检索 | 先做可确定的章/节/表题/角色前缀；LLM说明另立派生层，不能伪造原文 |
| 语义切分对照研究 [S7] | 其评测未发现语义切分相对简单方案具有一致的成本收益 | 作为A/B备选，不因为名字“语义”就把它设成最佳默认 |
| Late Chunking原论文 [S8] | 长文本token先经embedding transformer，然后按块pool | 原项目客户端只消费最终文本向量；不能把拼两页送现API宣传成late chunking |
| RAPTOR原论文 [S9] | embedding/聚类/递归摘要形成不同抽象层级树 | 跨章节综述可考虑，但摘要是派生导航，不是事实来源；不优先增加摘要生成费 |
| MinerU源码 [S10/S11] | 有跨页续表对及可选LLM边界单元格增强；增强开关独立于一般解析 | 沿用当前parser，先让关系层看到两页；不可把cloud LLM后处理当成免费本地OCR |
| PaddleOCR-VL/PaddleX [S12] | 多页结果可做`restructure_pages`，含续表与多级标题重建选项 | 可作跨页难例离线对照；**不是前轮PP-Structure CPU基准的同一能力/模型**，推理环境/资源成本另验 |
| PyMuPDF4LLM [S13] | 面向PDF的阅读序/Markdown、表格与布局提取工具链 | 文字型PDF可做轻量候选，也支持按需OCR及布局JSON；扫描教材需要OCR路径，先核对许可/引擎配置和输出位置，非马上替换MinerU |

对标本地WeKnora：已读 `internal/infrastructure/chunker/strategy.go`、`heading_splitter.go` 与parent-child相关入口；其自适应层级/递归/表格保护是可借鉴构件。不能据这些构件推断跨页文档语义全部自动正确；本项目也不应复制其完整服务/agent层。

## 3. 先区分三个“窗口”

### 3.1 OCR/结构分析窗口

作用：让引擎或跨页后处理同时见到页尾与下一页页首，识别句/表/标题续接。

候选实验：连续3页、步长2，或只对疑似边界取 `[p-1,p,p+1]`（3页不是已验证最优参数）。两页已足以验证大部分相邻续接；更大的窗口仅用于长表/复杂布局对照。

保留原逐页任务/checkpoint是合理的，不必为了知识跨页放弃断点续跑。新增独立**boundary reconciliation**阶段，读取已完成页的结构；只有证据不足时才显式请求小范围多页重识别。

全量3页步长2会有重算和去重成本，不默认对所有页跑。必须以全局物理page/block身份去重；两窗同一页输出不一致时保留差异和审核标记，不以拼接顺序掩盖。

当前单页V1 adapter不可直接调用多页：需要新请求/校验契约、global/local page映射、输入/输出上限和缓存键，不能只把`max_length=1`改大就上线。

### 3.2 索引窗口/overlap

作用：在同一知识单元过长时，切成有重叠的small chunks，使相邻句边界可检索。

- 优先保留完整句/段落/例句/表行；超预算才拆。
- 页可以跨；真实新章、不同题组、表格逻辑行边界不能被overlap随便穿过。
- 规则和例句可以属于同一个逻辑单元，但不一定是同一个原始段落。
- 不重复把整页放进多个vector输入；避免标题/公共词主导向量以及成本膨胀。

### 3.3 检索返回窗口

作用：命中小块后恢复必要的规则、例句、续表、表注、题干/解析。

这是本项目最该先做的层：可在不重嵌入所有leaf的情况下，按已存在源结构与关系增加上下文。

- 不是固定“前后一页全返回”；按知识单元/关系返回必要成员。
- 有严格字符/字节、member/citation和遍历上限。
- 关系无证据/源不完整时，只返回已知片段并标`incomplete`，不补写缺失内容。

## 4. 总体结构：物理来源与逻辑组织分层

```text
原文件 + 原生识别JSON（不覆盖）
  ↓
Page/Block/TableCell/SourceSegment（保留原阅读序/bbox/offset）
  ↓
文档级章节树 + 续接/定义例句/表注/题组关系（可审核、版本化）
  ↓
small leaf chunks（用于vector/keyword）
  ↓
relation-aware context bundles（用于生成，可跨页/跨段/含表）
  ↓
带各source segment引用的结构化上下文
```

### 4.1 数据表示建议（设计字段，不是当前已存在列）

```text
SourceSegment:
  document_id + source_hash + source_snapshot_version
  page_no / block_id / bbox / normalized_text_start,end
  original_table_id / source_row,column / content_hash

StructureNode:
  id / kind / parent_id / child_ids
  logical_section_id / logical_unit_id / logical_table_id
  members: SourceSegment refs（可不连续，顺序明确）
  source_role: rule/example/caption/table/footnote/question/answer

StructureEdge:
  from / to / relation_type
  evidence: layout/title/caption/native_relation/explicit_reference/reviewer
  state: proposed/accepted/rejected/uncertain
  structure_version / source_scope_hash / reviewer(optional)
```

- `paragraph_continues`与`same_knowledge_unit`不是一回事：分页断句可有原文连续语义；完整规则和下一段例句是关联片段，不能自动造出一个“原段落”。
- `table_continues`与`definition_to_table`也不同：同表续页与正文接说明表分别处理。
- `related_concept`用于跨章导航，不允许它把两段原文直接粘成一个事实。
- 简单层级可复用现有parent/child；一个child需要章节parent、知识单元group、续表group等多重归属时，不滥用只有一个`parent_chunk_id`的列。
- 第一阶段关系以版本化派生快照/小规模索引的元信息实现；规模检索需要时再迁移独立关系/成员表。不是先引入图数据库。

### 4.2 严禁丢失精确引用

跨页不能简单`page_no=None`后丢来源，也不能把跨页parent整段挂在第一页。

采用有序`source_segments[]`：每段真实页/块/表行/hash与返回offset分别记录。多段融合后可以额外显示`pages=[2,3]`，但这是集合信息，不替代每段来源。

若返回是非连续拼接，不许伪造单一`content_start/end`覆盖原文中未返回的中间段。源段分开保存，派生上下文前缀也与原文hash分开。

## 5. 四类跨边界处理规则

### 5.1 跨页：延续知识而非延续物理页

流程：

1. 原生`header/footer/page_number`作为版面家具保留，不直接作为正文section。
2. 重复页眉候选结合真实章编号、已知章标题别名/拆分、跨页位置与正文标题识别。数字相同或字符串近似本身不够；新章正文标题仍是强边界。
3. 章层级稳定后，让小节上下文跨连续页继承。对“链接中考/温馨提示/知识拓展”这类模块标签用角色关系，不在所有教材里硬编码它们一定是同级章节。
4. 检查页尾→页首：原引擎续接信息、句/列表是否未结束、行/版面阅读序、同小节/题号等。
5. 高证据关联accepted，不明者proposed并告警；缺页/选页不连续/被排除源块构成硬屏障，不能因为文字很像就跨过去。
6. 命中规则leaf→补其accepted续例，停止于新知识单元/新章/预算。

真实验收锚点：页2“表选择、条件关系”命中后，能带页3的“Hurry up, or...”续例；保留两个页的引用与源hash，不能悄悄恢复错误header。

### 5.2 跨段落：规则、例句、例外说明组成一个单元

- 标题/列表/题号/语法术语决定逻辑组；遇到句号不代表知识结束。
- 规则→例句→注意/例外可作为知识单元返回，原段落身份保持。
- 过长单元按完整句/例句小块embedding，再由unit成员关系恢复。
- 无标题/结构质量低的长prose再考虑邻句embedding相似度转折；语义断点只是候选，必须服从代码/表格/题组/明确标题等边界。
- 不以语义相似自动合并相邻题目的两个答案；相似词不等于同一个事实。

### 5.3 跨表格：分别处理正文接表、续表和跨页单元格

**A. 正文→表格→表注**：使用caption、原关系/邻接和“如下表”等证据，形成一个context bundle。leaf仍按正文/表行索引，不要求共用table_id才能关联。

**B. 同一表跨页**：组合证据包括连续物理页、页尾/页首位置、相同列数及几何列对齐、重复/兼容表头、相同表题/编号或原引擎continues信息。相同列数与同名表头单独不足以自动合并。

保存逻辑`logical_table_id`，每个physical table segment仍保留original_table_id、grid/spans、原始row与bbox。渲染去重重复表头，但源快照不删除；展示行是哪些source row组成必须可查。

**C. 单个cell被翻页切断**：先确认逻辑续表，再逐cell判定；不能“上一表最后一行 + 下一表第一行全部拼起来”。有colspan/rowspan时按实际逻辑列范围对齐，不按扁平下标草率拼。

确定性证据不足时保留两个片段、标未确认。MinerU可选LLM单元格增强只是另一个proposal来源，仍需代码结构校验、Trace、费用门控与审核，不默认启动。

**D. 两张独立对照表**：即使列相同也保留两表，跨表查询分别引用；不把两份互相冲突的答案合成一张“已认证”表。

**E. 超预算**：按完整表行分，补caption/表头；单行超预算分段必须明示`partial_row`，不能标完整支持。当预算容不下“表头+必要行”时不给无说明残行冒充完整答案。

### 5.4 跨章节：通常需要多证据，而不是一个超大parent

区分三种：

- 明确交叉引用（如“该规则参见第三章”）：保存`references_section`，按可解析目标补原证据。
- 同知识点跨章分散（定义、用法、例外）：canonical概念只能作为导航；章节/教材语境和别名可多义，不能仅标签相同就把所有内容融合。
- 问题本身比较多个主题（并列句结构 + 动词谓语作用）：保留原query，做有界子问题/多lane证据召回，按问题覆盖选bundle；跨章源分别保留出处，不串成原文。

第一阶段用明确引用与现有配置知识点导航；LLM子问题分解是后续选配并用Pydantic/原query保留/scope不扩大来约束。聚类/章节摘要可用于导航，但最终回到leaf原文核对。摘要不当教研事实金标。

## 6. 检索与上下文拼装

### 6.1 从“top_k个正文块”转为“top_k个种子 + 有界关联内容”

设计伪流程：

```text
查询和用户scope
  → 原hybrid/RRF/rerank候选
  → 多个独立知识单元seed（避免同一单元挤满槽位）
  → 每seed沿accepted结构边收集必要成员
  → 新章/缺页/排除/删除/版本漂移时停止
  → 按budget分配context bundle（seed必须优先保留）
  → 去重源段，不重复计数；不同来源冲突不去重成同一事实
  → 每个实际返回source segment的citations
```

初期优先保留原候选排序，验证单纯结构扩展收益；不能同时改rerank/阈值/chunk大小/全部embedding，失去因果对照。

现有top_k语义与API兼容需要明确：保留旧`snippets/citations`路径，新增versioned bundle结果；**不要悄悄把top_k从“返回数”变成“seed数”而下游照旧裁citations[:top_k]**。生成/评测/前端/provenance必须同步支持seed_count、bundle_count、segment_count和预算。

小块检索+大块返回不是默认整章。一个很长知识单元可有多个bounded bundles；章级group负责导航，并非强行把整章送模型。

### 6.2 结构化上下文注入（示意，不是已部署格式）

```text
知识单元：表选择/条件关系
章节：并列句 > 并列连词分类 > 表选择/条件
完整性：规则及已确认续例；其余题组未要求展开

[来源A：document、page2、block、hash]
规则原文……

[来源B：同document、page3、block、hash]
续例原文……

关系：A --same_knowledge_unit/continues_examples--> B
```

上下文前缀可由标题/caption/结构角色直接得到，未见原文的主语/定义不得凭摘要补写。原文、派生前缀、relation/evidence应分离记录，不让“这是某规则的例外”等未经验证推断混成原文。

引用仍是unverified事实状态；结构确认不是事实认证。源资料内出现的命令只作资料文字，不得控制权限/网络/代码执行。

### 6.3 删除、审核排除与租户仍是硬约束

- 从**当前有效且同scope的KnowledgeChunk**展开内容；不能绕开删除/API审核，从KnowledgeDocument.normalized_text把已排除或已删除内容恢复出来。
- 结构可以读源块做分类，但body materialization以活动索引成员为准；stale文档拒用旧membership。
- 删除/撤除/rebuild需使对应bundle/edge成员失效或重算，structure signature包含index revision/source scope hash。重建失败旧结构与旧知识仍是一致的一版。
- 明确section过滤时，不跨到不在过滤范围的章节；admin既有跨租户权限与普通researcher不同，不为新结构扩大权限。
- 关系环、跳过缺页、bbox缺失、页面旋转导致错误几何都要有负例；bbox未知不伪造“靠近页底/页顶”的事实。

## 7. 推荐实施顺序与验收

### STR-0：调研与决策（本轮完成）

完成一手资料、现有四个卡点和真实页眉/邻接审计；没有新策略上线或新的检索收益数字。

### STR-1：文档级结构重建与来源分段

- 免费本地纯函数，以审核过滤后的blocks为输入。
- 正文/重复页眉判定，章/节层级稳定与页间继承；缺页/排除屏障。
- source_segments + logical section/unit + accepted/proposed关系计划；保留原块/hash/offset。
- 先运行显式preview/dry-run，原活动索引不自动重建。
- **验收**：真实页2/3正确归属并提议续例；真实新章不粘；只选2/16页不可连；原row/grid/hash不变；无模型调用。用更改标题/缺页/新章的反例，不能只按唯一教材写硬编码修复。

涉及：`rag/ocr/assemble.py`、`rag/ocr/adapter.py`、新的`rag/structure/`纯函数、preview/审核plan签名；无需先换parser。

### STR-2：关系感知small-to-big与分段引用

- 从活动leaf扩accepted关系，有界多段bundle；不把同页/同table作为全局兼容键。
- 分开seed/expansion预算，原排序A/B先保持；legacy路径可回退。
- source segments对应实际返回文本，生成/RAG provenance/trace/API/UI同步；部分支持显式报告。
- **验收**：原跨页漏例问题所需页2+页3齐备；跨段规则+例外完整；删除/排除/tenant/section禁止内容不被回注；低预算不破坏关键表行。
- 先同36条gold/同源指纹评测，另有未参与调参的跨边界集；不能通过新bundle虚报全部parent子节点已经被送给模型。

### STR-3：表格逻辑组与跨页复核

- 正文-caption-table-footnote关系，续表多证据、logical table ID、重复表头去重、cell续接proposed与审核。
- 高难边界按2/3页窗口对照；保留逐页checkpoint，多页结果独立版本/缓存。
- **验收**：连续表、跨页cell、合并cell、不带表头续页、相同列的独立表、新章新表、删除一个table segment。无证据不自动merge，语料源错误排除不丢。

### STR-4：跨章节/复合问题证据覆盖

- explicit reference/概念导航与多种子覆盖；来源冲突分开。
- 如采用有界模型子问题，独立prompt/schema/Trace/费用选配，保留原query和scope。
- **验收**：两个不同章节的定义/例外、两个知识点比较、引用不存在、同术语多义、互相矛盾资料；需要多来源时只命中一章不得计完整。

### STR-5：索引结构切分升级与选择性重建

**实施状态（OPT-071）**：工程与同ID v2→v3真实重建完成；source_segments非连续映射/保护边界/预算/失败保旧通过。默认legacy不变；OPT-071的44→43为旧预算历史，OPT-072最终Top-5/8均44/44来源支持，仍不代表全库事实质量或默认structure推广，详见最新预算验收。

- 在完整逻辑结构上生成leaf，物理页变软边界；保护表行/题组/代码，结构过长再按预算拆，更新chunker/structure版本。
- corrected breadcrumbs进embedding前缀后需要新的计划hash与显式付费重建；旧索引不混新向量模型/维度。
- 第1/2阶段上下文收益可不重嵌入；不要为了更改引用字段就重复嵌入所有教材。
- **验收**：与已有稳定ID/v2→下一版原子切换兼容，失败保旧、scope/删块一致；旧行为可回退；完整生产入口前80%全app门禁仍必须过。

### STR-6：语义/LLM上下文/Late Chunking独立实验

**实施状态（OPT-071）**：独立CLI/语义/确定性与选配抽取式LLM上下文/token后pool本地适配器已实现。structure/semantic/确定性contextual真实小样本3/3均无收益；Late仅dry-run与适配器mock验证，真实模型/LLM效果仍待，不接生产默认。

- 仅对无结构prose做结构优先的语义split A/B。
- 可选LLM背景不覆盖源，成本/模型变化/派生hash单列。
- Late Chunking先验证token-level/分段pool接口或换专用模型的可行性，不能宣称现有OpenAI兼容客户端支持。
- 每种方法单独固定gold、指纹/费用/延迟评测，失败不给结构正文强行找边界。

## 8. 评测矩阵，不再只问“页命中了吗”

拟建40条以上冻结跨边界集：每类至少10条，包含组合/边界及反例；这是验收设计，不是已建成的数据集。

| 类别 | 正例 | 必须负例 |
|---|---|---|
| 跨页 | 断句、规则续例、重复页眉、3页知识单元 | 真新章、缺页、不连续选页、排除块、新题目、真实同名标题 |
| 跨段落 | 定义+例子+例外、题干+选项+解析 | 两道相邻题的答案、不同术语、旁注/脚注不属本规则 |
| 跨表格 | 正文+表+表注、续表、跨页cell、rowspan/colspan | 同列独立表、表头不同、表名多义、疑似源错误、删掉续页 |
| 跨章节 | 交叉引用、两概念比较、定义/例外分章 | 错目标引用、同名异义、冲突事实、scope排除目标 |

核心指标：

- 所需source segments召回与**context_complete**，而非pageHit或parent membership。
- 表格同行/列映射完整性与logical row provenance。
- FalseJoinRate / FalseExpansionRate：错误续接、误扩新章、无关答案污染。
- ProvenanceAccuracy：返回正文的页/块/行/hash是否可重放。
- DuplicationRate、budget/partial-row/incomplete报告准确率。
- 扩展前后p50/p95、实际字节/字符、模型tokens/Trace成本与处理时长，规模压力另测。
- 访问/排除/删除绕过应为0；该安全门槛不由总体召回高抵消。

现有v3评测若只以`matched_child_ids`或单page_no计parent，需要升级为检查**已实际返回的segments**；原36gold不改历史结果，但做新格式兼容评分，计数别被citations[:top_k]截掉。

首个真实A/B冻结：同源/同leaf向量，legacy_context对relation_context；显式框定seed数和context预算，测内容是否带齐、是否误扩。后续多页重OCR/重embedding改变源或输入时，分开报告，不声称同一语料的单因果对照。

## 9. 已核验一手参考资料

[S1] Docling — DoclingDocument：
https://docling-project.github.io/docling/concepts/docling_document/

[S2] Docling — Chunking / HybridChunker：
https://docling-project.github.io/docling/concepts/chunking/

[S3] Unstructured — Chunking / by_title / multipage_sections / orig_elements：
https://docs.unstructured.io/open-source/core-functionality/chunking

[S4] LlamaIndex — Metadata Replacement + Node Sentence Window：
https://developers.llamaindex.ai/python/examples/node_postprocessor/metadatareplacementdemo/

[S5] LlamaIndex — Auto Merging Retriever：
https://developers.llamaindex.ai/python/framework/integrations/retrievers/auto_merging_retriever/

[S6] Anthropic — Contextual Retrieval（2024-09-19）：
https://www.anthropic.com/engineering/contextual-retrieval

[S7] Qu / Tu / Bao — Is Semantic Chunking Worth the Computational Cost?（2024-10-16）：
https://arxiv.org/abs/2410.13070

[S8] Günther et al. — Late Chunking（初稿2024-09-07，v3 2025-07-07）：
https://arxiv.org/abs/2409.04701

[S9] Sarthi et al. — RAPTOR（2024-01-31）：
https://arxiv.org/abs/2401.18059

[S10] MinerU — 当前main分支跨页cell后处理源码（在线master与本机实测已安装4.0.10需分开核验）：
https://raw.githubusercontent.com/opendatalab/MinerU/master/mineru/backend/postprocess/table_merge/llm_cell_merge.py

[S11] MinerU — LLM辅助功能配置源码：
https://raw.githubusercontent.com/opendatalab/MinerU/master/mineru/config.py

[S12] PaddleX/PaddleOCR-VL — restructure_pages/merge_tables/relevel_titles（不是已验收的PP-Structure配置）：
https://paddlepaddle.github.io/PaddleX/3.7/en/pipeline_usage/tutorials/ocr_pipelines/PaddleOCR-VL.html

[S13] PyMuPDF — PyMuPDF4LLM：
https://pymupdf.readthedocs.io/en/latest/pymupdf4llm/

仅以上官方文档/原论文/官方源码作为技术引用，不以社区教程、机器人issue回复或商家“准确率”宣传替代证据。引用表示策略存在，不代表这些工具在本教材上都已实测或能无条件移植。

## 10. 本轮交接

- [x] 四类跨边界的方案和负例约束明确，窗口三层分开，结构/语义/上下文/父子/摘要路线对比。
- [x] 原页2/3失联原因确认：直接邻接，但页码/章节限制和重复header使扩展失败。
- [x] 原索引未动；正文/原图/原生JSON只在.local-eval，来源token费用本轮0。
- [x] STR-1/2代码、前端/生成/评测协议、真实检索A/B已在OPT-069完成；无需迁移/重嵌入。
- [x] STR-3/4在OPT-070实现/部署，811/81.63%、相同36+8全标注支持；真实跨页table/cell整体质量仍待新教材验证。
- [ ] STR-5/6尚未实施；684/79.51%、751/80.57%是历史基线。
- [x] 全appCI同口径80%在OPT-069本地验收通过（80.57%）。
- [ ] 复杂续表/真实冲突资料金标、批量生成/judge/人工产出质量和长期运营仍未验收。

本方案是现役设计入口；正式实现每轮依仓库规则追加OPT编号/同步tasks并回归，不把调研完成标成跨边界能力已上线。
