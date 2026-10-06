# RAG STR-5 / STR-6 落地与验收

> 本文是OPT-071阶段验收，44→43/默认Top-3/8192字符为当时证据，未改写历史。2026-10-06 OPT-072按用户要求改为默认Top-5/可选8、32768字符，同源/向量两档44/44齐备；不是排名算法修复，真实Late/LLM效果仍待。现役报告：`docs\现状文档\RAG-Top5-Top8与上下文预算验收.md`。

**日期**：2026-10-06（Asia/Shanghai）
**变更编号**：OPT-071
**方案依据**：`docs\现状文档\RAG跨边界知识组织与检索优化方案.md`
**范围**：结构感知索引切分、选择性重建、语义 / 上下文 / Late Chunking 独立实验；不改变生成质量通过口径。

## 1. 交付结论与不能混淆的边界

| 项目 | 本轮状态 | 证据 / 边界 |
|---|---|---|
| STR-5 结构化 leaf / parent、逐段来源、预算及覆盖校验 | 已实现并部署 | 原生上传 / 免费预览和 OCR 审核 / 重建入口均可显式选择 layout |
| STR-5 真实选择性重建 | 已执行 | 并列句同 document ID v2→v3；其他资料未重嵌入；旧源/hash不变 |
| STR-5 冻结来源支持回归 | **存在 1 条回归，不作为默认优化验收通过** | 44/44→43/44；正确向量候选排名1，但 RRF 排名4，未进 top-3 |
| STR-6 独立实验、语义切分、确定性 / 可选 LLM 上下文、Late 适配器 | 已实现 | 默认 dry-run；实验不写知识索引；真实云 embedding 另需显式参数 |
| STR-6 真实 structure / semantic / 确定性 contextual 小实验 | 已执行 | 同自编英文 prose / 3探针，全部3/3来源支持，**没有测得质量增益** |
| STR-6 真实 LLM contextual | 待验证 | 仅严格抽取 / 超时 / Trace / mock测试，本轮未调用真实 chat |
| STR-6 真实 Late Chunking | 待验证 | token-pooling / local-only适配器已测试；未配置或运行真实本地token模型，dry-run不是效果验证 |
| 批量教材与生成事实正确性 | 未认证 | 不是四类每类10条的完整新金标，不是专家事实评测，也不是全库负载 / 长期稳定性证明 |

**发布决策**：配置 `RAG_CHUNK_LAYOUT=legacy` 保持默认；structure 为免费预览、原生上传及 OCR 审核中的显式选项。一个 v3 试点保持活动，保留已知排序回归，不全库推广。切换上下文模式不等于恢复旧 leaf 索引；索引布局回滚仍需显式计划、费用确认及重建。

## 2. STR-5 如何实现跨边界切块

### 2.1 页只是定位，不再作为硬切分依据

新 `backend/app/rag/chunking/structural.py` 使用已接受的文档结构关系建立逻辑正文组，再在字符 / embedding 输入预算内生成 leaf。版本为 `rag-chunker-structure-v1`；legacy 路径不变。

- 跨页自然续文与被确认的跨段关系可进入同一 leaf，分页本身不再触发强制 flush。
- 新章节、缺页 / 不连续选页、审核排除、独立题目 / 答案、表格和代码边界仍受保护；不把“同术语”当作自动合并证据。
- 表格 / 代码是独立保护单元，正文与表格、跨章节证据继续由 STR-2～4 的关系层联合返回，不把整章 / 多表无区别地串成一个向量。
- 表格复用保护行 splitter：重复表头仅作为上下文，正常行整体保留。极端超长行 / 单元预算不足仍可能拆分，并输出 warning；不能宣称任何大小表格都绝不切断。
- 页眉页脚与纯导航标题保留于不可变原文，但不纳入本轮可嵌入正文覆盖分母；诊断明确列出 `source_coverage_scope` 和 `excluded_navigation_blocks`。100%覆盖指可嵌入正文，不是所有原文字符都成为向量。

### 2.2 非连续正文必须逐片段定位

每个 leaf 的 `source_segments` 记录原始规范化文本中的 block/page、`source_start/end`、leaf 内 `content_start/end`、`content_hash`、locator与role。表头复现、页眉排除等可能使 leaf 正文不连续，因此：

