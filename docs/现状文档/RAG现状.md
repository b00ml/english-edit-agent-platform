# RAG 当前状态（P0/P1 + OCR + P2单文档运营/多资料评测，2026-10-05）

## 现役补充：OPT-074（2026-10-06）

已按顺序修复数量契约、父/子Outbox投递恢复和Redis持久化。外层quantity为批次N、每item归一1，多对象包装不静默截断；周期20秒maintenance＋SQL skip-locked/CAS lease fencing与item锁，bounded对账/重试/显式死信投递；Redis迁入命名卷+AOF everysec，原4236值核对一致/旧卷备份保留，增量写重启保留。

955完整测试通过（50集成/905非集成），app8120/9828=82.6211%；21文件lint及4源scoped strict通过，非全app/远端CI认证。main/CI head已为opt074_delivery_leases，已部署，原task8/content10/知识3doc82chunk及OCR/用户计数保持，无真实模型调用或课程重建。题型潜在多解、并发请求防重、状态词、独立Judge等不在本次前三项修复范围。

验收：[1—2—3落地与验收](10-6优化1-2-3落地与验收.md)。下方OPT-073/旧head/911/未开启AOF等为原阶段记录，不能当现役配置。

## 现役状态（2026-10-06，OPT-073）

当前主RAG链路已切换到structure/relation：新资料默认结构化leaf和逐段source_segments，Top5默认/Top8可选、32768字符。三资料均structure：并列句v3 54leaf+1parent，名词v2 23leaf保留第2/16页且block:15排除，动词v2 4leaf保留第1页。内置single/cloze/reading模板均required来源和人工核验。44条source gold迁移后Top5/8均齐备；6条真实生成只是小样本，完形已发现人工质量风险，不可无人值守发布。

完整现役验收见 `docs\现状文档\RAG-主链路切换与真实生成验收.md`。OPT-072的8192/legacy/895/42等表述保留为历史，不代表当前默认配置。

## 历史阶段记录（以下日期、计数和“当前/尚待”描述仅属于各自阶段）

> **STR-3/4已部署：OPT-070（记录2026-10-05 UTC；本机日志2026-10-06）**：逻辑续表多证据/未知待审、逐cell确认与原row/column映射、重复表头派生去重、局部2/3页后台复核（独立snapshot，不改成功页/索引）及跨章明确引用/原query+有界互补问题/多seed公平预算已实现。真实MinerU4.0.10原页2/3、2/3/4完成；相同36+8gold planning off/rules：36仍齐备、8由7→8齐备，无真实chat/judge/rerank或语料重嵌入。811回归/36真PG，app81.63%门槛通过，14模块scoped mypy；head为rag_str34_boundary，主/CI迁移前已备份。原Doc3/52向量+2parent、业务/OCR成功页不变，新增2个复核job；结构审核v2显式更新。178真实embedding/2140报告tokens，金额仅未核验配置估算。真实窗口不含表格，跨页table/cell规则用fixture/真PG验证，不冒充全教材OCR准确率；STR-5/6、专家金标/生产事实质量仍待。详见 `docs\现状文档\RAG-STR3-STR4落地与验收.md`。下方STR-3/4待实施及head旧值为历史阶段。

> **STR-1/2已部署：OPT-069（2026-10-05 UTC）**：文档级结构/重复页眉及连续小节、缺页/排除/题组/新章屏障、免费结构预览审核及hash/版本门控已实现；关系感知small-to-big仅从活动同scope leaf返回逐段真实引用，v2 bundle/segments协议已接API/前端/生成provenance/人工审核和v4评测，legacy可回退。相同原文/leaf向量36条回归35→36齐备，新8条6→7齐备（跨小节复合问题仍1条未完整，属STR-4）。751回归/32真PG通过，全app80.57%越过80%门槛，13模块scoped mypy不等于全app。运行relation/aliases/rerankoff，未重嵌入或新OCR；Doc3/52向量+2parent、原业务及head不变，只有并列句结构审核meta更新。181真实embedding/2037报告tokens，费用仅未核验配置估算，无chat/judge/rerank。详见 `docs\现状文档\RAG-STR1-STR2落地与验收.md`。STR-3～6及批量事实质量仍待；下方STR-0“未实现”和OPT-068“79.51%未达门槛”为历史。

> **2026-10-05 跨边界设计补充（STR-0，仅调研/方案）**：已核验官方文档/原论文与现有源码，四个硬边界分别在页内chunk、page/section/table parent、page/section neighbor及单页OCR入口。真实页2规则与页3续例已有直接next关系，但重复章页眉误当heading重置小节，page/section限制挡住扩展。建议文档级结构重建+可审核知识单元关系+small-to-big/分段引用；跨章节用多证据，疑难续表用局部多页复核，不机械整页拼接。主库只读，embedding/DB写入0，无新策略上线或本轮回归；当前仍OPT-068。实施与负例矩阵见 `docs\现状文档\RAG跨边界知识组织与检索优化方案.md`。

