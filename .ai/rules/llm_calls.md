# LLM 调用标准化规范


> 文档分发：当前实现入口为 `docs/docs_public/英语内容生产工作台-当前实现与系统架构.md`；下文旧现状/验收文档仅在维护者本机保留，不随公开clone提供，源码仍为核验实现的依据。
> 适用范围：本项目新增或修改任何**对话式 LLM 调用**（chat completions）时，必须按此规范检查。
> Embedding / 图像 / TTS 等非对话类模型调用不强制 prompt 独立化，但必须带 trace 记录。

---

## 0. 核心原则

### 0.1 Prompt 必独立
任何对话式 LLM 调用的**提示词文本**（system/user/assistant 角色内容）不得硬编码在 `.py` 源码中，必须以独立文本文件存于 `backend/prompts/` 目录，由 `app/prompt_loader.py` 的 `load_prompt()` / `render()` 函数加载与渲染。

### 0.2 Skill 按需配置
Skill 不是给所有 LLM 调用都套一层。Skill 的本质是「**多流程步骤的可复用行为规范**」。

- **需要 Skill 的场景**：该 LLM 调用是一个**可重复的、多步骤的、有明确工作规则的业务动作**，需要稳定的行为约束、Anti-Patterns、执行流程。
- **不需要 Skill 的场景**：该 LLM 调用是**单步纯推理 / 纯格式化 / 纯变换**，或只是通用工程层的包装（如 JSON 强制约束）。

### 0.3 三层校验 + Trace 必录
- 结构化输出必须过 Pydantic 二次校验（代码兜底，不依赖 Prompt 内「请返回 JSON」）
- 每次 LLM 调用必须带 `trace_id`，入库 `trace_log`（含输入输出/耗时/成本）

---

## 1. 判断流程：我现在要加一个 LLM 调用，怎么搞？

```
步骤 A：判断模型类型
    ├─ Embedding / Audio / Vision（非 chat）→ 只需：config 取 key + 记录 trace，无需 prompt/skill
    └─ Chat Completions（对话 LLM）→ 进入步骤 B

步骤 B：Prompt 独立化（必做）
    1. 该调用的 system prompt 是否超过 3 行？ → 是 → 单独 .st
       （即使一行也要独立；但工程层通用的一行提示可共用通用模板，比如 json-enforce.st）
    2. 该调用有 system + user 两个角色吗？ → 都有 → 命名 {scenario}-system.st + {scenario}-user.st
    3. 用户提示包含动态参数吗？ → 是 → 用 {{param}} 占位符，prompt_loader.render() 注入
    4. 需要注入题型/任务专属规则（skill）吗？ → 是 → system st 模板中保留或在代码后置拼接

步骤 C：是否需要 Skill？（决策树）
    ├─ 多流程步骤？（分步思考 → 执行 → 自检？）                ┐
    ├─ 需要严格行为规范？（如评分尺度一致性 / 干扰项设计原则） │→ 需要 Skill
    ├─ 有 Anti-Patterns（易踩坑但规则可描述）？              │
    ├─ 可复用（多题型/多场景共用同一套规范）？               ┘
    └─ 单步纯变换？（翻译 / 格式转换 / 简单分类）
           └─ 不需要 Skill：在 .st 里写清楚指令即可

步骤 D：执行落地
    - 创建/更新 prompts/下的 .st
    - 需要 Skill → 创建 skills/{id}/SKILL.md + skill.meta.yml
    - 代码经 prompt_loader.load_prompt() 加载，再 render({...}) 注入参数
    - 每次调用经 record_trace() 记录 trace_log
```

---

## 2. Skill 判定 Checklist（必须满足 ≥3 条才值得做 Skill）

> 满足不足 3 条的场景，直接写在 .st prompt 里，不要硬造 Skill。

| # | 判定条件 | 判定说明 |
|---|---------|---------|
| 1 | **有明确的「执行流程」** | 超过 1 个操作步骤（先…再…最后…），或需要分阶段思维链 |
| 2 | **有明确的「工作规则」/ Anti-Patterns** | 不能只说「要做好」，必须能列出 ≥3 条「禁止项」或「必做项」 |
| 3 | **有「目标场景」可描述** | 能用一句话明确：此 Skill 用来解决什么问题，在什么业务场景被调用 |
| 4 | **输入/输出契约稳定** | 入参、出参的 schema 明确，不会每次调用都改结构 |
| 5 | **可复用**（≥2 个调用点 / 或 ≥2 个题型共用） | 多题型共享的通用能力（如 judge 质检 / 改版修订 / 题目拆小题） |
| 6 | **需要 Reference 文件** | 需要配套参考资料（如课标摘录、题型示例库、评分 rubric 集）才做得好 |

