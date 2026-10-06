# RAG P2：索引运营与多文档真实检索评测

**日期**：2026-10-05
**变更**：OPT-067
**状态**：已部署并验证 OCR 来源单文档重建/撤除；多 PDF 有限样本评测完成。**不是完整 P2 G 验收，也不是批量生成事实质量认证。**

## 1. 本轮完成范围

沿用原 Docker Compose、业务库与已有 CI PostgreSQL，不新建业务隔离环境，不清库。复用已完成的 OCR 页检查点，新增名词/动词选页索引，并对并列句做一次真实工作台重建。用户此前授权的真实 embedding 用于索引与检索；没有调用真实 chat、judge 或 rerank。

| 阶段 | 当前交付 | 尚未完成 |
|---|---|---|
| G1 索引/版本治理 | OCR 来源单文档显式重建、活动索引 revision、旧索引原子切换、失败保留、整资料撤除、部分删除一致性 | 单租户批量重建、legacy/非 OCR 来源重建、历史版本长期保留与回滚、全面故障演练 |
| G2 预览 | 原页、原始识别块、过滤后切块、表格/告警、审核设置恢复 | 大量复杂跨页题组/续表的完整人工金标 |
| G3 检索诊断 | 既有 candidate/scope/fusion/fallback/citation 诊断继续可用 | 规模性能与完整运营看板 |
| G4 评测 | 真实 embedding 的 3 份 PDF、18 条冻结页级样本；可重复使用的 CLI | 纯文本、DOCX 表格、XLSX 四类完整集；chunk/表行精确金标及生成事实评分 |
| G5 生产准入 | 原部署与实际供应商 embedding 的有限证据 | 全库稳定性、吞吐/延迟、供应商账单、真实生成/质检/人工驳回率 |

## 2. 索引运营实现

### 2.1 重建：显式授权，同 ID 原子替换

- 工作台必须重新确认原页/告警/付费声明；已存在索引时额外勾选“显式重建当前索引”，传 `rebuild_index=true` 与 `expected_index_revision`。版本不匹配在付费前拒绝；普通重复审核/消息仍保持幂等。
- 审核计划绑定原预览 hash、排除块、当前 chunk/parent 配置、embedding 模型/维度及 taxonomy。恢复此前标签、备注和排除决定，但费用/核对 checkbox **不继承确认**。
- 后台先计算并校验全部新向量，再在同事务中删旧 chunks、更新原 KnowledgeDocument 行、写入新 chunks 和 OCR 状态。文档 ID/FK 不变；成功后 revision +1。
- 向量异常或提交失败不清掉旧索引；任务进入 `needs_attention`，未知付费尝试不自动重放。重试需人工核对 Trace 并显式授权，旧索引继续可查。
- 当前重建从已保留的 OCR 结构快照开始；**parser/引擎升级若需要重新识别，必须另走 OCR，不会在索引重建时自动重新解析原 PDF**。不是所有文件类型的通用重建器。
- revision 是当前活动文档生命周期的版本；撤除后再次索引可从 v1 开始。未保留每版完整旧向量，不提供任意历史版本回滚。

### 2.2 撤除与单块删除

- 新增 `GET /api/knowledge/documents`、`DELETE /api/knowledge/documents/{document_id}`，沿用 `ops:read/write` 与既有租户/管理员权限。
- 整资料撤除：删除文档及其 leaf/parent；OCR 关联置空、索引状态变 `removed`，保留原文件、成功页、预览与审核信息。返回删除前实际计数，避免自引用 FK 级联导致 DELETE rowcount 少计。
- 索引排队/执行时拒绝并行删除；OCR owner 行锁与重建状态一致。
- 不允许通过单块 API 直接删 parent，避免自引用级联误删其他 leaf。删除 child 时解除其同 parent 存活子块关联、移除包含旧文本的 parent、修复 prev/next 链。
- 局部删除后文档为 `partial_index`，OCR 索引为 `stale`，后续必须显式重建；最后一块删除则撤除整个文档。
- 这是索引一致性措施，不是原材料数据销毁/法律意义的内容抹除；存活相邻块自身的正常 overlap 也不是自动语义脱敏。
- 本轮**未在真实主库撤除用户教材**。撤除、父子一致性与回滚路径以单测和真实 PG 的自有 UUID fixture 验证，不用破坏真实资料证明测试通过。

