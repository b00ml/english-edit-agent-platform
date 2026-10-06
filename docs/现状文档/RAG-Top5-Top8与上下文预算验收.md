# RAG Top-5 / Top-8 与上下文预算验收

**日期**：2026-10-06（Asia/Shanghai）
**阶段报告**：OPT-072（历史）
**现役报告**：OPT-073 `docs\现状文档\RAG-主链路切换与真实生成验收.md`
**授权依据**：用户要求召回使用top5或top8，并指出8192字符的应用侧上下文预算过小；本轮明确扩大返回预算，不假称原top3排名算法修复。

## 1. OPT-072阶段配置与交付（OPT-073已将默认布局切换为structure/relation）

| 项目 | 原配置 | 本轮最终配置 |
|---|---:|---:|
| 应用默认返回上限 `RAG_TOP_K` | 3 | **5** |
| 知识库网页可选 | 无显式数量控件 | **Top-5 / Top-8**；可使用服务端默认 |
| RAG总上下文字符上限 `RAG_CONTEXT_MAX_CHARS` | 8192 | **32768（4倍）** |
| 每个查询lane的候选池 | 30 | 30，未缩减 |
| RRF / rerank / scope | 原策略 | 未改权重、阈值、算法或范围；rerank仍off |
| 可选 `RAG_CONTEXT_TOKEN_LIMIT` | None | None；如显式设置仍执行保守UTF-8字节上限 |
| 新资料切分布局默认 | legacy | legacy；既有一个structure试点保留 |

**Top-K 是上下文包的返回上限，不是保证数量**。关联扩展、重复来源去重、scope、总字符 / 字节 / 来源段数等约束仍可使实际返回更少；逐包多个source segments不应误算为额外独立命中。物理页仍不是知识单位。

已同步Python配置、根 / 后端env示例、Compose默认及本机私有`.env`，backend / generation worker / OCRworker / scheduler使用同源设置，原Docker环境重建部署。网页数量选项与诊断展示请求Top-K、候选池和实际预算；刷新仍恢复服务端默认5。

API未传top_k时在请求时取当前配置，避免`Query(settings.RAG_TOP_K)`在路由导入时冻结旧值；显式API仍支持1～10，网页主选项5 /8。生成前`build_rag_context`未指定Top-K也继承配置5，无单独隐藏3或8192截断。

## 2. 模型窗口与应用侧预算不是一回事

当前默认及lite / standard / high模型档案均配置`deepseek-v4-flash`。本轮没有改模型档案、模型名、API凭据或调用路由。

2026-10-06读取DeepSeek官方模型表，标示Flash上下文长度1M **tokens**；其模型名说明推荐`deepseek-flash`，旧`deepseek-v4-flash`名称仍被接受。本项目可能经兼容转发端点调用，官方窗口不等于本项目已实际验证一次1M请求，本轮没有真实chat /judge调用。官方核对记录在私有证据中保留；参考地址：

```text
https://api-docs.deepseek.com/quick_start/pricing/
```

32768是本系统选出的**RAG字符预算**，并非模型硬窗口，也不是32768 tokenizer实测tokens。系统提示、出题技能、参数、高质量样本、历史状态及输出还占模型窗口，因此不把模型1M全用于知识资料。此次32K是按用户要求放宽的初始配置，不是效果最优值；更大预算可用环境变量继续调整。

现有`RAG_CONTEXT_TOKEN_LIMIT`虽含token字样，实现是UTF-8字节保守预算，不是真实tokenizer计数；本轮默认继续不设置。示例改为明确的可选131072字节上限，避免读者把旧8192示例当成必须启用的隐藏第二道限制。字符上限提高不关闭显式字节保护、member /hop /segment限制、原来源校验和访问隔离。

## 3. 同语料 / 同向量真实对照

金标原文件不改，查询 /来源相关性 /required terms /pages均保留；原文件41题自带top3、3题自带top5，不冒充原来全部相同预算。新增`rag_eval.py` v7的`--top-k`显式覆盖运行预算，报告同时保留原case、`effective_top_k`、`top_k_override`及原gold字节hash；评分也按实际运行预算而不是旧case.top_k截断。

冻结gold SHA256：

`897863f543c0e05017afdb9346639462b413a25206e4fee1880e60544e497fcc`

语料 /向量指纹三档及最终32K复测均相同：

`c1191bcbab52dc7edd64d4c142048357a5a8299ef75bc26f8f37ce6c4b0705a7`

| 真实运行配置 | 标注来源上下文齐备 | 实际包范围 | 平均完整渲染字符 | 最大完整渲染字符 |
|---|---:|---:|---:|---:|
| 原金标预算（41×top3 +3×top5），8192字符 | 43/44 | 3～5 | 1090.11 | 2212 |
| 全部显式top3，8192字符 | **43/44** | 3 | 1067.14 | 1903 |
| 全部显式top5，8192字符 | **44/44** | 3～5 | 1761.09 | 3008 |
| 全部显式top8，8192字符 | **44/44** | 3～8 | 2791.68 | 4230 |
| 最终top5，32768字符 | **44/44** | 3～5 | 1761.09 | 3008 |
| 最终top8，32768字符 | **44/44** | 3～8 | 2791.68 | 4230 |

同case的query规划、各lane候选IDs、融合IDs /rank /score逐项一致；没有重嵌入语料、偷改scope、gold、排名权重或阈值。此次是用户授权的**返回预算对照**，不是同预算排名优化。

### 3.1 之前那条失败现在如何得到证据