---

## 3. Prompt 独立化 Checklist

每次新增/修改对话 LLM 调用，对照本表：

### 3.1 命名（强约束）

```
{scenario}-system.st    →  system 角色内容
{scenario}-user.st      →  user 角色内容（含 {{placeholder}} 占位符）
{scenario}.st           →  单角色场景（非 system/user 分离的单提示词场景，尽量少用）
```

`scenario` 命名规则：
- **按题型**：`single_choice`, `cloze`, `reading`（直接对映 `templates/*.yaml` 的 `type_id`）
- **按业务动作**：`judge`（质检）、`revise`（改版）、`summarize`（摘要）
- **按工程层通用能力**：`json-enforce`（JSON 强制约束）、`schema-constraint`（JSON 结构说明包装）

### 3.2 内容（强约束）

- [ ] `system.st` 包含：Role / 目标 / 核心原则 / 输出约束（四要素不必分章节，但内容必须齐全）
- [ ] `user.st` 包含：`{{input_data}}` 形式的参数占位符（占位符名与调用方传参严格一致）
- [ ] 占位符只占动态内容，静态文字不占（避免「模板里就一行 {{all}}」，丧失独立化意义）
- [ ] 如需注入 skill 规范：在 system st 末尾留一个锚点段落，或在调用代码中 `f"{system_prompt}\n\n# {skill_title}\n{skill_md}"` 拼接
- [ ] 输出格式要求放在 system（不是 user），除非格式依赖输入参数

### 3.3 渲染（强约束）

- [ ] 代码经 `prompt_loader.load_prompt("xxx.st")` 读取原始模板
- [ ] 参数注入用 `prompt_loader.render(template, {params…})`，不用手工 `.format()` / f-string
- [ ] `render()` 缺占位符不报错（严格模式由 prompt_loader 决定，默认缺失留空串）

---

## 4. Skill 文件骨架（新建时必含以下段）

```
backend/skills/{skill_id}/
├── SKILL.md           ← 主文档（必含，下文结构）
└── skill.meta.yml     ← 元信息（必含，display + categories）
```

### SKILL.md 结构（必含 6 段）

```
---
name: {skill_id}
description: 一句话描述此 Skill 的业务用途与核心能力。
---

# Overview
- 本 Skill 用来做什么？在哪个业务场景被调用？
- 与相关 Skill 的边界（比如 judge / revise / generate_question 不要互相覆盖职责）。

# 核心目标
- 列出 2-4 条可量化的成功标准（不是口号）。
  例：「稳定性：同一样本多次打分方差 < 1」；「一致性：跨样本同维度尺度一致」

# 执行流程 / Workflow
- 分步骤列：Step 1 …… Step 2 …… Step 3 ……
- 每步写清：输入是什么 → 处理什么 → 输出什么
- ≥2 个步骤才有写此段的意义，<2 步直接进 目标 / 规则即可

# 工作规则
- ≥5 条「必须/严禁」级别的规则，不含糊。
- 每条尽量带判定句（「如果 X 则 Y」），不用「尽量」「最好」之类模糊词。

# Anti-Patterns（禁止事项）
- ≥3 条反模式，每条用 ❌ 标记，说明"为什么不能这么做"。
- 例：❌ 输出 JSON 以外的解释文字 → 调用方无法 parse，导致二次校验失败。

# 输出格式 / 输入输出契约
- 明确输入的字段与含义；
- 明确输出的字段结构（与 prompt st 文件的 Output Format 保持一致）。

# 常见设问句式 / 参考示例（可选）
- 可复用的句式库；
- 可放置 Reference 索引：如 "reference 目录见 docs/references/{skill_id}/"。
```

### skill.meta.yml 结构（必含）

```yaml
displayName: <中文可读名，2-6 字>
display:
  icon: <图标文字>
categories:
  - key: <分类标识大写，如 QUESTION / QUALITY / REVISE>
    label: <中文可读分类>
    priority: CORE   # CORE（核心）或 AUX（辅助）
```

---

## 5. Reference 文件何时需要？