> **当前结论：2026-10-05，OPT-068（下方为历史阶段）**：已用可回退的有界中文bigram关键词通道修复无scope连系动词漏召回，不改知识点范围、不重嵌入语料。真实同语料A/B：原18条17→18命中；新16条精确块14→16、上下文齐备13→16；10条表格同行/13元组两策略均通过，没有新增表格准确率收益。另2条跨页探针仅1条带齐全部来源，页3续例仍在融合第11未进top5；36条仅35条标注上下文齐备，不称36/36完整回答。v3评测分开页/精确块/同行/全部来源与逐单位失败，并绑定语料/向量指纹。684回归通过/28真实PG；全app覆盖率79.51%仍未过CI80%，4模块scoped mypy不替代全app。92次真实embedding/944报告tokens，费用仅未核验配置估算；无chat/judge/rerank。原业务和3资料/52向量+2parent不变，head仍rag_ocr_review。当前部署bigram/32项、aliases默认、rerank off。详见 `docs\现状文档\RAG-P2中文召回修复与结构评测.md`；更早“原问题未修复/未重测覆盖率”等为历史，跨页和完整P2/批量生成质量仍待。

> **当前结论：2026-10-05，OPT-067（优先于下方历史快照）**：OCR 来源单文档显式重建/活动版本、失败保留旧索引、资料撤除及父子块删除一致性已部署；工作台恢复原审核排除/标签。并列句同 ID 已真实 v1→v2；新增名词仅页2/16、动词仅页1，主库当前3资料/52个真实1024维非零leaf向量+2parent，非三本整书。18条多文档冻结样本最终上下文PageHit@3=17/18、页MRR=0.916667；“连系动词为什么需要表语？”不限知识点漏召回，verbs scope对照命中，失败未被算作修复。最终659全量回归通过，含26真实PG集成；本轮26次真实embedding/5146报告tokens，金额仅未核验配置估算，无真实chat/judge/rerank。原task3/content4/user1不变，head仍rag_ocr_review。G1/G4为有限交付，完整P2/全库与批量生成事实质量仍未验收。详见 `docs\现状文档\RAG-P2索引运营与多文档评测.md`。下方649/1资料及更早的空知识库、未付费/未部署描述均为历史阶段，不代表当前运行态。

> **2026-10-05 OCR-4 与真实RAG更新（OPT-066）**：工作台提交/进度/原页与块/表格定位、排除/恢复、审核及费用确认、后台原子索引已部署，head rag_ocr_review。完整8页并列句教材经真实text-embedding-v3形成36个1024维非零leaf向量+2个parent，KnowledgeDocument1/Chunk38；8条单文档页级样本最终上下文Hit@3=8/8，不代表全库/事实正确率。14次真实embedding累计3642报告tokens，金额仅未核验全局配置估算。新增17项，全量649通过；原task/content/user不变。其余教材/批量生成质量与P2运营仍待，详见 `docs\现状文档\OCR-4工作台与真实RAG验收.md`。

> **2026-10-05 OCR-3 运行态更新（OPT-065）**：后台文档/页任务、独立OCR队列/scheduler、hash缓存、取消/重试/租约与断点续跑已部署原Compose；新head rag_ocr_jobs。真实名词选页取消续跑、87,315,331字节动词PDF经代理上传202→completed并缓存命中通过。新增38项回归，全量632通过；主库原task3/content4/user1不变，知识表仍0，新OCR任务3/页检查点4是有意保留的真实验收数据。无付费模型或知识入库，网页界面/确认流程待OCR-4。详见 `docs\现状文档\OCR-3后台任务与大文件验收.md`。

> **2026-10-05 OCR-1/2 接入更新（OPT-064）**：已实现本地 MinerU 4.x V1 adapter、显式逐页路由、合并表格/标题/脚注/坐标桥接和现有父子切块预览；真实原 PDF 的3页验证、12项同行检查通过。新增52条回归，573非集成/594全量通过。入口目前是本地CLI/可复用Python，不是网页自动OCR；部分选页禁止正式入库，知识表仍为空，未调用付费模型。主容器未重建，后台批次/网页入口尚待OCR-3/4。详情 `docs\现状文档\MinerU-OCR接入与验收.md`。

> **2026-10-05 OCR 选型实测更新（OPT-063）**：PaddleOCR/MinerU/Docling 在相同 12 页教材 PNG 上均成功；36 个规范化锚点完全匹配分别 35/35/34，近似匹配 36/36/35，12 个同表行检查均通过。以上不是全页准确率/整表正确率或检索效果。优先候选 MinerU basic/ONNX；Docling 样本存在标题阅读顺序问题，Paddle GPU 未测。仅本机 CPU 评测，**生产 OCR/大文件批次仍未接入，知识库仍为空**，没有付费 embedding。新增 9 条测试，项目 venv 非集成 521 通过；原服务健康、旧业务计数不变。详情 `docs\现状文档\OCR与文档解析选型实测.md`。

> **2026-10-05 运行态更新（OPT-062）**：原Compose的Postgres/Redis/backend/worker/frontend已启动，主库实际迁移rag_p1_def、ready200+PostgresSaver健康、真实Redis/worker空Outbox回执；浏览器登录与课程PDF预览已验收。Nginx上传上限和无文字预览/入库提示已修复。课程15份PDF共341页全无可抽取文字，12份超10MiB，OCR仍未接入，RAG资料没有入库；无付费模型。512非集成回归通过，不代表课程召回率或事实正确率。详见 `运行态与教材预览验收.md`，旧“主服务未启动”段落为历史快照。


