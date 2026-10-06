# OCR-4 工作台、审核入库与真实 RAG 验收

**日期**：2026-10-05
**对应记录**：OPT-066
**结论**：OCR工作台与审核后的真实embedding/知识入库链路已部署原环境，并用一份完整8页教材实际跑通。不是mock向量；不是所有教材/内容生产质量已经达标。

## 1. 本轮完整链路

`工作台提交 PDF/受控路径批次 → 后台OCR与页检查点 → 原页图像/识别块/表格/切块核对 → 显式审核及费用授权 → 后台真实embedding → 原子入库 → 知识点/别名检索与来源引用`

前端入口为现有知识库页面的“扫描 PDF · 后台 OCR 与审核入库”。原同步上传10MiB限制仍不变；后台OCR128MiB和独立队列继续复用。

### 工作台能力

- 提交文件、配置目录相对路径批次和物理页选择；列表分页、实时进度、缓存页数、失败与尝试次数、取消及续跑。
- 逐页显示鉴权原图，识别段落/标题/表格逻辑网格；点击块显示bbox定位。
- 定位框只有引擎页面尺寸与原页匹配时才显示，缺失/不匹配时不伪造坐标。**尺寸匹配不是所有旋转/crop/坐标原点都已被证明正确**，定位仍需核对。
- 用户可排除疑似错误块并重新预览切块，原PDF和原生快照保留，不擅自改写教材事实；排除后也可恢复原块。
- 原页确认、表格/词边界/顺序告警确认和真实embedding费用确认全部满足后才允许提交；仅选部分页另需声明“仅索引所选页”。
- 可配置资料类型、知识点标签、审核备注；已入库按钮禁用，重复提交不再次调用模型。只读角色不能触发写入操作，服务端仍强制权限/tenant scope。

## 2. 审核与付费任务约束

新增单head `rag_ocr_review`，下接 `rag_ocr_jobs`，只在ocr_job增加审核及索引状态字段，旧数据保留。迁移前主库/CI均已备份。

- `review-plan`是无模型调用的当前切块预览；preview_hash绑定原预览，plan_hash绑定排除块、模型/维度、切块/父块配置、实际embedding输入和taxonomy版本。
- 后端必须收到原页/告警/费用声明；hash或配置改变则要求重新审核。声明是**用户确认记录**，不是代码可以自动证明用户真的逐字阅读或事实正确。
- 部分页索引保留 partial_document=true，另记录reviewed_index_scope=selected_pages，不能通过改成false来冒充整书。普通文本/文件上传对未审核partial的拒绝门控保留。
- 后台索引使用稳定文档ID、SQL条件领取和租约；知识文档/chunks与job indexed状态在同一事务提交，重复点击或重复消息不会重复付费。
- 供应商异常、worker失联或费用不确定进入needs_attention，**不自动重放付费尝试**。用户需核对Trace并显式授权重试；这不是跨供应商严格exactly-once保证。
- 所选块过滤重新计算规范化offset/hash，同时保留源locator和原始快照。知识点catalog补充“并列句/compound sentence(s)”归一映射，未实现自动全知识图谱抽取。

## 3. API / 实现位置

在已有后台OCR API上追加：

| 路径 | 用途 |
|---|---|
| GET `/api/knowledge/ocr/jobs/{id}/review` | 完整审核计划 |
| POST `/api/knowledge/ocr/jobs/{id}/review-plan` | 按人工排除块重算计划，免费本地预览 |
| GET `/api/knowledge/ocr/jobs/{id}/source-pages/{n}` | 鉴权/范围/hash校验后返回原页PNG |
| POST `/api/knowledge/ocr/jobs/{id}/approve-index` | 显式审核与费用授权，202后台执行 |

- `frontend\src\pages\OCRWorkbench.tsx`：复用ERP/单色令牌的工作台，安全React文本/表格展示，不执行OCR HTML。
- `backend\app\services\ocr_review.py`：计划/hash/排除/确认门控。
- `backend\app\worker\ocr_index.py`：付费任务、稳定ID与原子写入；原indexer默认行为保留，增加可选document_id/commit=False供同事务使用。
- `backend\app\rag\ocr\render.py`：有界PDFium渲染，进程内串行锁，最多1600px边；依赖PDFium与Pillow均显式声明，不将OCR权重放进API镜像。
- embedding新增可配置provider batch cap，默认10；本次36个输入拆为10/10/10/6，模型与维度仍来自原配置，没有硬编码供应商模型名。
- 引用增加source_reviewed与source_review_job_id；verification仍保持unverified，**原文/OCR核对不等于知识事实已认证**。

## 4. 真实教材与入库数据

实际资料：用户原目录 `一、句法篇/第二章 并列句.pdf`，完整物理页1–8（书内印刷页33–40），没有只取样本冒充整书。

- 原8页图像均通过受保护API渲染并在真实工作台切换/核对；页面尺寸校验8/8通过。原文件未修改。
- 已核对定义、并列/选择/条件/因果/转折、例句、练习与答案；仍观察到章标题重复/练习小标题等OCR结构噪声，保留告警与审核备注，不宣称完美语义恢复。
- 工作台显式勾选确认后走真实Redis索引任务，状态indexing→indexed；不是调用测试mock或手写数据库向量。
- 实际知识文档ID：`fe5db498-4cd5-595b-af6e-af0943325619`。
- 一份KnowledgeDocument，36个有向量叶子（32 single + 4 child），2个无向量parent，共38个KnowledgeChunk。
- 配置模型text-embedding-v3，实测1024维；36个向量L2范数约0.99999987–1.00000018，全非零且finite。父块无embedding。
- 文档保留原文件hash、原生OCR快照、所有物理页、警告、解析/切块版本、审核来源和知识点标签。

