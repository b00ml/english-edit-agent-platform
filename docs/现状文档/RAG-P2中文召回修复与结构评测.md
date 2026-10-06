# RAG P2：中文自由问句召回修复与结构化来源评测

**日期**：2026-10-05
**变更**：OPT-068
**状态**：已部署并真实验证中文关键词补召回、分阶段诊断、精确块/同行/全部来源评测。原连系动词漏召回修复；新跨页探针仍有 1 条不完整，完整 P2/无人值守批量生产尚未验收。

## 1. 问题定位，不按单题硬编码

OPT-067 的问题“连系动词为什么需要表语？”：

- 正确的动词分类表在 vector 候选第 **4** 位，已经进入 30 个候选的池子。
- 中文连续字串被当作完整词串匹配；原 keyword lane 返回 **0**。不是正文或表格没入库，也不是 tenant/知识点过滤误排除。
- rerank off 时融合只有 vector 信号，最终 top3 全为并列句。之前加 verbs scope 可绕开竞争，但不是全库修复。

本轮没有给该问题配置专用路由，也没有给“动词”偷偷加自动 scope；修复面是所有自由中文问句的关键词通道。

## 2. 代码交付

### 2.1 有界中文相邻二字片段

保留原完整短语、英文词项、数字/符号及 PostgreSQL English FTS，再增加连续中文串的重叠二字片段。例如问题中能提取“连系”“系动”“动词”“表语”。

- `RAG_CJK_KEYWORD_MODE=bigram` 默认启用；`legacy` 保留旧的完整字串/16 项策略，作为回退和 A/B 对照。
- `RAG_KEYWORD_MAX_TERMS=32`，Pydantic 限制 2–64；长问题按位置采样，避免所有末尾词被截掉。不生成大量单字 LIKE，不引入新分词库/模型。
- SQL 仍参数绑定和 LIKE autoescape，沿用 tenant/document/source/knowledge scope 和非 parent 条件。补 PG 实测验证 `%_` 不成通配符、外租户和 parent 不混入结果。
- 没改向量模型、阈值、RRF 权重、默认知识点范围或父子/邻接预算；也没有重嵌入已有语料。
- backend/生成 worker/OCR worker/scheduler 共享 Compose 配置，示例环境文件同步。运行服务仍为 query expansion **aliases**、rerank **off**；A/B 仅对应评测进程设 query expansion off。

**边界**：bigram 是字面片段，不是中文语义分词、BM25 或事实判定器。常见词/资料标题会带来噪声；32 项 LIKE + FTS 的小语料效果不代表大规模查询性能。

### 2.2 可定位的分阶段诊断

- lane 增加候选的 chunk/document/page/table 身份、similarity、keyword 相对排序分，以及本次关键词 terms；不额外输出全部候选正文/密钥。
- fusion/rerank 同样保留来源身份；rerank 诊断现在来自实际 ranked 输出，不用未过滤的 fused 列表冒充最终排序。
- 诊断版本 `rag-retrieval-v2`；保持旧 `chunk_ids` 和 scope/fallback 字段兼容。

### 2.3 评测器 v3：不把命中一页当成全部回答

`rag_eval.py` 兼容旧页级 gold，新增：

- `chunk_ids`（parent 用 matched_child_ids 校验）、`table_id` 和 `required_rows` 同行锚点。
- PageHit 与精确 UnitHit/MRR/NDCG/recall 分开，去重计分。错误块即使在正确页，也不能算精确块支持。
- `required_rows` 必须在同一符合来源身份的 Markdown 表格行共现；不能把 A 行的词与 B 行的解释散落凑成正确关系。报告 10 个表格 case 与 13 个行锚点元组。
- `context_complete` 要求全部标注单位命中、各单位所需词齐备、所需同行关系全部满足。命中两个单位中的一个仍是部分命中。
- `stage_unit_recall` 与逐单位 `failure_details` 定位 recall/fusion/rerank/context_or_top_k/content/row 支持失败；旧版本缺诊断不伪造为“召回失败”。
- gold 禁止未知字段，拒绝空/超长行锚点；run-id 1–32 字符，case-id ≤64，带 run-id 的 Trace 仍符合 128 字符字段。
- 保存模型/维度/检索参数、gold SHA-256、语料前后指纹。指纹包含源版本、正文/hash、实际向量/hash、检索文本/元信息及邻接；语料发生变化保留报告但 CLI exit 2，不当成同语料 A/B。

**边界**：同行检查只是锚点共现，不是完整表格逐字校对、逻辑蕴含/否定识别或答案事实认证。没有生成答案或调用 judge。gold 来自已审核快照及本轮人工标注，不是独立教研专家双盲金标。

## 3. 冻结集与真实 A/B