主要代码：
- `backend\app\rag\indexer.py`
- `backend\app\services\knowledge_service.py`
- `backend\app\services\ocr_review.py`
- `backend\app\services\ocr_service.py`
- `backend\app\worker\ocr_index.py`
- `frontend\src\pages\KnowledgeDocuments.tsx`
- `frontend\src\pages\OCRWorkbench.tsx`

## 3. 当前真实语料

| 资料 | 实际入库范围 | 活动版本 | 真实非零 leaf 向量 | parent |
|---|---|---:|---:|---:|
| 第二章 并列句 | 完整物理页 1–8 | v2 | 36 | 2 |
| 第二章 名词 | 仅物理页 2、16 | v1 | 12 | 0 |
| 第一章 动词 | 仅物理页 1 | v1 | 4 | 0 |
| 合计 | 3 份资料，不是三本整书 | — | 52 | 2 |

- 向量来自真实 `text-embedding-v3`，1024 维非零；parent 不做 embedding。52 leaf 包含 4 child、48 single，共 54 knowledge_chunk。
- 名词物理页 16 的 `block:15`（family/home/house 定义表）存在疑似源释义对调，人工排除。原图/原生快照保留，不私自修改事实；工作台再次打开时仍勾选该排除块。该表内容不能算作已索引或已证实。
- 名词/动词仍如实保存 `partial_document=true` 与所选范围。**没有自动处理全部 15 份/341 页教材。**
- 并列句真实重建保持文档 ID `fe5db498-4cd5-595b-af6e-af0943325619`，v1→v2，36 个 leaf 重新向量化。
- 主库原 generation_task=3、content_item=4、users=1 不变；ocr_job=4、ocr_page=12 不变；knowledge_document=3、knowledge_chunk=54。

## 4. 多文档评测与失败样本

### 4.1 方法

评测入口：`backend\scripts\rag_eval.py`。

先冻结 gold，再执行真实检索。18 条样本包含 16 条不限制文档/知识点的跨资料检索，以及 2 条英文 alias 的 exact 知识点检索。使用 hybrid、top_k=3、rerank off；仅评测进程将 query expansion 设 off，**没有改运行服务默认 aliases 配置**。

CLI 读取 JSON gold 的 query、相关 document/page、required_terms；限制 1–1000 个唯一 case ID，返回页级 Hit/MRR/相关单位 recall、去重 NDCG、支持上下文关键词与 canonical snapshot hash 检查。同一相关页重复返回不给 NDCG 重复信用。检索只读语料，但会生成真实 embedding 调用和 Trace，不是零成本脚本。

复跑前显式配置现有数据库/供应商环境；从 backend 运行，例如：

```powershell
python scripts/rag_eval.py --gold ../.local-eval/ocr-2026-10-05/index-ops/multi-document-gold.json --output ../.local-eval/ocr-2026-10-05/index-ops/multi-document-rerun.json
```

### 4.2 实测

| 指标 | 结果 |
|---|---:|
| 最终上下文 PageHit@3 | 17/18，94.44% |
| 页级 MRR | 0.916667 |
| 去重页级 NDCG | 0.923941 |
| 相关页单位 recall | 17/18，94.44% |
| 支持上下文 required_terms 覆盖 | 17/18 |
| 返回引用 canonical snapshot hash 校验 | 全部通过 |

Gold 文件 SHA-256：`1a89e589f65efbb637953ad72d50c5571739d885321ba8745aa6efe1858ba2cf`。

**指标是最终拼接上下文的文档/页级结果，不是纯 ANN leaf Recall，也不是答案正确率、整表正确率或人工事实认证。** 单文档旧 8/8 与本次跨资料 17/18 使用不同集和语料竞争条件，不可直接解释为优化收益/退化幅度。

### 4.3 保留的失败与对照