> **2026-10-05 数据库验证记录（OPT-061，早于运行态验收）**：已修复 Compose RAG 全参数透传，并直接复用已有 `english-edit-ci-postgres` 实际迁移到 `rag_p1_def`；PostgreSQL专项13项、集成目录21项通过。新增20项单测，非集成506项，全量527项断言通过；本地全app覆盖率约77.45%，80%门槛未过。没有新建隔离环境、清库或付费模型调用，教材未导入；主全栈/真实队列随后在 OPT-062 验收，真实语义质量仍待。详情见 `RAG-部署与PostgreSQL验收.md`。**下文2026-10-04“未运行真实数据库”的描述是历史快照，不再代表所有当前验证状态。**


> 下文 P0 与原评估为历史快照；当前能力以本节和 `RAG-P1-DEF落地与验收.md` 为准。

已实现：P0 结构化解析/保护切块 + 新文档父子/邻接 + hybrid 召回（pgvector + PostgreSQL FTS/字面）+ RRF + off/optional/required rerank adapter + YAML 知识点 canonical/aliases/多标签/祖先/子孙/关联 + aliases/可选 LLM query expansion。

- 可以按“现在时 / simple present / 一般现在时”归一到同一配置知识点，默认 exact 不放宽到关联点。知识目录不是权限，所有查询仍按 tenant/document/source/section scope。
- 向量只给 leaf；父块不额外 embedding，child 命中后可回溯 parent。上下文去重与预算包括来源 wrapper，截断后的快照/hash/源坐标同步。
- 默认 rerank off、扩展 aliases（不调用 chat）；外部重排/LLM query 改写需要用户明确配置，当前没有真实供应商验收。未知 rerank 计费不会伪装为免费。
- 65 条新增回归，486 passed / 8 deselected；限定 P1 模块 coverage 95%，非全 app/生产证据。
- 新迁移 `rag_p1_def` 仅离线 SQL 通过；部署前须备份迁移，实际 HNSW/FTS/trigram 索引和 query plan 未实测。
- 尚未：P2 重建索引运营化、真实 Recall@K/MRR/NDCG/压测、OCR/PDF 布局、自动知识点抽取或完整知识图谱。旧数据不自动重嵌入/补父子结构，旧 Word 丢失表格仍需原文件重新导入。

---

# RAG 当前状态（P0 A/B/C 后，2026-10-04）

> 本页下半部分保留的是 **P0 A/B/C 前的评估原文**，不是当前能力清单。当前状态以本节与 `RAG-P0-ABC落地与验收.md` 为准。

## 已落地

- 结构化 parser 输出 DocumentBlock/ParsedDocument；支持 txt/md/markdown/docx/pdf/html/htm/xlsx/csv。
- DOCX 段落与表格按 body 顺序输出，保留空列、合并信息/嵌套表格文本与 warning。
- Markdown/HTML 表格标准化，Excel/CSV 保留 sheet/列标签/真实行号；PDF 保留页码、空页和失败 warning。
- 自适应切块和策略验证 fallback，修复重复换行边界小块；标题上下文、表头补回、代码/公式保护与超大块告警。
- chunk 的 start/end 指向持久化规范化文本；原文、context_header、embedding 输入分离。覆盖率是源位置并集，不因重复表头/overlap 被虚增。
- `knowledge_document` 保存解析快照；chunk 保存 document_id、位置、页码、章节、parser/chunker/embedding 版本和 hash。
- 保留旧 chunk 的查询/删除/引用、tenant scope、知识点 exact filter 和 off/optional/required。新资料使用新切块策略，旧资料不自动重嵌入。
- 后端预览/诊断接口、前端解析切块预览和 warning；预览不调用模型。

## 仍未具备或未验证

- 未实现 Parent-Child、邻接扩展、关键词/BM25、RRF、rerank、知识点 alias/层级与 query expansion（P1 待做）。
- 未实现 OCR、PDF 布局感知、复杂 HTML rowspan/colspan 的视觉还原；源 warning 必须人工核对。
- 超大行/公式/代码仍可能硬切并告警；token 预算为 UTF-8 字节保守预算，非精确 tokenizer。
- 旧 DOCX 若此前已丢表格，需要由原文件重新上传；代码升级不能恢复历史丢失内容。
- 没有真实 Postgres/Redis/embedding/LLM 部署验证，也没有真实检索 Recall@K、MRR/NDCG 或产出事实正确率数据。
- 单测 421 passed / 8 deselected 不等于部署验收；RAG 修改模块 scoped coverage 91% 不等于全 app 覆盖率。

### 对原评估的一项数学修正

单路向量检索按同一余弦指标排序时，top_k 后再按该指标过滤，**并不会使低于前 k 名的结果突然满足更高阈值**。小候选池确实不利于后续混合召回、去重、rerank 和多样性，但不应把“top 3 都低于阈值，却有第 4 个高于阈值”作为当前同一排序指标下的根因。

---

# 历史基线：P0 A/B/C 前

# 结论先说

当前项目已经具备一个**基础可用的文本向量 RAG**，但还不是完整的“文档理解型 RAG”。

更准确的定位是：

> **带租户隔离、知识点精确过滤、pgvector 向量召回、相似度阈值、来源溯源和 RAG 必需模式控制的基础 RAG。**

不宜描述为：

- 表格友好型 RAG
- 企业级文档 RAG
- 混合检索 RAG
- 高召回高精度 RAG
- 与 WeKnora 同等级的知识库系统

如果资料主要是**人工整理的纯文本、Markdown、短段落英语语法规则**，当前实现可以作为内部自用版本。

如果资料包括：