语料保持 3 文档/54 chunks（52 个真实1024维非零 leaf向量+2 parent），文档版本仍并列句 v2、名词/动词 v1。名词仅页2/16、动词仅页1；源疑点 block:15 排除保留。

- 原 18 条 gold 不修改：16 个不限定文档/知识点问题 + 2 个 exact 英文 alias scope。
- 新 16 条在实现前冻结：10 表格同行、5 规则/精确块、1 双文档问题，全部无知识点限制。
- 另 2 条跨页探针在首轮 A/B 后、跨页检索前冻结，未据其结果调整策略；两条均 top_k=5，并要求分别返回两个物理页的精确来源及锚点。
- 核心 A/B 为 legacy 与 bigram，每组使用同一 gold、同一完整语料/向量指纹、hybrid/pool30/RRF60、rerank off、query expansion off。接口/SQL/embedding 无 fallback。

| 数据集 / 指标 | legacy | bigram |
|---|---:|---:|
| 原18条：页级/精确单位命中 | 17/18 | 18/18 |
| 原18条：页级 MRR | 0.916667 | 1.000000 |
| 新16条：页级至少一页命中 | 15/16 | 16/16 |
| 新16条：精确单位至少一个命中 | 14/16 | 16/16 |
| 新16条：全部来源/锚点齐备 | 13/16 | 16/16 |
| 新16条：精确单位 MRR | 0.812500 | 0.937500 |
| 新16条：精确单位平均 recall | 0.843750 | 1.000000 |
| 新16条：去重精确单位 NDCG | 0.804688 | 0.948849 |
| 表格同行 case / 元组 | 10/10、13/13 | 10/10、13/13 |
| 2条跨页：至少一个精确单位命中 | 1/2 | 2/2 |
| 2条跨页：全部来源/锚点齐备 | 0/2 | **1/2** |
| 2条跨页：精确单位平均 recall | 0.25 | **0.75** |

新策略 36 条合计有 **35 条标注上下文齐备**。不能只取“36 条都有某个相关来源命中”就称 36/36 完整回答，也不能把 34 条先完成的样本代表全库质量。

表格同行指标两种策略均满分，**没有本轮新增表格准确率收益**；新增的是可反例验证的严格检查能力。错误表/同行互换、重复第一页冒充跨页、冲突资料保留独立 unverified 引用，由模拟单测验证；冲突样本不是新增真实教材，更不证明冲突事实已被自动裁决。

原始引用 canonical hash 全部通过，语料前后及所有 A/B 组指纹一致。v2 首轮评测把“部分单位命中”留作 failure_stage=None 的缺陷已改为 v3 逐单位判断；原始报告保留，以同一实际 citations/diagnostics 离线重算 `.rescored.json`，**没有为重算额外调用供应商**。

## 4. 明确保留的新失败

问题：“or表示选择关系和表示否则时，分别有哪些例句？”

需要并列句物理页2的选择例句，以及页3的“否则”续例。新策略有相关页/块命中，但最终页为 7、2、2、8、5，未带到所需页3续例；精确单位 recall=0.5，context_complete=false。

- 两个正确单位都在 vector/keyword/fusion/ranked 候选中，各阶段单位 recall=1。
- 页2单位融合第2；页3续例融合第 **11**，未进入 top5。当前诊断为 `context_or_top_k`；主要是排序后 top_k 选择不足，不把它描述成解析丢字或 ANN 没召回。
- 不为此单独扩大 top_k、写问题路由、修改 gold 或宣称已解决。下轮应固定此失败，再设计通用排序/跨页上下文选择 A/B；必要时评估可选 rerank 的成本与收益，而不是先引入付费依赖。

另一个跨页问题（基本结构 + for/because 位置）本轮已带齐页1与页4来源，不能替代所有跨页续表/题组验证。

## 5. 真实调用、界面与运行态

本轮 **92 次真实 embedding**，供应商报告输入 tokens **944**；全部成功且 usage_reported。构成：实现前18条复现 + 两策略(18+16)共68条 + 两策略跨页2条共4条 + 工作台实际2条。仅查询向量，不新增/重建语料。

Trace 全局 fallback 配置估算合计 **0.001888**；币种/模型价目表/实际供应商账单未核验，不报人民币/美元实付。没有真实 chat、judge、rerank。

真实 Playwright 通过前端 Nginx→API：清空知识点/资料过滤，原连系动词问题命中动词分类表，advice/suggestion 问题命中正确名词表；部署 mode=bigram、max_terms32、aliases 默认，pageerror=0。截图/认证 state 保持私有，不发起新的索引/资料删除。

