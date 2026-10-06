# RAG 主链路切换、存量迁移与真实生成验收

**日期**：2026-10-06（Asia/Shanghai）
**变更编号**：OPT-073
**范围**：新资料默认结构化索引、关系型上下文主链路、两份存量资料选择性迁移、内置题型来源门禁、6条真实生成/质检/人工核验闭环。

## 1. 结论

**可以把新版 RAG 作为当前项目的主链路使用。** 本轮已完成：

- 新资料默认 `structure` 切分，默认 `relation` 上下文返回；Top-5默认、Top-8可选、总上下文32768字符保持。
- 名词、动词两份旧资料已在原环境中选择性从 legacy v1 升级为 structure v2；并列句structure v3保持不变。
- 三种内置题型（single_choice / cloze / reading）升级为来源必需：无有效来源时生成任务失败关闭；有来源时保留逐段 provenance，并需要人工确认实际引用后才能通过。
- 真实6条正向内容（单选2、完形2、阅读2）完成生成与自动质检，均进入 `pending_qc`，没有自动通过或发布；无来源反例在调用chat前失败。

**尚未完成的不是“旧链路替换”，而是生产质量闭环继续加强**：真实生成样本很小，且发现一份完形存在“选项/答案质量需人工复核”的实际问题；自动QC分数不能替代人工事实与题目质量核验。当前适合个人小批量、人工复核后生产，不适合无人值守批量发布。

## 2. 主链路切换

### 2.1 运行配置

| 配置 | OPT-072 | OPT-073实际运行 |
|---|---|---|
| 新入库默认切分 | legacy | **structure** |
| 检索上下文 | relation实现但默认配置曾保守 | **relation** |
| 默认返回 | Top-5 | Top-5 |
| 可选返回 | Top-8 | Top-8 |
| RAG字符预算 | 32768 | 32768 |
| 候选池 / RRF | 30 / 60 | 不变 |
| 全局 `RAG_MODE` | optional | optional；内置题型模板显式 `required` |
| 旧布局 | 默认 | **显式legacy回退保留** |

backend、worker、OCR worker、scheduler实测参数一致：`structure / relation / Top-5 / 32768`。无新增迁移，main / CI head仍为 `rag_str34_boundary`。刷新服务后API catalog返回的布局与上下文模式为当前配置；原生预览默认显示structure，仍可手动选择legacy。

API未传 `top_k` 时请求期读取当前配置，不冻结旧默认；生成工作流未指定Top-K时沿用Top-5。RAG仍按来源、租户、活动索引、segment、member/hop/字符/字节预算约束，结构化source envelope不能代替实际来源段。

### 2.2 旧索引兼容与回退

默认值切换不会在读取旧OCR任务时擅自重建：已有文档打开审核时优先使用文档/任务保存的layout；历史meta缺少layout时按旧索引兼容为legacy。显式切换legacy仍可预览/重建，失败保留旧索引。

前端知识库和OCR工作台的布局选择与计划hash绑定；改变布局后必须重新预览并重新核对来源/告警/费用授权，不能只改一个环境变量就直接付费重建。

## 3. 两份存量资料的选择性迁移

复用原Docker、主库和OCR审核状态；未重新OCR，未扩大选页范围，未清库。

| 资料 | document ID | 迁移 | 迁移后 | 保留约束 |
|---|---|---|---|---|
| 第二章 名词 | `dd8142b1-1dc6-5de4-8006-95f893af343f` | v1→v2 | structure，23 leaf，0 parent | 仍仅第2、16页；`block:15`继续排除 |
| 第一章 动词 | `f9f85aae-e7ab-5f8b-bb9c-9f7f77533174` | v1→v2 | structure，4 leaf，0 parent | 仍仅第1页 |
| 第二章 并列句 | `fe5db498-4cd5-595b-af6e-af0943325619` | 本轮不动 | structure v3，54 leaf，1 parent | 原8页、原试点和已知评测状态保持 |

迁移后主库为 **3资料 /82 chunks**，其中 **81个非零1024维leaf +1 parent**。所有来源hash/content hash保持；名词排除块未进入活动source segments；原业务 task3 / content4 / user1、OCR job4 / page12 / boundary job2不变。迁移没有产生新OCR任务。

每份迁移均在网页中显式选择structure、沿用已有排除和accepted/rejected结构决定、确认费用后执行，同document ID原子升级。私有报告记录了计划、审批、版本和页范围；网页无pageerror。

## 4. 内置题型和来源门禁

### 4.1 模板策略

single_choice、cloze、reading模板升级到version 2，均增加：

```yaml
rag:
  mode: required
  require_human_verification: true
```

这意味着：

1. 有知识点但检索无有效来源时，生成在chat前失败，任务 item以 `RAG_CONTEXT_REQUIRED` 关闭；本轮无来源反例验证了这一点，chat调用数为0。
2. 有效检索命中时，将relation上下文和真实citation注入生成prompt；生成Trace保存参数和RAG provenance。
3. 内容落库后状态先为 `pending_qc`，即使自动QC分数超过70也不能直接当作已发布。
4. 人工通过前必须勾选“我已核对上述来源与题目、答案及解析”；前端未勾选时通过按钮禁用，后端未勾选时返回409。
5. pending_qc内容直接发布返回409。整个验收没有提交人工通过、没有发布任何本轮样本。

### 4.2 引用和人工页面

质检页改为按真实 `segment_id`渲染引用，显示资料、页码、block ID、chunk ID和原文片段，不再以重复chunk ID作为列表key。人工页面对6条内容检查了实际来源段：单选/阅读各20段，完形各8段；segment ID均唯一，页/block信息存在。