- Word 教材
- 含表格的试卷
- PDF 版教材
- Excel 知识表
- 扫描件
- 大量长文档
- 需要精确引用页码、章节、原文位置

当前实现还不够稳，尤其存在**内容丢失、表格丢失、切块破坏语义、知识点标签不一致导致召回失败**等问题。

---

# 一、当前项目的实际 RAG 链路

相关实现主要在：

- `backend\app\rag\parser.py`
- `backend\app\rag\indexer.py`
- `backend\app\rag\retriever.py`
- `backend\app\r ag\embedding.py`
- `backend\app\workflow\graph.py`
- `backend\app\models.py`

当前链路基本是：

```text
上传文件 / 文本
    ↓
按后缀解析成纯文本
    ↓
固定字符数切块
    ↓
每个 chunk 调 embedding
    ↓
写入 KnowledgeChunk
    ↓
生成任务传入 knowledge_point
    ↓
knowledge_point 作为查询文本进行 embedding
    ↓
按 tenant_id + knowledge_point 精确过滤
    ↓
pgvector 余弦距离排序
    ↓
取 top_k
    ↓
Python 层相似度阈值过滤
    ↓
将命中 chunk 拼接到生成 Prompt
```

当前实现的核心特点是：**先通过 metadata 做范围过滤，再在范围内做向量检索。**

---

# 二、当前项目做到了什么程度

## 1. 文档解析

当前支持：

```text
.txt
.md
.docx
.pdf
```

实现方式：

| 文件类型 | 当前实现 | 主要问题 |
|---|---|---|
| TXT | UTF-8，失败回退 GB18030 | 基本可用 |
| Markdown | 作为纯文本读取 | 没有识别标题、表格、代码块 |
| DOCX | `python-docx` 读取 `document.paragraphs` | 表格不会被提取 |
| PDF | `pypdf` 按页 `extract_text()` | 没有布局感知、没有 OCR、表格顺序可能错乱 |

### DOCX 的关键问题

当前代码本质上是：

```python
paragraphs = [
    p.text
    for p in document.paragraphs
    if p.text and p.text.strip()
]
return "\n".join(paragraphs)
```

它只遍历正文段落，没有遍历：

```python
document.tables
```

所以 Word 中的表格不是“被切断”，而是更严重：

> **在解析阶段就直接丢失了。**

例如一个 Word 文件：

```text
正文：一般现在时用于表示习惯性动作。

表格：
主语 | 动词形式
I/You/We/They | do
He/She/It | does

正文：注意第三人称单数变化。
```

当前解析结果可能只剩：

```text
正文：一般现在时用于表示习惯性动作。
正文：注意第三人称单数变化。
```

表格中的规则、例句、对照关系都不会进入 embedding。

---

## 2. 当前切块实现

当前实现位于：

```text
backend\app\rag\indexer.py
```

默认参数：

```python
_DEFAULT_MAX_CHARS = 500
_DEFAULT_OVERLAP = 50
```

具体逻辑：

1. 对全文 `.strip()`
2. 每块最大约 500 个字符
3. 尝试在当前窗口中最后一个换行处切分
4. 找不到换行时，按字符硬切
5. 下一块回退 50 个字符形成 overlap

伪代码：

```text
取 start 到 start + 500
    ↓
如果中间有换行，在最后一个换行处切
    ↓
下一次从 end - 50 开始
```

### 当前切块并不是语义切块

当前不会识别：

- 句号
- 中文句号 `。`
- 分号
- 段落语义
- Markdown 标题
- Markdown 表格
- 代码块
- 数学公式
- 文档页边界
- 章节层级
- 题目与解析的对应关系

因此它只是：

> **“优先按换行，否则按固定字符数切”的字符切块器。**

---

# 三、会不会切断语义？

会，而且已经能复现。

## 1. 长句或规则会跨 chunk

例如：

```text
RULE_START:
第三人称单数主语使用一般现在时谓语动词时，
通常需要在动词后加 s 或 es，但特殊动词形式不同，
并且在否定句和疑问句中需要恢复动词原形。
RULE_END
```

如果这一段跨越 500 字符边界，当前实现可能得到：

```text
chunk 1:
... RULE_START ...
第三人称单数主语使用一般现在时谓语动词时，
通常需要在动词后加 s 或 es ...

chunk 2:
... 但特殊动词形式不同，
并且在否定句和疑问句中需要恢复动词原形。
RULE_END ...
```

没有任何一个 chunk 同时包含完整规则。

`overlap=50` 只能缓解边界问题，不能保证语义单元完整保留。

---

## 2. 当前实现存在换行边界病态情况

当前切块逻辑有一个实际风险：

```python
newline = text.rfind("\n", start, end)
if newline > start:
    end = newline

start = max(end - overlap, start + 1)
```

当某个换行位置距离当前起点很近时，下一轮仍然可能在同一个换行处切分，造成：

```text
[480, 50, 49, 48, 47, 46, ...]
```

也就是后面不断出现很小的 chunk。

这会带来：

- chunk 数量异常增加
- embedding 调用次数增加
- 索引成本增加
- 小片段语义不足
- 召回噪声增加
- 可能导致数据库中产生大量低质量碎片

这不是理论问题，之前已经通过纯内存样例复现。

---

## 3. 字符长度不是 token 长度

当前按 Python 字符数切分，不按模型 token 切分。

这会导致：