| 需要 Reference 的情况 | 示例 |
|---------------------|------|
| Skill 需要外部知识才能执行正确 | 出题 skill 需要课标摘录、真题范例库、考点分布表 |
| Skill 的判定标准是「对照某文档执行」 | judge skill 有 rubric 细则库（分题型×难度的分档标准） |
| Skill 有大量可复用句式/模板片段库 | generate_question 需要"设问句式库 / 干扰项类型库" |

**存放位置**：`docs/references/{skill_id}/` 下，文件名说明用途（如 `kb_curriculum_grade7.md`）。
在 SKILL.md 的末尾「Reference」段落列出，不要把整份 Reference 塞到 SKILL.md 正文。

---

## 6. 项目内所有 LLM 调用点对照表（P0/P1 修复快照 2026-10-04）

| # | 调用模块 | Prompt 独立化 | Skill | Trace | Pydantic 二次校验 | 状态 |
|---|---------|-------------|-------|-------|-----------------|------|
| 1 | `structured_output.py` 内容生成 | `single_choice/cloze/reading` × system/user.st + `schema-constraint.st` / `json-enforce.st` 通用 | `single_choice`/`cloze`/`reading` 题型 skill + `generate_question` 兜底 | ✅ | ✅ | ✅ 合规 |
| 2 | `quality.py` LLM-as-judge 质检 | `judge-system.st` + `judge-user.st`，独立注入本次生成约束 | `judge`（通用质检评分 Skill） | 成功/失败尝试均记录 token/成本 | 动态 Pydantic：维度齐全、0–100 有限数 | 有限重试、耗尽失败关闭（OPT-039/040） |
| 3 | `rag/embedding.py` 向量化 | 非对话模型，N/A | N/A | 成功/失败、tenant/task/trace、usage/成本均记录，支持补偿 | 索引数量/维度/有限数校验 | P1 已实现，真实服务未验 |
| 4 | 改版 revise（graph.py 节点里调用 generate_structured） | 复用 `revise-system/user.st`（题型专属，经 `build_user_prompt` 注入 revise_flag） | 复用题型 skill + 改版规则来自 quality_record | ✅ | ✅ | ✅ 合规（复用生成链路） |

---

## 7. 新增 LLM 调用 Step-by-Step

```
1. 在代码里写好调用前的参数准备，确定 system/user 有哪些固定文本，哪些是动态参数。
2. 确定 scenario 名（按题型 or 按动作 or 按通用能力）。
3. 新建 prompts/{scenario}-system.st + -user.st，把固定文本填入，动态部分用 {{param}}。
4. 用 §2 的 Skill Checklist（≥3 条）判断要不要做 Skill。
   - 要做 → 按 §4 骨架新建 skills/{scenario}/SKILL.md + skill.meta.yml
   - 不做 → 在 system.st 里写清楚工作规则，别硬造 Skill。
5. 写 Reference 判断（§5）：
   - 要 Reference → 在 docs/references/{scenario}/ 放 md，在 SKILL.md 引用
6. 调用代码：load_prompt → render 注入 params → LLM 调用 → Pydantic 校验 → record_trace
7. 补单测：mock LLM 输出，验证
   - prompt 加载正确（含占位符替换后的内容）
   - skill 注入正确（如 system prompt 含 SKILL.md 的标识段）
   - 异常路径（校验失败重试、降级）正确
8. 把本对照表的第 6 节更新，追加新的调用点行。
```

P1 补充：独立生成/Judge 调用缺少 trace_id 时自动创建 ID；DB 失败持久化补偿或严格失败关闭，unknown usage 不得伪装提供商报告 0。RAG 事实依据记录和人工核验配置见 `docs/P1-修复与验收.md`，本轮不声明真实供应商/生产验收通过。


## RAG query expansion / rerank（P1 DEF）
- query expansion 使用 rag-expansion-system.st / rag-expansion-user.st + ExpandedQueries Pydantic；稳定文件 prompt hash，原查询保留，scope 不允许由模型修改。该步骤是单步结构化转换，不额外新增多步 Skill。
- 默认 aliases 无 chat 调用；LLM 显式配置后最多 4 个 query、替代 query≤128 字符、输出 budget、timeout 和 SDK max_retries=0，失败记录并回退。
- rerank 为 JSON 非聊天接口，不要求 .st，但结构/索引/finite score 必须代码校验；optional 回退，required fail-closed。模型/端点/key 读 settings，调用带 trace_id/模型/耗时；未知 provider 计费不能套用 LLM 价格伪装已对账。