前端内容预览去除模型输出中已经带有的`A.`/`B.`等选项前缀后再显示界面标签，**只改展示，不改payload**。本轮真实检查发现完形一条样本的正确项全部集中在A，且若干连词选项存在人工需要复核的语义问题；它们仍保持pending_qc，未发布。

## 5. 真实生成与质检验收

### 5.1 运行批次

| 批次 | 题型 | 数量 | 结果 | 自动QC分 |
|---|---|---:|---|---|
| 无来源反例 | single_choice | 1 | **失败关闭，chat=0** | 无 |
| 名词单选 | single_choice | 2 | 成功，pending_qc | 91.35 / 88.50 |
| 并列句完形 | cloze | 2 | 成功，pending_qc | 88.95 / 91.28 |
| 名词阅读 | reading | 2 | 成功，pending_qc | 87.70 / 91.75 |

正向批次总共6条内容；worker任务均成功，item均1次完成。一次阅读生成的JSON首尝试解析失败，结构化输出机制记录失败Trace并重试，随后成功；这不是静默接受坏JSON。

生成Trace确认：每条正向内容的generate输入包含实际RAG上下文，citation内容可以在注入上下文中找到；无来源反例没有generate/chat Trace。QC prompt只接收声明的生成约束和payload，不把RAG上下文、密钥或旧稿混入评分目标。

### 5.2 质量结论

自动QC分数均高于70，只说明自动评分链路完成，不能证明：

- 选项干扰项一定同质、唯一；
- 完形每个空不存在另一种可接受答案；
- 阅读题所有答案与原文完全一致；
- 生成内容没有复述来源错误；
- 批量规模下成本、延迟和失败率稳定。

本轮开发复核明确发现完形样本需要人工退回/修改的风险，因此不执行自动发布。这个结果正说明来源必需+人工核验门禁正在发挥作用，而不是说明生成质量已经无人值守达标。

## 6. 工程与运行验证

- 后端完整门禁：**911 passed**，其中本轮新增主链路单测13、主链路PG测试3，以及既有回归适配；最终app覆盖率 **82.34%**，超过80%门槛，18条既有warnings。
- 本轮新增测试覆盖：默认structure/relation、显式legacy回退、原生预览配置目录、旧OCR索引读取兼容、默认OCR审核计划、同ID选择性迁移rollback、source-required生成、租户/来源段/人工引用门禁。
- 12个选定Python文件 black/isort/flake8通过；2个源文件scoped strict mypy（`--follow-imports=silent`）通过；前端tsc/Vite通过。不宣称全app strict或远端CI全链路通过。
- 实际Docker服务：backend、worker、OCR worker、scheduler、frontend、Postgres、Redis均运行；API readiness及前端页面已验证。迁移后真实Top-5/Top-8 44条source gold均 `context_complete=1.0`；这是来源支持指标，不是答案正确率。
- 迁移后source gold语料指纹为 `4d437bf331330833b327e3549a15aa7435826cf94f49278bed05a688c85ca794`；gold hash仍为 `897863f543c0e05017afdb9346639462b413a25206e4fee1880e60544e497fcc`。迁移改变了leaf/向量，不能与迁移前做单因果同向量A/B。

## 7. 本轮真实调用、成本和证据

从本轮开始时间起，Trace ledger包含：

- **99次embedding**：索引迁移/回归检索；
- **7次generate**：6次成功内容加1次JSON解析失败重试；
- **12次qc**：6条内容，每条2轮自动质检；
- 合计118条Trace，reported prompt tokens **47938**、completion tokens **37200**，配置成本估算 **0.170276**；全部usage reported，存在1次失败generate尝试，所以不能把全部记录称success；供应商实际账单和币种未核验。
- 无真实rerank、Late forward或LLM query-context调用；无新OCR识别、无新增迁移。

注意：本轮正向生成使用了真实chat/自动QC，因此与前一轮只有embedding的验收不同；但没有人工确认答案正确，也没有发布内容。

私有证据保留在 `.local-eval\str58\`：主/CI dump、before/after状态、迁移计划/审批/报告、Top-5/Top-8回归、6条内容私有快照、Trace ledger、人工核验截图、全量gate、网页默认预览及部署状态。教材原文、凭据、登录态未写入公开文档。

## 8. 当前可用范围与下一步

**现在可以：**

- 新资料直接按structure入库；
- 使用relation进行Top-5/Top-8检索；
- 以`required`模板做小批量生成；
- 逐条人工核对来源、题干、选项、答案、解析后再通过/发布。

**现在不要：**

- 不要把自动QC分数当作无人值守发布许可；
- 不要把44/44 source gold当作事实正确率；
- 不要一次提交大批量50题并直接发布；
- 不要因默认已切换就删除legacy代码或历史索引回退路径；
- 不要把Late/LLM-context实验当作生产链路能力。

**下一步优先级：**

1. 先补一轮人工标注：逐条决定6条样本通过/驳回/修改，并把完形问题作为质量规则或结构化校验的候选回归。
2. 扩展真实生成金标到每种题型至少20～30条，统计人工驳回率、来源一致性、答案唯一性、选项质量和成本/延迟。
3. 在有足够人工样本后，再考虑将部分模板的`required`与自动QC阈值细分；不建议现在关闭人工核验。
4. 继续补Late真实模型、LLM context、跨页table/cell专家集和长上下文噪声实验。

## 9. 收尾状态

| 事实面 | 状态 |
|---|---|
| 代码 | changed-and-verified |
| 运行态 | verified-current / deployed / live checked |
| 文档与规则 | pending OPT-073同步后 changed-and-verified |
| 外部记忆 | out-of-scope，未写入 |
| 工作区 | pending，大量未提交改动和私有验收证据保留；不清场 |