- 数据库 / chunk meta 顶层 `content_start/end=None`，不伪造一个连续正文跨度。
- `source_envelope` 只供导航，并标记 `not_a_contiguous_body_span=true`；不能拿包络读出正文作为引用。
- 校验逐段文字、hash、范围、重复归属、未映射非空白正文、正文覆盖与输入预算；拼接空白不是原文事实。
- 父块保留段映射并去除子块 overlap，不从页眉或包络重新读回排除内容。
- retrieval 的 relation 模式只从同 tenant / document 的**活动 leaf 实际映射**加载正文；源结构过期则保留精确 seed 引用并停止关系扩展。
- 候选诊断中的 block/page身份不代表其正文已交付。最终评分继续检查实际返回 segments，而不是候选列表或 parent membership。

### 2.3 配置 / 审核 / 重建链路

`layout` 从原生文本 / 文件上传、免费预览、OCR计划、审批与 worker 一路传递。计划 hash绑定 layout / 配置 / 结构 / embedding输入签名；审批前预览须同步，执行前再次校验，失效计划在付费调用前拒绝。OCR单文档原子交换复用原机制：同ID增加 revision，失败 / 无效来源保旧，维度和模型兼容条件不放宽。

前端知识库与 OCR 工作台显示布局选择、当前布局及来源段诊断。切换布局不会自动触发全库重建。

## 3. 真实选择性重建与运行态

复用既有 Docker / 主库 / CI PostgreSQL，不新建环境、不清库。开始前主库与 CI 库 dump及语料快照保留在忽略目录。

| 资料 | revision / layout | 本轮活动索引 | 源范围 |
|---|---|---|---|
| 并列句 `fe5db498-4cd5-595b-af6e-af0943325619` | v2 legacy→v3 structure | **54非零1024维leaf +1parent，2个跨页leaf** | 完整8页，原文与内容hash不变 |
| 名词 `dd8142b1-1dc6-5de4-8006-95f893af343f` | v1 / legacy未改 | 12leaf | 仅选页2/16；`block:15`审核排除保留 |
| 动词 `f9f85aae-e7ab-5f8b-bb9c-9f7f77533174` | v1 / legacy未改 | 4leaf | 仅选页1 |

主库当前 **3资料 /71个chunk =70个非零1024维leaf向量 +1parent**。并列句原为36leaf+2parent；变为54leaf源于保护独立单元等结构变化，不是向量数量下降或压缩收益。

迁移 head主库 / CI均为 `rag_str34_boundary`；本轮没有新增迁移。原业务 task3 / content4 / user1、OCR job4 / page12 / boundary job2不变，没有新OCR识别或局部复核任务。

真实浏览器完成显式计划 / 费用确认及同ID重建。随后仅规范化55个新chunk的来源包络meta，**没有额外重嵌入**；详情见私有 `source-envelope-normalization.json`。服务已构建并部署；网页确认v3恢复、原生structure免费预览、跨页多段查询、已知排序失败、名词排除保持，未见pageerror。

## 4. 固定金标前后对照：不能隐藏的 44→43

### 4.1 固定的是原始来源相关性，不是易变的 chunk ID

在真实索引交换**之前**，把既有36回归+8独立来源题中的旧chunk-ID相关性映射到不可变物理 `source_block_ids`。query、pages、terms和原相关性不变；原gold保留。`rag_eval.py` v6 支持这个字段，冻结hash：

`897863f543c0e05017afdb9346639462b413a25206e4fee1880e60544e497fcc`

`gold-translation.json` / `gold-freeze.json` 记录翻译和时点。此次 leaf、embedding输入、数量与实际向量均改变，不属于STR-1/2那种“同leaf向量只切换context”的单因果A/B。旧独立8题本轮为回归，不再次称新盲测。

### 4.2 结果与失败定位

| 指标 | 重建前 | 重建后 |
|---|---:|---:|
| 冻结44题的标注来源上下文齐备 | 44/44 | **43/44** |
| 未完整案例 | 0 | `holdout-or-consequence` |
| 并列句leaf / parent | 36 /2 | 54 /1 |

问题：**“or表示否则时有哪些例句？”**

正确新 leaf `e90f19e3-6fd7-4d08-9b13-b609d063e837` 含原 `block:31` 的规则和 Hurry up 例句，向量排名 **1**、关键词排名 **13**、RRF融合排名 **4**，默认top-3未交付。正文覆盖没有丢，ANN也已召回；这是切块变化后的融合排序 / top_k竞争，准确失败阶段为 `context_or_top_k`。