主库验收后：generation_task3/content_item4/users1仍不变；ocr_job4/ocr_page12；knowledge_document1/knowledge_chunk38。增加的8页任务和真实索引有意保留，不清库、不删除原任务/用户。

## 5. 真实检索评测

评测先按源页定义8条页级问题，覆盖定义、neither/nor、either/or、for/because、转折、or警告句、因果差别及练习题。

- 配置：hybrid（真实pgvector + keyword/RRF）、top_k3，rerank off；为隔离本轮基础召回，评测进程临时query expansion off，**没有更改运行服务全局aliases默认值**。
- 只在这一已索引文档内评估，知识点exact；两条使用英文别名输入，实际归一到“并列句”。
- 8/8问题最终上下文前三项包含预期原页；第一条预期页均排名1，页级MRR@3=1.0。
- 文本与canonical JSON hash_value约定对应，24个返回引用snapshot hash均核对通过。它与存储原文的raw UTF-8 SHA256不是同一编码约定，不能混用校验。
- 某些结果是命中child后回溯的parent，指标明确是**最终上下文页命中率**，不是纯ANN叶子Recall@K，也不是全库/所有知识点召回率。
- 工作台随后又用真实API检索for/because并查看引用：命中物理页4，source_reviewed=true，verification=unverified，浏览器无pageerror。
- **不能由单文档8条得到“大规模RAG质量100%”或“生成答案事实正确率100%”的结论。** 未调用真实chat/judge生成内容。

## 6. 实际调用与费用口径

用户本轮明确授权真实embedding。最终Trace记录本轮14次成功调用：1预检、4入库批次、8页级评测、1工作台检索；累计供应商报告prompt_tokens=3642。

- 36个文本入库输入全部来自本次实际OCR/切块，无零向量mock；查询也使用真实embedding。
- 根配置没有text-embedding-v3的模型价目表，当前Trace总配置估算为0.007284（全局fallback单价计算）。**该金额的币种/供应商实际单价/账单未核验，不能把它写成已实际支付的人民币或美元。**
- 成功embedding的Trace输出增加cost_basis=global_fallback_unverified / provider_bill_verified=false；工作台未配置模型价格时不承诺金额。
- 真正费用以用户供应商控制台账单/优惠额度为准，本轮未读取或伪造账单。其余教材没有自动向量化。

## 7. 验证、故障发现与修复

- 新增16非集成+1真实PG（较OPT-065新增17）；全量 **649 passed**，含25真实集成；限定103相关回归 coverage **89.96%**（review/index/render/indexer），不是全app/CI80%门禁。
- 修改Python Black/isort/flake8，6个限定模块strict mypy与TypeScript/Vite build通过；原Compose实际镜像重建/迁移、ready/代理/nginx可用。
- 真实Playwright：登录→知识库→后台提交→完整OCR→8页原图/块定位→确认按钮未勾选禁用→真实审核入库→已入库禁用→排除/恢复预览→真实检索和来源。最终pageerror为0。
- 真实Docker渲染首次暴露Pillow未声明（宿主测试环境已装Pillow掩盖），已补直接依赖并在实际镜像重验；不把宿主单测通过当容器可用。
- PDFium也暴露旧synthetic PDF fixture的直接stream不规范（pypdf宽容读取而PDFium回退到Letter页面）；测试fixture改为合法indirect stream，真实教材不改。
- 测试覆盖权限/tenant、所有审核门控、hash/config漂移、部分页说明、排除块不embedding、重复消息、无部分知识写入、费用不确定不重试、生产依赖和真实PG同事务等。

所有教材识别全文、原页/界面截图、原数据库备份、浏览器认证state及评测raw只在 `.local-eval\ocr-2026-10-05\ocr4`，被Git忽略；报告不附整书OCR文本或密钥。

## 8. 当前可用范围与仍待事项

现在可以从工作台完成小批资料的OCR、人工核对、显式向量入库及检索引用，已具备个人使用的实际资料闭环。但不等于所有批量内容生产质量已验收：

- 其他14份教材/复杂跨页表格/全部题组与源错误处理仍需逐批核对；本轮没有自动处理全部341页或索引全部文件。
- 章节重复、练习小标题、词边界/分页、旋转/crop的定位等仍有局限；可排除问题块/写备注，但未做全能自动修正或知识图谱抽取。
- 批量知识运营（索引删除后的修复/重建、长期文件配额与清理）、全库Recall/NDCG、多教材冲突与真实生成/judge/人工质量金标仍待。
- 宿主MinerU不是自动开机服务；重启需按部署脚本启动。GPU/规模吞吐/全面故障演练/对外分发许可审查不在本次验收结论内。
- 人工确认和source_reviewed不是自动事实认证；真正教研产出仍需来源核验与人工质检。

**当前交付状态：OCR-4已部署，真实embedding/入库/检索已验收；不是全项目无风险或全量内容质量已证明。**
