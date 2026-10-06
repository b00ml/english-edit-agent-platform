# RAG STR-3 / STR-4：逻辑表格、多页复核与多证据检索验收

**变更**：OPT-070
**记录日期**：2026-10-05 UTC；实测本机 Asia/Shanghai 日志日期为2026-10-06
**当前状态**：STR-3/STR-4的代码、审核/前端/API、后台多页复核、模拟/真实PG及原环境实测已交付。STR-5/6、全目录复杂资料/产出事实质量未验收。

## 1. 交付范围与不能混淆的证据

| 范围 | 已交付 | 验证证据 |
|---|---|---|
| 逻辑续表 | 多证据accepted/proposed/rejected关系、logical table ID、重复表头派生去重、逐cell审核与原列范围 | 单测/真实PG固定HTML/grid/span；不是新教材整表准确率 |
| 原生关系 | table caption/body/footnote及continues_prev桥接，所有物理源保留 | adapter/结构回归，原源hash不覆盖 |
| 多页复核 | 连续2/3页独立后台job、本地V1多页契约、局部→全局页、源/版本缓存、取消/续跑/回收/幂等 | 原并列句页2/3和2/3/4真实MinerU4.0.10完成 |
| 跨章导航 | 明确参见章节→唯一真实目标正文的有向关系、失配/多义引用标未定位、不突破scope | 模拟/真实PG，source segments原出处保留 |
| 复合检索 | 原query保留、有界互补子问题、canonical概念导航、多lane覆盖与预算分配 | 原36+8真实embedding off/rules对照、实际工作台 |
| 选配LLM | 独立.st/schema/Trace、失败回规则、仅显式配置触发 | mock验证；**没有真实chat调用/收益实测** |

两个真实OCR窗口没有表格，证明多页入口/队列/映射可用，**不证明真实跨页表格或cell识别已全部正确**。续表/cell的确定性结构与原文引用测试使用自有fixture，未把模拟引擎或grid结果冒充真实教材OCR。

## 2. STR-3：逻辑表格与单元格

### 2.1 自动确认条件与负例

`rag/structure/tables.py`在STR-1结构完成后建立独立派生计划：

- 页序连续；中间不能有新章/节、缺页、排除块、独立题目、未知正文或工作表变更。
- 列数/逻辑范围兼容；位置需有效normalized_page单位及相同非空坐标frame。
- 同时具备页底/页顶及列位置对齐，再结合明确引擎continuation与已确认重复表头/相同编号，或相同表编号+续表caption+已确认重复表头。
- 只列数相同、同表头或bbox缺失均不自动accept；可作为proposed由人工确认。未知表头不把候选首行当“已证实表头”。
- 表数≤256、原行/spans≤10000、续表/cell待审核集合≤256，签名包含logical_tables/未定位引用，策略变化不会继续复用旧审核。

原text/grid/spans/bbox/native snapshot不重写。逻辑组保存物理table ID/页/块、原header rows、normalized→source row映射、rowspan/colspan与内容hash。

### 2.2 跨页cell不是整行自动粘

- 先确认table_continues，再分别生成对应边界源行的cell候选。
- 用原column..column+colspan逻辑范围匹配；span不匹配或跨边界rowspan无法证明时标`unresolved_cell_boundaries`，不生成可自动合并的伪cell。
- 每个cell的确认/拒绝与表确认独立；只确认一个cell不会把其他列顺手拼起来。没有accepted table就拒绝accepted cell。
- 展示的合并值只来自**实际送达leaf片段**，每列保存所有segment/page/block/source row/column parts；多段/多页连锁续接可沿同源range延续。
- 重复确认表头只在派生视图显示一次；header_sources保留各页原文依据。未知header只能显示明确标记的合成列名，不删除原首行。
- 原始片段与派生表视图都保留；派生view有content hash、context_delivered与完整物理segment状态，预算不够时明确省略，不宣传合并内容已送模型。

### 2.3 权限、删除、预算