初版诊断评分未识别复合leaf的来源身份，误归因为召回。新增候选诊断的 `source_block_ids/pages` 后，仅离线补齐 / 重评分同一批实际结果，**零新增模型调用**。原报告和rescored报告均保留，最终齐备率仍43/44，没有改top_k / 阈值 / gold来凑100%。

## 5. STR-6 实验实现与实际结果

### 5.1 统一独立入口

`backend/scripts/rag_experiment.py` 支持 `structure` / `semantic` / `contextual` / `late`，输入为 canonical ParsedDocument JSON，或明确 document / tenant scope。默认仅准备报告；云embedding真实执行须同时 `--run --paid-embedding`；模型上下文还须独立 `--allow-llm-context`。

实验只写报告及调用Trace，不写 KnowledgeDocument / KnowledgeChunk或活动版本。不改变生产检索策略，不安装 / 下载模型，不启用全局chat。

配置上限：源4MiB、chunk128、句子128、语义相邻余弦阈值0.65、上下文源4096字符、选配LLM最多8调用、Late最多8192tokens。所有值走config / 环境变量及Compose透传。报告绑定模型 / 配置 / 源 / 金标 / 输入hash、耗时与批次；source-block / required-term支持分数不是最终回答正确率。

### 5.2 Semantic：结构优先、句向量只是边界建议

只有无歧义的单prose块使用真实句向量相邻余弦寻找边界，再受结构 / 字符 / token预算约束。明确多块知识单元、标题、题组、表格、代码不重排。句数上限在provider调用前检查；数量 / 维度 / 非零 / finite、覆盖及超预算均有失败测试。不是通用语义理解，也不能只凭一个阈值保证英语缩写等所有句边界正确。

### 5.3 Contextual：派生说明不能冒充源

默认确定性来源名 + 结构面包屑，prefix与body、hash分开。选配LLM用独立 `rag-context-experiment-system/user.st`，严格Schema，只接受在源中确实存在的抽取片段；任意新增解释 / 事实不通过。限流、超时、Trace与失败关闭有mock验证。literal extractive是来源约束，不是专家事实认证；本轮未执行真实LLM。

### 5.4 Late：真正的token上下文后pool，但还未跑真实模型

`LocalTokenEncoder` 只接受显式本地模型目录，延迟导入可选transformers / torch；`local_files_only=True`、`trust_remote_code=False`、safetensors-only及fast tokenizer。模型 / tokenizer文件hash、目录约束和模型token上限均检查，禁止自动下载与静默截断。

先对全文进行transformer forward，再按leaf精确源段选择token hidden states，mean pooling + normalization；查询也使用同一本地encoder。校验全文token-offset覆盖、attention/special tokens、维度、finite与非零pool结果。只接受真正token状态，不能把云API的最终向量平均一下冒充Late Chunking。

本地forward独立Trace stage为 `rag_late_local`，物理计算不报价、不冒充云账单。本轮token-pool数学 / offset / local-only模型适配器用fixture和mock测试，CLI仅dry-run；**无真实权重forward、无Late真实质量分数**。非token接口和超长输入失败关闭，尚未实现长文滑窗Late推理。

### 5.5 真实小样本结果（自编prose，不是教材 corpus）

| 变体 | 实际chunk | 额外句向量输入 | provider批次（含查询） | 3探针来源支持 |
|---|---:|---:|---:|---:|
| structure | 2 | 0 | 2 | 3/3 |
| semantic | 4 | 8 | 3 | 3/3 |
| 确定性contextual | 2 | 0 | 2 | 3/3 |
| late | 未执行 | 不适用 | 0 | 无分数，仅dry-run |

三种云embedding实验使用同一自编英文prose源和固定探针。没有测得改善；语义方法增加了调用。不得将这3题与44教材题合并宣传效果，更不能把Late准备成功算真实效果通过。生产默认保持不变。

## 6. 工程验证、成本与可复现入口