主库仍 generation_task3/content_item4/users1、ocr_job4/ocr_page12、knowledge_document3/knowledge_chunk54。主/CI head `rag_ocr_review` 不变，无新增迁移、清库或业务隔离环境。原 API/worker/OCR worker/scheduler/frontend/Redis/Postgres 保持运行。

## 6. 测试与未过门槛

- 新增 **23 非集成 + 2 真实 PG**，最终 **684 passed / 28 integration / 656非集成**，18条既有警告保留。
- 覆盖完整短语/中英符号/二字片段、长问题上下限与采样、旧模式等价、模型/费用不被词法补召回扩大、真实PG参数与外租户/parent门控、第四向量候选融合恢复、来源身份/同行互换/跨页重复、分阶段部分失败、语料/向量指纹、漂移exit2、run-id和未知gold字段。
- 修改7文件 black/isort、修改后端 flake8、4个RAG/评测模块 scoped strict mypy 通过。Compose 透传、真实部署/ready/Nginx/浏览器通过；本轮无前端源码变更。
- 4个限定模块覆盖率 **95.24%**（340/357），不是全应用覆盖率。
- 全 app 精确覆盖率 **79.5116%**（6252/7863），**低于 CI 80%**，当前差39条已存在应用语句的覆盖。全 app + 评测脚本混算为79.8150%，终端四舍五入显示80%也不等于通过。保留原门槛，不降低/排除文件/把脚本加入CI分母冒充达标。
- scoped mypy 不覆盖全app；显式检查 config.py 仍有既有 MODEL_PRICES 泛型、未注解 __init__ 等3处 strict类型问题，未做无关配置/价格语义改写，不宣称整个项目类型检查通过。

## 7. 文件与复跑

核心文件：
- `backend\app\rag\retrieval\keyword.py`
- `backend\app\rag\retrieval\models.py`
- `backend\app\rag\retriever.py`
- `backend\app\config.py`
- `backend\scripts\rag_eval.py`
- `deploy\docker-compose.yml`
- `backend\tests\test_rag_cjk_recall.py`
- `backend\tests\integration\test_rag_postgres.py`

私有证据：`.local-eval\ocr-2026-10-05\recall-ab`。

- `gold-freeze.json`、三套 gold、`corpus-private.json`：冻结与源快照；完整向量指纹 `7f4baa8d0b048b34281a7f238345632cf60dafb5e622cdd0eb7e250c89544cab`。
- `baseline-regression.json`、六组原始 legacy/bigram 报告：真实查询与阶段结果。
- `*.rescored.json`、`final-ab-summary.json`：v3严格完整性判断，原始文件SHA绑定，零额外调用。
- `live-ui-report.json`、`live-ui-*.png`：生产默认 aliases 下的实际无scope界面查询。
- `final-ledger-and-db.json`：本轮92条调用与业务计数。
- `full-regression.log`、`coverage.json`、`coverage-summary.json`：最终测试和覆盖率准确分母。

复跑 CLI 会真实调用 embedding 并写 Trace，先配置现有数据库/供应商环境；从 backend 执行，例如：

```powershell
$env:RAG_CJK_KEYWORD_MODE='bigram'   # legacy用于对照
$env:RAG_QUERY_EXPANSION_MODE='off'
python scripts/rag_eval.py --gold ../.local-eval/ocr-2026-10-05/recall-ab/cross-page-gold.json --output ../.local-eval/ocr-2026-10-05/recall-ab/cross-rerun.json --run-id cross-rerun
```

上面只改变评测进程，不是修改运行服务；永久回退需在现有Compose环境设置 `RAG_CJK_KEYWORD_MODE=legacy` 后重建/重启对应服务，不必重嵌入语料。

## 8. 收尾与后续

| 事实面 | 状态 |
|---|---|
| 本轮代码、模拟/真实PG回归 | changed-and-verified |
| 最新原部署与实际工作台 | changed-and-verified |
| 文档/计划/任务/OPT与项目规则 | changed-and-verified |
| 完整CI80%覆盖率/全app strict类型 | pending，未达标/未整体认证 |
| 外部记忆 | out-of-scope，不读写其作为约束 |
| 工作区 | 原未提交改动及私有现场有意保留；未清场/自动提交/撤除真实资料 |

下一优先项：
1. 跨页例句第11位被top5截断的通用排序/上下文选择对照；保留本轮失败，另建未参与调参的外部测试集。
2. 补应用相关边界回归，真实全量覆盖率达到80%并独立运行CI同口径；不靠四舍五入。
3. 四类格式/复杂续表题组/真实多教材冲突完整集，独立专家事实金标；本轮不等于其完成。
4. 单租户有界批量/非OCR/legacy重建与长期存储治理；真实生成→judge→人工验收质量、规模/GPU/完整恢复演练及分发许可仍待。