仍从同document/tenant/knowledge/document/section scope的活动leaf取正文；logical grid内容不能代替已删源。删除一个物理table segment使资料stale，不再扩旧group；未知/部分行/缺表头不伪造完整表。

派生表的字符/UTF8字节也计入全局预算；复合问题还有按待覆盖来源公平分配的局部预算，避免第一个长知识单元挤掉后面的定义/例外。

## 3. 疑难2/3页复核：独立持久化，不覆盖成功页

### 新模型与迁移

新增 `OcrBoundaryJob` / `ocr_boundary_job`，迁移head为`rag_str34_boundary`，从`rag_ocr_review`新增表。字段包括parent job/tenant/pages/source_preview_hash/status/token/lease/attempts/result hash/cache/engine/error。

原主/CI数据库迁移前pg_dump到`.local-eval/str34`，实际升级head，不stamp、不清库、不新建业务隔离库。旧资料/成功页/FK保留。

### 执行路径

```text
工作台明确确认本地计算
  → API校验原OCR完成、原selected范围内连续2/3页
  → SQL持久pending并投独立ocr队列
  → 原PDF局部抽页，本地MinerU basic/ocr V1
  → 校验local pages 0..N-1完整、native index/Producer协议
  → 映射global physical pages，独立原生/结构快照
  → fencing token/hash提交completed，不改parent preview/index
```

- 原单页接口仍返回严格单页NativeDocument；多页新契约单独使用，不能把max_length改大后让旧调用接错映射。
- 只允许本机/私网已配置URL、同源输出资源、鉴权/输入输出上限/总时限/cancel检查，无云/LLM回退。
- 缓存键包含tenant、source/preview hash、global pages、引擎版本、客户端/adapter/table/structure源码与policy/配置版本；结果envelope hash和identity再校验，损坏不会直接当成功。
- 创建同范围/同preview的活动或完成任务幂等，重复worker消息不再调用。cancel失效token，旧owner返回不得覆盖取消；过期lease由scheduler回收，尝试默认3次，耗尽failed，不无限重投。
- Broker不可用时pending保留并周期补发；resume重新核验源范围/版本。失败/取消不删原成功页、原OCR preview或索引。
- HTTP请求不跑重OCR；查询result有鉴权/父任务归属/hash/preview漂移检查，控制API在scope内执行。
- 没有自动“应用多页结果”按钮：两窗同页识别差异供人工核对，不能悄悄替换已审核源。需要改源时另走显式重新解析/重建，并保留旧索引原子规则。

### 真实两种窗口

- 原并列句job `d7419129-dfc6-4498-83e4-9b5fac0a347e`。
- 2页任务 `b66570ea-fd63-4e62-9671-f38376454ca6`：global `[2,3]`、local `{0:2,1:3}`，completed/attempt1。
- 3页任务 `1586d621-167e-4054-96fc-783ee6a21634`：global `[2,3,4]`、local `{0:2,1:3,2:4}`，completed/attempt1。
- 两次调用真实本地MinerU **4.0.10**；没有新云OCR、embedding或chat用于窗口。原preview hash均`4cb7be39e57112e151e2355999a6f41e90de12c2e46f179d10a672d6a5d00302`，原parent未替换。
- 最终工作台列表、按钮声明门控、独立result/structure视图、状态完成可用，无pageerror；取消/恢复/缓存/失联等路径由mock协议+真实PG补验，不把它们全称已真实停机演练。

## 4. STR-4：跨章节与复合问题

### 4.1 明确引用导航

`structure/references.py`识别配置pattern中的“参见第二章/see Chapter 2”等：

- 只匹配同文档真实存在且唯一的section；目标为其真实正文/子节，而非孤立heading。
- refs有方向，目标不反向拉回所有引用者；环有visited/hop限额。
- 缺目标/同名多义写unresolved_references，不选择任意章，不凭模型造章。
- 用户section/document/knowledge/tenant范围仍在活动leaf筛选层强制；明确引用不获得越scope权限。
- 相关定义、例外各自保source segments和原section path，不能拼成“原文件的一段”；冲突资料仍独立unverified，未提供机器事实仲裁。