## OCR-4 真实embedding补充（2026-10-05，OPT-066）
- 用户本轮明确授权实际embedding；完整8页36个1024维非零向量与查询真实调用已通过，不用mock代表供应商验收。
- 工作台索引必须代码校验审核与费用声明、hash/config/partial范围；新后台任务幂等，同事务写知识与状态，不确定费用不自动再调用。
- 未配置模型价目表时Trace标global_fallback_unverified/provider_bill_verified=false，工作台不承诺金额；当前配置估算不能当实际币种账单。非聊天任务仍需要Trace/usage/版本/耗时。


## 重建与检索评测调用（2026-10-05，OPT-067）
- 显式OCR单文档重建再次调用真实embedding，expected revision/审核/付费声明在调用前代码校验，成功前旧索引保留。失联/不确定费用需人工Trace核对，不静默重付。
- rag_eval.py每条query真实embedding并记录Trace，只读语料不等于没有调用费用；冻结gold/保存失败，页级Hit/MRR/NDCG不冒充ANN leaf Recall或答案正确率。
- 本轮实际26次embedding/5146 reported prompt tokens；global fallback配置估算0.010292，币种/账单未核验；无真实chat/judge/rerank。引用hash完整性不是事实认证。


## 检索A/B与离线重算（2026-10-05，OPT-068）
- bigram关键词补召回不调用chat、不自动增加query expansion费用；默认aliases/rerankoff不变。新评测使用run-id+case-id区分Trace并限制总长，真实查询仍有embedding费用。
- 本轮92真实embedding/944报告tokens，仅查询不重嵌入语料；配置fallback估算0.001888，币种/账单未核验，无真实chat/judge/rerank。
- 保存gold/语料向量指纹/原始引用与diagnostics；v3可纯函数离线重算同一结果，明确零新增供应商调用，不用mock冒充新真实检索。
- 页/块/同行与context_complete是标注支持指标，不是答案事实正确率；保留跨页部分命中和CI79.51%未过门槛，不报36/36完整生产。


## STR-1/2现役约束（OPT-069）
- 结构是版本化派生层，不覆盖原文；布局家具不重置同章小节，缺页/排除/新章/独立题答案是屏障，未知关系proposed且越屏障ID不可确认。
- relation只从当前同scope活动leaf取body，stale/过期审核不扩展；不可从document snapshot回注已删文字。source segments保真实页/块/坐标/hash，组合体不伪造单跨度/bbox。
- top_k仍约束bundle/snippet返回数，实际segments可更多；生成/评测/人工快照计实际送达内容，不按claimed IDs或citations[:top_k]计全部证据。
- 免费结构预览/审核不embedding，OCR计划包含structure/policy签名；new索引必要时才显式费控。运行relation、legacy可回退，预算/member/hop/segment硬上限不依靠prompt。
- 当前751/32真PG、app80.57%，原36齐备/新8仅7齐备，事实状态unverified；STR-3～6及真实生成质量未完成。181本轮embedding/2037报告tokens，配置估算0.004074/账单未验，无真实chat/judge/rerank。
- 现役证据：`docs\现状文档\RAG-STR1-STR2落地与验收.md`。更早STR-0仅调研、79.51%未过门槛为历史，原未提交变更/私有现场有意保留，不自动清场。


## STR-4互补问题与STR-3本地窗口（OPT-070）
- planning=rules无chat，复合问题会增加有界embedding输入；原query必留，最多4parts/总8queries，不允许模型改scope。
- planning=llm用rag-plan-system/user.st+严格Parts二次校验/timeout/Trace，失败回rules；本轮仅mock兼容测试，未真实调用chat，不报供应商效果。
- 本轮178真实embedding/2140 reported tokens，global fallback估算0.00428/币种账单未验；未真实judge/rerank/重嵌入。真实本地MinerU2页/3页复核无云回退、独立snapshot/不自动入知识索引。
- 源table/cell原引用与派生合并parts/hash分开，结构确认不代表事实认证；真实44gold齐备不代表新教材整表OCR准确率。详见 `docs\现状文档\RAG-STR3-STR4落地与验收.md`。