- 英文和中文的 token/字符比例不同
- 标点、空格、特殊符号的 token 成本不同
- 长 URL、代码、公式可能迅速占用 token
- embedding 模型和生成模型的上下文预算无法精确控制

所以 `500 字符` 并不等于稳定的 `500 token`。

---

# 四、表格会不会被切断？

当前项目要分三种情况看。

## 1. DOCX 表格：直接丢失

当前 `parser.py` 只提取段落，不提取表格。

结论：

> DOCX 表格不是被切断，而是在解析阶段丢失。

这是目前最严重的 RAG 数据完整性问题之一。

---

## 2. Markdown 表格：会被当普通文本切开

例如：

```markdown
| 语法项目 | 用法 | 例句 |
|---|---|---|
| 一般现在时 | 表示习惯 | I go to school. |
| 现在进行时 | 表示正在发生 | I am reading. |
| 一般过去时 | 表示过去动作 | I went home. |
```

当前切块器不会识别这是表格，只会把它当成普通字符流。

可能得到：

```text
chunk 1:
| 语法项目 | 用法 | 例句 |
|---|---|---|
| 一般现在时 | 表示习惯 | I go to school. |
| 现在进行时 | 表示正在发生 |
```

```text
chunk 2:
I am reading. |
| 一般过去时 | 表示过去动作 | I went home. |
```

问题包括：

- 表头只存在于第一块
- 后续 chunk 没有列名
- 行可能被切成两半
- 一行内部的列关系可能被破坏
- embedding 无法准确理解字段含义
- 召回后生成模型看到的是残缺表格

之前已做过纯内存验证：长 Markdown 表格被分成多个 chunk，后续 chunk 不再包含表头。

---

## 3. PDF 表格：可能变成错序文本

`pypdf.extract_text()` 只负责提取文字，不保证还原页面视觉布局。

PDF 中的表格可能被抽取成：

```text
主语 谓语 例句
I do I do my homework.
He does He does his homework.
```

也可能变成：

```text
主语 I He
谓语 do does
例句 I do my homework. He does his homework.
```

还可能出现：

- 列顺序变化
- 单元格内容交错
- 页眉页脚混入正文
- 两栏文字串在一起
- 表格边框信息丢失
- 题干和选项顺序混乱

之后再进行固定字符切块，问题会进一步放大。

---

## 4. Excel：当前不支持

当前 `SUPPORTED_EXTENSIONS` 中没有：

```text
.xlsx
.xls
.csv
```

因此 Excel 知识表不能直接入库。

---

# 五、当前是如何保留语义的？

当前真正起作用的语义保留机制只有几项：

## 1. 换行优先

尽可能在换行处切分，而不是直接切满 500 字符。

但它只识别换行，不识别句子、标题、表格或章节。

---

## 2. 50 字符 overlap

相邻 chunk 之间保留 50 个字符重叠。

作用：

- 缓解一部分边界丢失
- 让相邻 chunk 之间保留少量上下文

局限：

- 不能保证完整语义单元
- 不能解决标题丢失
- 不能解决表头丢失
- 不能解决题干和解析分离
- 不能解决跨页、跨段落关系
- 不能解决长句超过 overlap 的情况

---

## 3. embedding 整个 chunk

每个 chunk 单独调用 embedding，然后将向量写入：

```text
KnowledgeChunk.embedding
```

当前代码实际是**每个 chunk 逐一向量化**。`indexer.py` 中“文本整体先做一次 embedding”的注释与实际代码并不完全一致，实际没有看到单独的文档级向量入库。

---

## 4. 生成时注入原始 chunk 文本

命中后会拼接成类似：

```text
[来源 chunk_id: source_name]
chunk content
```

再传入生成 Prompt。

P1 修复后还增加了：

- `chunk_id`
- `source_name`
- `source_type`
- `content_hash`
- `content`
- `similarity`
- `verification`
- `provenance`
- RAG status
- trace/lifecycle event

这些增强了可追溯性，但它们属于**来源记录能力**，并不能弥补前面解析和切块造成的语义损失。

当前没有：

- `ContextHeader`
- 章节路径
- 父子 chunk
- 相邻 chunk 扩展
- 标题自动补全
- 表头自动补全
- 题目与解析关联
- 页码与原文坐标
- 召回后上下文扩展

---

# 六、能不能按知识点召回？

## 可以，但属于“手工标签 + 精确过滤”

当前知识点字段是：

```text
KnowledgeChunk.knowledge_point
```

上传资料时可以传：

```text
knowledge_point = "一般现在时"
```

生成时也传：

```text
knowledge_point = "一般现在时"
```

检索时实际执行：

```text
knowledge_point 精确过滤
    +
query embedding
    +
pgvector cosine distance
```

也就是说：

```text
知识点标签完全一致
    ↓
在这个知识点范围内做向量召回
```

## 当前支持的情况

例如：

```text
上传资料：
knowledge_point = "一般现在时"

生成请求：
knowledge_point = "一般现在时"
```

可以在“ 一般现在时 ”这个标签下召回相关 chunk。

如果知识点为空，生成流程目前基本不会构造有效的 RAG 查询。

---

## 当前不支持的情况

以下能力目前没有：

- 自动从文档识别知识点
- 知识点层级
- 知识点多标签
- 同义词匹配
- 别名匹配
- “现在时”匹配“一般现在时”
- “第三人称单数”关联“主谓一致”
- 知识点分类器
- query rewrite
- query expansion
- BM25 关键词召回
- RRF 融合
- reranker
- 召回相邻知识点
- 相关知识点扩展