### 4.2 多问题检索与概念导航

新增`retrieval/planning.py`、`planning.yml`：

- 默认`RAG_QUERY_PLANNING_MODE=rules`，识别“以及/同时/另外/分号/and also”等明确互补连接；无复合目标不加子query。
- 用已有canonical/alias字典识别多个概念，仅供导航与候选分配，不能因为同标签或句中出现词就自动改knowledge scope。
- 原query永远作为query0；互补问题默认最多4、单条≤256，整个query集≤8。有限长度/去重，不由供应商输出控制tenant/document过滤。
- 每个子query沿原vector/keyword lanes召回，再按各互补lane选种子，去重后剩余沿融合顺序；bundle仍保持top_k与scope/预算门控。
- 公平预算保证第一个大bundle不占掉所有后续证据；候选分配本身并不宣称回答已齐备，context_complete按实际segments评测。
- 关闭planning可回上一阶段；默认rules不调用chat。LLM模式仅显式配置，独立system/user .st+严格Pydantic Parts/超时/Trace/usage/错误回规则，真实供应商效果与账单未验。

现有YAML模型/LLM档案与embedding配置不硬编码新模型。没有引入GraphRAG、图数据库或全目录章节摘要。

## 5. 真实A/B：原gold不改，44个有限样本

相同3资料、同leaf/source/向量指纹，relation context固定；只比较planning off与rules，hybrid/pool30/RRF60、相同top_k、rerank off、评测进程query expansion off。运行服务aliases默认保持。

| 指标 | planning off | planning rules |
|---|---:|---:|
| 原36回归context_complete | 36/36 | 36/36 |
| 旧独立8context_complete | 7/8 | **8/8** |
| 旧独立8精确unit recall | 0.9375 | **1.0000** |
| 旧独立8unit NDCG | 0.951643 | **1.0000** |
| 原表行case/tuple | 10/10、13/13 | 相同 |
| 旧独立表行case/tuple | 2/2、4/4 | 相同 |

原失败“for和because表示原因时句子位置，以及so表因果的例句是什么？”被拆为两个互补问题，分别引入页4规则与页3例句；实际前端确认两组原chunk/source segments齐备，不靠扩大top_k或单题路由。

这8条是**上一阶段已冻结集**，本轮没有新增独立专家盲测集；44/44只是这些已标注来源支持，不是全库召回率、答案正确率或真实跨页cell的全格式质量认证。全表格OCR/专家金标还需要新资料。

Gold SHA-256：36条`905986b2a9dc8260dc8fc324e963f155f44896ebfde216c1b9683f2c9aa62ecf`，8条`7e4f0bee3d32ae2d8b2f600ccd4ab81f27e79dd73a0d6f80de8da267c0f41174`。

最终所有组语料/向量指纹相同：`7f4baa8d0b048b34281a7f238345632cf60dafb5e622cdd0eb7e250c89544cab`，无fallback、snapshot hash全对。初次结果保留initial-ab，最后签名/预算/引用校验后同集再次实测；不覆盖唯一证据或将反复同集当泛化证明。

## 6. v5评测与源/派生关系

`rag_eval.py`新增`required_logical_rows`：

- 物理同行要求仍只在实际源表行计分；跨页拼cell的逻辑行单列，不把source row定位改成派生位置。
- logical view必须标context_delivered，所有source_segment_ids必须已经送入上下文。
- 对每个cell从来源segment和column重算原值/确认拼接值，不能用伪造text+合法segment ID骗过支持判定。
- 预算省略派生view、引用未送达、假ID/错误列/值改写不能得分；source hash不被派生表hash替代。
- 该新增评分由fixture反例验证，不在真实教材44条里凭空增加跨页cell金标或收益数字。

## 7. 门禁、部署与调用