## STR-6独立实验调用（OPT-071）
- `rag_experiment.py`默认dry-run；云embedding需显式`--run --paid-embedding`，LLM-context须另加`--allow-llm-context`，不改变全局chat/生产检索默认。实验可写Trace及报告，不写知识索引；输入/句数/chunk/token上限在可行时调用前检查。
- contextual系统/用户prompt独立文件，Pydantic严格输出；只接受源中存在的extractive片段，派生prefix/hash和原文分离，失败关闭/timeout/Trace。抽取来源约束不等于事实认证。未实际调用供应商不得宣传兼容或收益。
- Late需显式local-only token模型/safetensors/fast offsets及文件hash，禁止下载、remote code、静默截断及伪token输入；本地forward记`rag_late_local` Trace，物理算力不报价/不冒充云bill。
- 句向量边界、chunk向量、query向量均计实际embedding调用；固定gold/源/config/model/hash分别记录。费用只能据Trace reported usage及标注的配置估算，供应商币种/实付未核验时不得宣称“实际花费”。真实小样本无增益如实保留；Late dry-run不得计真实运行。
- 本轮真实Late和LLM-context效果待验，详见 `docs\现状文档\RAG-STR5-STR6落地与验收.md`，不能从阶段代码交付推断全教材质量。


## RAG预算与模型窗口（OPT-072）
- 应用RAG默认返回Top-5/可选8，总32768字符；不是模型token窗口。增加字符/Top-K不自动增加同query的embedding输入，但生成prompt变长后的费用/延迟/事实正确性应真实另测。
- 当前可选RAG_CONTEXT_TOKEN_LIMIT是UTF-8字节保守预算，不是tokenizer实测token；默认None，显式值仍硬限制。官方窗口metadata不等于兼容转发端点已实测1M请求，本轮无真实chat/judge。
- 用户授权预算变化用gold不可变/effective_top_k/override单列报告；不把来源补回说成融合算法优化，不把未触旧预算的小样本声称为长上下文质量增益。现役证据见 `docs\现状文档\RAG-Top5-Top8与上下文预算验收.md`。


## 真实生成验收边界（OPT-073）
- source-required模板无有效RAG来源不得调用chat；生成Trace的`input_data.params`可复核实际RAG上下文和source citation，但不得把RAG文本混入judge评分目标之外的生成约束。
- 结构化输出解析/Pydantic校验失败必须记录失败Trace并按模板max_retry重试；失败尝试的usage也计入成本，不能只统计最终成功。
- 自动Judge/QC只是筛选信号。来源要求开启时，人工通过必须确认真实segments与题干/答案/解析一致；pending_qc不得发布。正向小样本发现题型错误时，先建回归/规则再扩大批量。


## 单项生成数量（OPT-074）
- 一个workflow/item输出一个题目或语篇对象，params.quantity固定1；外层批次数不得原样渲染为每项再次批量生成。旧checkpoint也在生成前兜底。
- 多元素列表/多命名对象包装不静默取第一个，走现有Schema拒绝/有限反馈重试；单字段单元素兼容可保留。
- 自动恢复有预算/终态/锁保护，但provider响应后本地未保存仍可能重复费用，不宣称exactly-once。本次修复以mock/真PG/真Redis/网页拦截验收，无新增真实模型调用。


## 管理员Provider与路由（OPT-076）
- 实际chat客户端由Provider绑定档案选择，生成/Judge可独立Provider；配置缺失、禁用或错key不静默换成ENV。旧未绑定档案保留ENV兼容。
- 每次调用记录实际model与安全profile/provider配置身份，禁止凭据进入Prompt、Trace、审计、响应或错误正文；请求校验不回显原始input。
- /models手动探测仅验证该接口，不代表chat/JSON/题目质量通过；不同档案名也不证明不同模型家族/价格，必须据真实候选评测再宣称效果。


## 受限请求/响应快照与示例（OPT-077）
- 新chat调用把实际messages、请求参数与应用收到的响应文本通过snapshot_data封存；摘要保持脱敏/截断，密文留独立受限存储。不得记录API Key/Authorization headers、原始敏感错误正文，缺失/超限/旧记录不伪称完整回放。
- few-shot为服务器选择的人审整例，独立于RAG事实和Judge目标；scope/源状态/实际人审/hash/现役规则/整例预算必须代码强制。客户端与checkpoint不可伪造示例上下文，复制整个示例输出不得直接通过。
- 模型比较只有显式execute才收费，必须保留相同输入/source身份与失败费用，不暗换默认模型、不代签人工标签、不将自动分数宣称专家结论。