例如资料标记为：

```text
一般现在时
```

但生成请求传：

```text
现在时
```

当前的精确过滤很可能直接没有结果。

因此目前的“按知识点召回”更接近：

> **按人工标签限定检索范围，而不是对知识点进行智能理解。**

---

# 七、当前查询链路还有哪些问题？

## 1. 只有向量召回，没有关键词召回

当前核心是：

```text
query embedding
→ pgvector cosine distance
```

没有 BM25、全文检索或关键词匹配。

这对以下内容不利：

- 具体语法术语
- 固定搭配
- 数字
- 题号
- 选项编号
- 专有名词
- 精确例句
- 否定词
- `do / does`
- `have / has`
- `-ed / -ing`
- 字母、符号、缩写

向量相似不等于字面精确匹配。

---

## 2. 没有 rerank

当前检索流程没有第二阶段重排。

实际是：

```text
向量召回 top_k
→ 相似度过滤
→ 直接注入 Prompt
```

没有：

```text
候选池扩大
→ reranker 精排
→ 去除重复
→ 选最终上下文
```

因此 top 3 中可能出现：

- 多个相似重复片段
- 语义接近但不回答问题的片段
- 相关但不够具体的片段
- 规则介绍命中，但例外条件没有命中

---

## 3. `top_k` 太小且过滤发生在后面

当前 SQL 先按向量距离取 `top_k`，然后在 Python 层根据：

```python
RAG_MIN_SIMILARITY
```

过滤。

这意味着：

```text
数据库只给出 top 3
    ↓
Python 再过滤
```

如果 top 3 中有两个低于阈值，系统不会继续向数据库请求第 4、5、6 个候选。

可能出现：

> 实际有合格结果，但因为候选池太小，最终被错误判断为没有命中。

更合理的方式应是：

```text
先取更大的候选池，例如 top 20/50
    ↓
阈值过滤
    ↓
去重
    ↓
rerank
    ↓
最终取 3 个
```

---

## 4. 当前没有显式看到 ANN 索引

当前代码使用了 pgvector 的余弦距离查询，但在仓库搜索中没有看到明确的：

```sql
CREATE INDEX ... USING hnsw
```

或：

```sql
CREATE INDEX ... USING ivfflat
```

因此需要区分：

- 代码层：实现了 pgvector 余弦距离查询
- 数据库层：是否有 HNSW/IVFFlat 索引尚未确认
- 性能层：大规模数据下的检索耗时尚未验证

小数据量可能没问题；数据量扩大后，可能退化为全表扫描。

---

# 八、WeKnora 做到了什么程度？

WeKnora 的 RAG 不是只做“文本切块 + 向量查询”，而是覆盖了更完整的文档处理与检索链路。

主要目录包括：

```text
<本地WeKnora源码目录>\internal\infrastructure\chunker
<本地WeKnora源码目录>\docreader
<本地WeKnora源码目录>\internal\infrastructure\docparser
<本地WeKnora源码目录>\internal\application\service
```

---

## 1. 文档解析能力更完整

WeKnora 提供多级解析路径，包括：

- DOCX
- PDF
- HTML
- Markdown
- Excel
- PPT 等 Office 文件
- 图片
- OCR
- 扫描 PDF
- 内嵌图片和资源
- 多种转换器 fallback

### DOCX

WeKnora 不只遍历段落，还会按照文档 body 顺序处理：

- 段落
- 表格
- 段落
- 表格

Word 表格会转换为 Markdown/GFM 风格，并处理：

- 合并单元格
- `rowspan`
- `colspan`
- Markdown 中的 `|`
- 表格列数
- 表格顺序

因此不会像当前项目一样直接丢掉 `document.tables`。

### Excel

WeKnora 可以按 sheet 处理，并能将行转换成带字段含义的文本。

另外还有表格摘要和列描述能力，大致包括：

```text
表格整体说明
表格列说明
样例数据说明
```

这类信息会额外构建成可检索的 chunk。

---

# 九、WeKnora 如何切块？

WeKnora 使用自适应 chunker，而不是单一固定切块。

核心策略包括：

```text
auto
heading
heuristic
recursive
legacy fallback
```

默认配置大致是：

```text
ChunkSize    = 512 字符
ChunkOverlap = 80 字符
```

同时支持：

- 分隔符配置
- 语言配置
- token limit
- chunk 诊断
- chunk 验证
- parent-child chunk
- 多级 fallback

## 自适应策略

WeKnora 会根据文档特征选择策略：

- 有标题结构时，优先标题感知切分
- 有明显段落和句子边界时，使用 heuristic
- 普通长文本使用递归分隔
- 异常情况下使用 legacy fallback

它不是简单地从字符串第 500 个字符处截断。

---

# 十、WeKnora 如何保留语义？

WeKnora 比当前项目多了几层语义保留机制。

## 1. 标题层级

它会识别标题层级，并形成类似：

```text
英语语法
  → 时态
    → 一般现在时
      → 第三人称单数
```

## 2. `ContextHeader`

WeKnora 有单独的：

```go
ContextHeader
```

它不会破坏原始正文内容和字符坐标，但在 embedding 时会使用：

```text
ContextHeader + chunk content
```

例如正文 chunk 是：

```text
He goes to school every day.
```

embedding 内容可能带有：