- 问题：“连系动词为什么需要表语？”
- 不限制知识点：top3 返回并列句文档页 3、2、2，未命中期望的动词页 1。
- 独立对照：同问题加 `knowledge_point=verbs`，经 alias 归一为“动词”，命中正确文档页 1。
- 说明显式知识点 scope 可用，但全库候选竞争/排序仍需诊断。**未把 scope 对照混入 gold，未把总体结果改成 100%，也未宣称失败已经修复。**
- 下一轮应先固定失败集并区分向量候选/关键词 lane/RRF/上下文阶段，再做统一策略 A/B；不能给单个问题硬编码路由或用调参后的同集自证泛化。

## 5. 调用与费用边界

本轮索引/重建/18 条评测/1 条 scope 对照共 **26 次真实 embedding**，供应商报告 prompt_tokens 合计 **5146**。Trace 的全局配置 fallback 估算合计 **0.010292**；模型价目/币种/供应商实际账单未核验，不能称人民币/美元实付。没有真实 chat、judge、rerank 调用，也没有新增生成内容。

最后只读 UI 验收不发起付费调用：3 条资料卡、并列句 v2、名词知识点/备注/排除继承、原页加载、pageerror=0。

## 6. 验证与部署

- 本轮新增 9 条非集成 + 1 条真实 PG 专项；最终全量 **659 passed / 26 integration / 633 非集成**，无跳过集成代替通过。18 条既有警告保留（包含依赖弃用和短测试 JWT secret），不是业务错误静默吞掉。
- 回归覆盖显式重建/版本冲突、失败保留旧数据、撤除后复核恢复、parent 不回注删块、拒删 parent、付费重建期间拒删、租户边界、重复调用幂等、页级评测去重。
- 真实 PG 验证同 ID v2 替换、撤除状态/FK/原 OCR 保留；补含 parent/child 的级联删除计数断言。只清理 fixture 自有 UUID，不清业务库。
- 6 个本轮模块 scoped strict mypy、修改 Python black/isort/flake8、TS/Vite 构建通过。限定范围通过不能替代全 app CI 80% 覆盖率门禁；本轮未重新认证该全 app 门槛。
- 最新 backend/生成 worker/OCR worker/scheduler/frontend 镜像已部署原环境，ready 与 Nginx 配置检查通过。宿主 MinerU 沿用原鉴权服务；未新增迁移，主/CI head 仍 `rag_ocr_review`。

## 7. 证据与交接

私有目录：`.local-eval\ocr-2026-10-05\index-ops`（Git 忽略）。

- `additional-indexes.json`、`selected-jobs.json`：新增真实文档。
- `rebuild-live-report.json`、`before-rebuild.png`、`after-rebuild.png`：真实工作台重建。
- `multi-document-gold.json`、`multi-document-evaluation.json`、`evaluation-summary.json`、`linking-verb-scoped.json`：冻结集、原始结果、失败与对照。
- `final-db-and-ledger.log`：语料计数与本轮调用账。
- `final-regression.log`：最终全量回归。
- `final-readonly-ui.json`、`final-index-operations.png`：最终只读界面检查。

全文、图片、认证 state 和私有日志不提交。原有大量未提交 P0/P1/OCR 改动保留；未自动提交、清理环境、撤除真实资料、写外部记忆或重置仓库。

| 收尾事实面 | 状态 |
|---|---|
| 本轮代码与回归 | changed-and-verified |
| 原部署与工作台 | changed-and-verified |
| 现状/计划/任务/变更记录 | changed-and-verified |
| 项目规则 | architecture/llm_calls 同源补充；既有编码/变更规则保留 |
| 外部记忆 | out-of-scope，不读取作为约束、不写入 |
| 工作区 | 有意保留未提交改动与私有验收现场；未执行清场 |

## 8. 后续优先项

1. 无知识点全库漏召回诊断与统一策略 A/B，增加多教材/冲突/表行检索 gold；保留本次失败作为回归。
2. 单租户有界批次重建、非 OCR/legacy 来源重建与版本/配额/存储治理；parser 改变要区分重新 OCR 与仅重嵌入。
3. DOCX/Excel/纯文本等四类固定评测集及表格同行/跨页题组金标。
4. 在明确调用授权与预算下，验证真实生成→judge→人工质检的批量事实质量，而不是以检索命中率代替产出质量。
5. 规模性能/GPU、长期失败恢复/备份演练及许可证分发审查。