- 后端完整app覆盖率门禁：**871通过**，含**39真实PostgreSQL集成 /832非集成**；本轮新增57单元+3PG=60测试。
- `--cov=app --cov-fail-under=80`：**7883/9579=82.2946%**，exit0。script不加入app分母，不用scoped覆盖率代替全app；18条既有warnings。是本机测试门禁，不是远端CI全链路成功。
- black / isort / flake8检查26个选定源 / 测试文件通过；18个源文件 scoped strict mypy（依赖跟随静默）通过，**不代表全app strict通过**。展开依赖的strict检查仍报185处 /25文件，作为类型债保留，不用隐藏配置宣称全库无错。
- 前端 `npm run build`（tsc + Vite）通过；最终后端/worker/OCRworker/scheduler构建部署，API readiness=200/ready=true、前端=200，结构化免费预览再次验证通过；主/CI迁移head及原业务计数不变，host/container结构切分与实验脚本hash一致。实际Playwright复核无pageerror。没有重新测试真实生成 / judge事实质量或完整全格式压力。
- 本轮Trace核对 **103次真实云embedding /5532 reported prompt tokens**，都成功 /usage reported；配置fallback估算 **0.011064**，供应商币种 / 实付账单未核验。baseline44 + after44 + 重建6批 + 实验7批 + 最终查询2 =103；无真实chat /judge /rerank，无新OCR。

### 免费准备与显式执行

以下在 `backend` 执行；示例全部本地绝对路径，凭据只使用已有环境，不贴入文档。

```powershell
# 默认 dry-run：零模型调用，不写知识索引
.\.venv\Scripts\python.exe scripts/rag_experiment.py `
  --input .local-eval\str56\experiment-source.json `
  --variant semantic `
  --gold .local-eval\str56\experiment-gold.json `
  --output .local-eval\str56\next-semantic-dry.json
# 真云embedding：在上面命令追加 --run --paid-embedding（明确费用动作）
# contextual只有另加 --allow-llm-context才调用chat；不建议未评测就开全局功能
# late真实运行必须另给 --late-model-dir 显式本地token模型目录；本轮未提供真实模型
```

实际全量门禁复用CI PostgreSQL，`TEST_DATABASE_URL`由本地私有helper读取原容器配置，不在公共文档展开凭据。测试命令本质为：

```powershell
.\.venv\Scripts\python.exe -m pytest -q -o addopts= `
  --cov=app --cov-fail-under=80 --cov-report=term-missing
```

主要源码：`backend/app/rag/chunking/{models,structural,strategy,parents}.py`、`backend/app/rag/{indexer,preview}.py`、`backend/app/rag/retrieval/{relation,context,models}.py`、`backend/app/rag/structure/runtime.py`、`backend/app/rag/experiments/`、`backend/scripts/{rag_experiment,rag_eval}.py`、`backend/app/services/{knowledge_service,ocr_review,ocr_service}.py`、API/schema/OCRworker及前端知识库 / OCR工作台。测试见 `backend/tests/test_rag_str56.py`、`backend/tests/integration/test_rag_str56_postgres.py`。

## 7. 后续优先级与知识收尾

1. 先独立修复 / 比较 `holdout-or-consequence` 的融合排序与top_k竞争，固定gold和预算再验收；不要把改变gold当修复。
2. 准备明确支持token hidden states / offset的本地模型，再执行Late真实效果 / 延迟 / 长文能力实验；模型不合适时继续fail-closed，而不是伪造结果。
3. 如需LLM上下文，独立授权真实chat并检验来源约束 / 供应商响应 / 成本；确定性prefix没有真实增益前不默认重嵌入。
4. 完整四类正反例、真实跨页表格 / 合并cell、新教材专家金标、负载恢复与真实生成事实质量另验收。

| 收尾事实面 | 状态 |
|---|---|
| 代码 | changed-and-verified：已实现，工程测试通过；质量边界如上 |
| 运行态 | verified-current：原环境服务部署 / 网页复核；保留一个已知回归试点 |
| 文档 / 规则 | changed-and-verified：OPT-071 / tasks / 现状 / README /架构及LLM规则同步 |
| 外部记忆 | out-of-scope：仓库为权威，未写外部记忆 |
| 工作区 | pending：继承大量未提交改动和私有验收证据；不自动提交、清场、删除或回滚 |

证据在忽略目录 `.local-eval\str56\`：主 /CI库dump、源快照、两版审核计划、gold翻译冻结、before/after原报告与rescored报告、comparison-summary、3真实实验与Late preflight、重建 / 最终 / 原生预览网页报告、全量覆盖率门禁、静态检查与依赖展开类型债、closeout-runtime、最终DB及费用ledger。教材正文、登录态与截图不公开提交；本报告只记录必要ID /统计，不复制原教材全文。