```text
英语语法 > 时态 > 一般现在时 > 第三人称单数

He goes to school every day.
```

这样即使 chunk 本身没有标题，也不会完全丢失章节语境。

当前项目没有这一层。

---

## 3. Parent-child chunk

WeKnora 支持：

```text
Parent chunk：较大、上下文完整
Child chunk：较小、适合召回
```

检索流程可以是：

```text
child embedding 命中
    ↓
回溯 parent
    ↓
将更完整的 parent 上下文返回给模型
```

这样可以兼顾：

- 小 chunk 的召回精度
- 大 chunk 的上下文完整性

当前项目只有单层 chunk，没有 parent-child 关系。

---

## 4. 相邻上下文

WeKnora 的 chunk 还保留：

- 顺序号
- 起止位置
- 前后 chunk 关系
- parent chunk 关系

因此召回一个 chunk 后，可以进一步扩展邻接上下文。

当前项目只返回命中的 chunk 内容，不会自动取前后块。

---

# 十一、WeKnora 如何处理表格？

WeKnora 也不是“永远不拆表”，而是采用：

> **表格识别 + 表头跟踪 + 表头补回 + 受控拆分。**

## Markdown 表格

它能识别 Markdown table protected pattern：

- 表头
- 分隔线
- 数据行
- 表格边界
- 列数变化

如果大表格超过 chunk 上限，仍然可能拆分，但后续 chunk 会携带或补回表头。

例如原表格：

```markdown
| 语法项目 | 用法 | 例句 |
|---|---|---|
| 一般现在时 | 表示习惯 | I go to school. |
| 现在进行时 | 正在发生 | I am reading. |
```

拆分后后续块可能保持：

```markdown
| 语法项目 | 用法 | 例句 |
|---|---|---|
| 现在进行时 | 正在发生 | I am reading. |
```

这样至少不会失去列语义。

## HTML 表格

WeKnora 会尝试标准化 HTML table。

对于复杂的 `rowspan`、`colspan`，不能简单转成普通 Markdown 时，会保留更接近 HTML 的结构，而不是强行破坏成错误的 Markdown 表格。

## 超大表格

WeKnora 也不能保证任何超大表格完全不拆。

实际策略是：

```text
大表格可以拆
    ↓
但保留表头/表格上下文
    ↓
尽量维持列语义
```

这比当前项目“直接当普通文本切开”要健壮很多。

---

# 十二、WeKnora 的检索能力

WeKnora 不止做向量检索，通常包含：

```text
向量召回
关键词召回
    ↓
RRF 融合
    ↓
去重
    ↓
rerank
    ↓
阈值过滤
    ↓
返回上下文
```

还提供：

- 多知识库 fan-out
- tag 过滤
- knowledge 过滤
- 租户与权限过滤
- query expansion
- 多 query 并发召回
- rerank 失败 fallback
- rerank 阈值降级
- 候选池控制
- embedding 模型维度校验

因此 WeKnora 的优势不是某一个单点，而是：

> **解析、切块、上下文、召回、融合、重排、权限和可观测性形成了完整流水线。**

---

# 十三、核心差距矩阵

| 能力 | 当前项目 | WeKnora |
|---|---|---|
| TXT/Markdown | 支持 | 支持 |
| DOCX 段落 | 支持 | 支持 |
| DOCX 表格 | 丢失 | 按 body 顺序解析并保留 |
| PDF | 普通文本抽取 | 多解析器、布局/OCR 路径 |
| 扫描 PDF | 不支持 | 有 OCR 路径 |
| Excel | 不支持 | 支持 sheet、行、表格摘要 |
| HTML 表格 | 不支持 | 有标准化处理 |
| 切块方式 | 固定 500 字符 | 自适应多策略 |
| 句子边界 | 不识别 | heuristic/recursive |
| 标题层级 | 不保留 | heading-aware |
| 上下文标题 | 无 | `ContextHeader` |
| Parent-child | 无 | 支持 |
| 相邻 chunk 扩展 | 无 | 支持相关机制 |
| overlap | 50 字符 | 默认 80 字符，可配置 |
| Markdown 表格保护 | 无 | 有 |
| 表头补全 | 无 | 有 |
| 向量召回 | 有 | 有 |
| 关键词召回 | 无 | 有 |
| RRF | 无 | 有 |
| rerank | 无 | 有 |
| query expansion | 无 | 有 |
| 知识点过滤 | 人工标签精确匹配 | tag/metadata/filter 等更丰富 |
| 来源追踪 | P1 已有 chunk provenance | 更完整的 source/parent/位置链 |
| 召回质量评测 | 尚不完整 | 有更多检索链路能力 |
| 大规模性能 | ANN 索引尚未确认 | 有较完整的检索基础设施 |

---

# 十四、当前 RAG 可能遇到的主要问题

## A. 解析阶段

1. DOCX 表格静默丢失
2. PDF 表格顺序错乱
3. 扫描 PDF 无 OCR
4. 图片中的题干或表格无法识别
5. 页眉页脚污染正文
6. 多栏 PDF 文字顺序异常
7. 不支持 Excel、CSV、PPT、HTML
8. 编码或特殊字符导致内容异常

## B. 切块阶段

1. 长句被切断
2. 题干和选项被拆开
3. 题目和解析被拆开
4. 规则和例外被拆开
5. Markdown 表格行被切断
6. 表头不会补回
7. 标题不会继承
8. overlap 不能真正解决语义跨块
9. 换行附近可能产生大量极小 chunk
10. 按字符而不是 token 控制