问题“or表示否则时有哪些例句？”的正确leaf仍为`e90f19e3-6fd7-4d08-9b13-b609d063e837`，含原block:31及Hurry up例句。其vector rank1 /keyword13 /RRF4均未变。

- top3仍不能带齐该标注来源，原失败可复现。
- top5和top8已实际返回正确来源段，不能用候选命中或父块声明代替此证明。
- 网页真实默认请求（省略top_k）返回5包 /10段 /1593字符；切top8返回8包 /19段 /2835字符，两者均带Hurry up例句，无fallback /pageerror，刷新恢复默认5。

### 3.2 不能从这次结果推断什么

top8比top5平均完整渲染上下文约多58.5%，在这44题没有额外来源齐备收益，因此默认5、需要广覆盖时选8。没有真实generation /judge调用，不把字符增量当作精确token、实付费用或生成质量变化。

本集最大只有4230字符，**没有触发原8192字符上限**；最终32K回归保持同分，并不证明扩大字符预算带来质量提升。超过8192的真实预算执行路径用长正文单测 /真PG另验。44/44也只代表这份有限标注来源支持，不是全库召回保证、精确率 /噪声评测、专家事实正确率或批量内容生产认证。

## 4. 工程验证与部署

- 最新完整后端 **895通过**：**853非集成 /42真实PostgreSQL集成**；本轮新增 **21单元 +3PG =24**。原39PG升级到42，复用现有CI PostgreSQL并实际Alembic head核对，测试事务只回滚自有资料，不清库。
- `--cov=app --cov-fail-under=80`：**7884 /9579 =82.3050%**，exit0；18条既有warnings。先前891通过 /8192预算的中间gate保留为历史，不覆盖证据。
- 新增测试验证默认5 /显式8、API动态默认、tenant隔离、候选30不降、32768确实容纳超过8192的正文、旧小预算和可选字节预算仍有效、CLI不改gold /按有效预算评分 /费用前参数校验。
- black /isort /flake8检查8个本轮选定文件通过；4源文件scoped strict mypy（`--follow-imports=silent`）通过，不宣称全app strict或远端CI全部通过。
- 前端tsc /Vite构建通过；原环境实际网页Top-K切换 /默认API参数省略 /reload与诊断预算验证通过，无pageerror。backend /worker /OCRworker /scheduler /frontend均已部署，最终readiness=200/ready=true、前端=200，主/CI head与参数核对一致，部署和本地config/retriever/evaluator源码hash一致。
- 主库仍3资料 /71chunk=70非零1024维leaf向量 +1parent；并列句v3 /structure保持、名词与动词v1 /legacy未改。原task3 /content4 /user1、OCRjob4 /page12 /boundaryjob2不变，main /CI head仍`rag_str34_boundary`，无新迁移 /OCR /重建。

## 5. 本轮真实调用与复现

**268次真实云embedding /3262 reported prompt tokens**，均成功且usage reported；fallback配置估算 **0.006524**，币种 /供应商实付账单未核验，不称实际付款。264次来自上述6轮×44题，另4次为两个预算阶段的网页查询。仅embedding stage，无真实chat /judge /rerank、本地Late forward或语料embedding重建。

单纯提高Top-K和RAG字符预算不会增加同一次query的embedding输入数量；此次调用数增加来自显式验证重复运行。生产生成中更长prompt的费用 /延迟需要未来真实模型调用另测。

在 `backend`，使用原provider /DB环境：

```powershell
# 原gold不动，用显式预算控制本次评测（会真实调用embedding）
.\.venv\Scripts\python.exe scripts/rag_eval.py `
  --gold .local-eval\str56\source-gold.json `
  --top-k 5 --run-id next-top5 `
  --output .local-eval\str57\next-top5.json
# 改为 --top-k 8 可复现另一档；不传时仍按原gold逐题预算，非自动覆盖gold
```

本轮源码：`backend/app/{config,api/routes}.py`、`backend/app/services/knowledge_service.py`、`backend/app/rag/retriever.py`、`backend/scripts/rag_eval.py`、`frontend/src/pages/KnowledgeBasePage.tsx`、Compose/env示例。新增测试：`backend/tests/test_rag_return_budget.py`与`backend/tests/integration/test_rag_return_budget_postgres.py`；旧中文召回fixture显式top3，以保留其原budget回归语义，不把扩大默认值冒充旧算法改善。

## 6. 收尾与遗留

- 代码 /部署 /文档 /规则：changed-and-verified，本报告为OPT-072现役入口。
- 外部记忆：out-of-scope，未写入；工作区：既有大量未提交变更及忽略的验收现场保留，不自动提交 /删除 /清库 /回滚。
- 未完成：Late真实本地模型 /LLM-context真实chat、四类完整新教材专家金标、真实跨页table /cell事实核验、生成事实正确性 /长上下文噪声 /压力与长期运行质量。可用token模型缓存做下一阶段显式实验，但本轮只核查本机缓存，没有加载 /安装 /下载模型。
- 排序限制未消失：top3仍有这一来源缺口；top5 /8补回结果不等于RRF质量提高。新问句 /新资料和更大窗口仍须独立验证。

私有证据：`.local-eval\str57\`，含原state /历史预算报告、统一3 /5 /8报告、最终32K两档报告及comparisons、原 /32K网页报告截图、两轮完整gate /coverage、静态检查、官方模型窗口及本地profile核对、最终主库计数 /Trace ledger和runtime核查。原gold与先前STR-5/6报告保留，不公开上传教材全文、密钥或登录态。