- 新增56非集成+4真实PG，共60条；最终 **811 passed / 36 integration / 775非集成**，18条既有warning保留。
- 实际运行仅`--cov=app --cov-fail-under=80`，**7448/9124 = 81.6309%**，门槛通过，未混入脚本/降低阈值/排除低覆盖文件。
- 修改20个Python文件black/isort、后端flake8、14个本轮模块scoped strict mypy、前端TS/Vite/镜像/Compose、后端和代理ready200/Nginx通过。不是全appstrict或远端CI自动认证。
- 真PG覆盖logical table/cell+源hash、明确ref/子问题、boundary幂等取消/恢复及删物理续表不回注snapshot；fixture只清理UUID事务，不清业务库。
- 本轮2轮A/B各88个查询+2个实际UI复合查询：**178次真实embedding**；reported输入tokens **2140**；Trace fallback配置估算 **0.004280**。全部成功/report usage，无真实chat/judge/rerank；币种/价目/实际供应商账单未核验，不能报实付金额。
- 原业务task3/content4/users1、OCRjob4/page12、knowledge Doc3/chunk54不变；仍52真实非零1024维leaf向量+2parent，source hash/content hash/leaf/index版本不变，无重嵌入。
- 新增且有意保留2个completed OcrBoundaryJob。结构policy/version从v1→v2，原结构审核签名按预期失效；实际来源已核对后重新显式保存默认决定，不能把旧审核当成新策略自动批准。
- 主/CI真实迁移head均`rag_str34_boundary`，迁移前备份/所有私有结果保留；原宿主MinerU、backend/worker/OCR worker/scheduler/frontend及数据库继续运行。

## 8. 文件入口与私有证据

核心代码：
- `backend\app\rag\structure\tables.py`、`references.py`、`builder.py`、`runtime.py`、`policy.yml`
- `backend\app\rag\retrieval\planning.py`/`planning.yml`、`table_context.py`、`relation.py`
- `backend\app\rag\ocr\mineru.py`/`adapter.py`
- `backend\app\services\boundary_service.py`、`worker\boundary.py`、`worker\ocr_tasks.py`、`api\ocr_routes.py`
- `backend\alembic\versions\rag_str34_boundary.py`、`app\models.py`、`app\config.py`、`deploy\docker-compose.yml`
- `frontend\src\pages\BoundaryReview.tsx`及结构预览/知识库/OCR工作台/API类型
- `backend\scripts\rag_eval.py`、`tests\test_rag_str34.py`、`tests\integration\test_rag_str34_postgres.py`

私有目录：`.local-eval\str34`（Git忽略）：

- 两原库`*-before.dump`、before-db与final-db-and-ledger、start-utc、verification-summary。
- gold-freeze、36+8原gold、四最终报告/ab-summary、initial-ab。
- compound-query/compound-live、live-start/live-final-start、双/三页summary/native+document+structure/result及截图。
- full-app-gate.log、app-coverage.json：最终811与81.63%的原始门禁证据。

API复核只本地计算，不embedding；复跑rag_eval仍要真实embedding费与Trace。回退STR-4设RAG_QUERY_PLANNING_MODE=off；旧上下文仍可context_mode=legacy。不要将down迁移/删除资料当成关闭新功能的方法。

## 9. 收尾与仍待

| 事实面 | 状态 |
|---|---|
| STR-3/4功能、负例、协议/权限/迁移 | changed-and-verified |
| 最新原部署、真实2/3页队列、复合界面查询 | changed-and-verified |
| 文档/任务/OPT/规则 | changed-and-verified |
| 外部记忆 | out-of-scope，不写入作为权威 |
| 工作区 | 原大量未提交变更、备份/复核现场保留，未清场/提交 |
| 新教材跨页table/cell整体质量、专家盲测、真实生成/judge | pending |
| STR-5/6、规模/GPU/完整故障/长期运营 | pending |

完整交付不是承诺任意PDF续表都自动正确：无证据/复杂rowspan边界仍安全待确认，LLM增强不默认启用，独立boundary识别差异不自动重写原源。后续从STR-5结构切分/必要显式原子重建与新复杂教材金标继续。