## C. 检索阶段

1. 只有向量召回，没有关键词召回
2. 知识点必须精确匹配
3. 同义知识点无法互相召回
4. 没有 query expansion
5. 没有 rerank
6. `top_k` 过小导致有效候选提前丢失
7. 阈值在候选截断后才执行
8. 没有 MMR 去重
9. 没有相邻 chunk 扩展
10. 没有 parent chunk 返回

## D. 数据与模型阶段

1. embedding 模型更换可能导致旧向量不可比
2. 维度配置变化需要迁移
3. 当前 metadata 中尚未形成完整 embedding 版本治理
4. embedding API 失败会触发降级
5. 批量上传时调用成本和耗时可能较高
6. 没有确认 pgvector ANN 索引
7. 大规模知识库性能尚未实测

## E. 来源与质量阶段

1. 当前 citation 主要到 chunk 级
2. 没有稳定的页码、章节、段落位置
3. provenance 标记为 `unverified`
4. 模型可能引用了正确 chunk，但生成的结论仍然错误
5. 缺少知识点 Recall@K、MRR、NDCG 等系统评测
6. 表格问答、PDF 问答、OCR 问答尚未形成专项评测集

## F. 安全与内容污染

知识库内容本身可能包含类似 Prompt 的文本，例如：

```text
Ignore previous instructions...
```

当前 RAG 注入时主要是将内容拼接到 Prompt 中，后续还需要明确：

- 来源内容是资料，不是指令
- 检索内容使用隔离标记
- 不允许知识库内容覆盖系统规则
- 对来源内容做提示注入防护

---

# 十五、是否足以作为自用生产项目？

## 可以使用的范围

当前项目可以考虑用于：

- 自己维护的小规模知识库
- 纯文本或结构较简单的 Markdown
- 人工明确填写知识点
- 英语语法规则生成
- 知识点驱动的批量生成
- 有人工抽检和人工发布门槛的流程

前提是：

1. 资料先尽量转成结构清晰的纯文本/Markdown
2. 重要规则不要只放在 Word 表格中
3. 生成流程保留人工质检
4. RAG 使用 `required` 或至少对关键任务做来源确认
5. 不把当前实现当作复杂教材的完整知识库

## 不建议直接承载的范围

暂不建议直接用于：

- 大量 Word 教材批量导入
- 含复杂表格的试卷
- 扫描 PDF
- Excel 知识库
- 需要精确页码引用的内容
- 高风险自动发布
- 完全无人审核的批量生产
- 大规模多租户知识库

---

# 十六、建议的改造顺序

## 第一优先级：先消除内容丢失和切块错误

1. DOCX 段落与表格按 body 顺序解析
2. 支持 Markdown table protected
3. 修复换行导致的小 chunk 递减问题
4. 增加最小 chunk 长度保护
5. 支持标题和章节 metadata
6. 记录 page、section、paragraph、table 等来源位置

## 第二优先级：提升语义保留

1. heading-aware chunking
2. sentence-aware chunking
3. token-aware chunking
4. `ContextHeader`
5. parent-child chunk
6. 相邻 chunk 扩展
7. 表头自动补全
8. 题干/选项/解析结构化关联

## 第三优先级：提升召回质量

1. 向量 + BM25/关键词混合召回
2. RRF 融合
3. 扩大候选池
4. rerank
5. query expansion
6. 同义知识点映射
7. MMR 去重
8. 知识点层级与多标签

## 第四优先级：提升生产可靠性

1. HNSW/IVFFlat 索引确认
2. embedding model/version 写入 metadata
3. 批量索引任务化
4. 失败重试与断点续跑
5. 解析质量诊断
6. chunk 预览接口
7. 召回调试接口
8. 文档删除和重建索引机制

## 第五优先级：建立专项评测

至少建立四类测试集：

```text
纯文本语法规则
Word 表格
PDF 教材
Excel 知识表
```

重点指标：

```text
Recall@K
MRR
NDCG
知识点命中率
表格行/列召回率
来源引用准确率
无依据生成率
RAG 降级率
```

---

# 最终判断

当前项目和 WeKnora 的差异，不是“默认 chunk size 是 500 还是 512”这么简单。

当前项目目前主要完成了：

```text
纯文本解析
→ 固定切块
→ embedding
→ pgvector 向量召回
→ 知识点精确过滤
→ Prompt 注入
→ 来源追踪
```

WeKnora 则已经覆盖：

```text
多格式文档解析
→ 表格/OCR/结构保留
→ 自适应切块
→ 标题上下文
→ parent-child
→ 表格保护
→ 向量 + 关键词
→ RRF
→ rerank
→ query expansion
→ 多级过滤和检索治理
```

所以当前项目不是不能用，而是适合被定位为：

> **面向英语教研场景的基础版、可控范围内可用的 RAG。**

要成为可以稳定支撑批量内容生产的生产级 RAG，最应该先补的不是 rerank，而是：

1. **先保证文档内容不丢**
2. **再保证切块不破坏结构**
3. **再增强上下文保留**
4. **最后做混合检索和 rerank**

本轮仅进行了源码和已有纯内存验证结果的分析，**没有启动 Docker、没有连接真实 PostgreSQL/Redis、没有调用真实 embedding 或 LLM，也没有修改代码**。