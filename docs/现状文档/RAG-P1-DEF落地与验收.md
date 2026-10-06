# RAG P1 D/E/F 落地与验收

> **2026-10-05 最新验证补充**：已修复 Compose RAG 全参数透传，并直接复用已有 `english-edit-ci-postgres` 实际迁移到 `rag_p1_def`；PostgreSQL专项13项、集成目录21项通过。新增20项单测，非集成506项，全量527项断言通过；本地全app覆盖率约77.45%，80%门槛未过。没有新建隔离环境、清库或付费模型调用，教材未导入，主全栈/真实队列和真实语义质量仍待。详情见 `RAG-部署与PostgreSQL验收.md`。**下文2026-10-04“未运行真实数据库”的描述是历史快照，不再代表所有当前验证状态。**


**日期**：2026-10-04
**状态**：D → E → F 已完成代码级落地；真实部署和检索效果未验收。
**约束**：未启动 Docker，未连接真实 PostgreSQL/Redis，未调用真实 embedding、重排或 LLM 服务。没有实施 P2 G 的运营化重建索引、真实金标/压测和生产准入。

## 1. 实际链路

```text
上传 → P0 结构化解析/切块 → 知识点 canonical/多标签 → 小块 embedding
 → 按章节/页/表格聚合父块 → 全批向量校验 → 文档/父子/邻接关系一次提交

检索 → 固定 tenant/document/source/section/knowledge scope
 → 原查询 + 有界别名/可选 LLM 查询扩展
 → 向量候选 + PostgreSQL 英文 FTS/字面关键词候选
 → 每路阈值/去重 → RRF → 可选/必需 rerank
 → child 命中回溯 parent / 可选邻块 → 位置去重和完整上下文预算
 → 原文片段、匹配依据、来源快照/坐标/版本 → 原有生成与人工来源核验
```

## 2. D：父子与相邻上下文

- 默认新文档超过 1024 字符才考虑父子分组；父块目标上限 2048 字符，小块沿用 P0 512/80。
- 按已通过校验的小块聚合父块，保证每个 child 原文区间完全落入 parent。章节、页码和 table_id 不跨组；单个小块不额外建立无收益父块。
- 父块 `chunk_type=parent`、`embedding=NULL`，只保存原文上下文，**不额外调用 embedding**。child/single 才参与向量和关键词召回。
- prev/next 保存文档内邻接关系，入库先 flush 完所有子块，再绑定邻接指针，避免即时外键校验失效。
- 召回同一个 parent 的多个 child，只注入一次父块，citation 保留 `matched_child_ids` 和原始匹配分数。
- 邻接扩展再次检查 tenant、document、知识点、章节与页；缓存同请求中的重复 lookup。匹配对象优先于可选邻块。
- SQL 扩展失败隔离在 savepoint 内，记录降级并保留有效匹配 leaf；不会凭空返回跨范围上下文。
- overlapping source 区间不重复注入；预算裁剪同步调整 citation.content/content_hash/content_start/end 并标记 truncated/overlap_trimmed。
- 所有 source wrapper、标题/表头、片段间分隔计入字符/保守字节预算。
- 列表/上传 chunks 数仍表示可召回叶子块，不把纯上下文 parent 当作额外出题资料；删除最后一个 child 时清理空 parent 和空 document。

### D 数据字段

`parent_chunk_id`（CASCADE）、`prev_chunk_id` / `next_chunk_id`（SET NULL）、chunk_type、content_hash、search_text、knowledge_point_ids/labels；embedding 改为可空。P0 位置、页码、section_path、parser/chunker/model/version 字段保留。

## 3. E：混合召回、融合与重排

### 候选与索引

- 默认每路候选池 30，最终 top_k=3；二者分别配置，不在初始召回阶段直接截成 3 个。
- 向量查询校验查询向量维度/有限数/非零，过滤已知模型或维度不匹配的记录。历史 NULL-model 行为兼容，但**不能证明它与当前模型兼容**，citation 会标识 legacy。
- 关键词采用 PostgreSQL `to_tsvector/websearch_to_tsquery/ts_rank_cd` 加参数化字面匹配；覆盖中文、语法术语、数字和符号。**这不是 BM25**。
- 关键词 score 是检索顺序派生的 rank score（1/rank），不是余弦相似度或答案正确率；向量/RRF/rerank 分数分开记录。
- 新迁移创建 HNSW cosine、英文 FTS GIN、pg_trgm GIN 和知识点 JSONB GIN；旧 search_text 可回填，**不重新调用模型、不改旧向量**。
- SQLite 仅用于离线参考实现：向量扫描上限 5000，普通字面关键词匹配；诊断标识 sqlite_reference，不把它当作真实 pgvector/FTS/HNSW 效果证据。

### RRF 与失败策略

- RRF：`Σ 1/(k+rank)`，k 默认 60；每路每 ID 只记一次 rank，最后按 score/ID 稳定排序。
- embedding/单路检索失败，健康的其他路可独立提供有效来源。每个 SQL recall route 通过 savepoint 隔离，避免一条坏 SQL 污染后续查询。
- RAG required 要求最终有有效来源，并不等于“必须向量成功”；有效关键词来源可以满足 required。
- degraded/fallback 列表、queries、每路 chunk IDs、fusion/rerank scores、最终引用均可诊断；来源仍为 unverified。

### Rerank

- `RAG_RERANK_MODE=off|optional|required`，默认 **off**。
- 支持 Cohere/Jina 风格 JSON 请求和 `results[index,relevance_score]` 响应；URL/model/key 独立配置。
- 校验完整结果索引、不重复、不越界、score 有限；无效结果 optional 回退 RRF，required 抛错。**required rerank 即使遇到 optional RAG，也不能吞掉错误继续生成**。
- 成功后按 rerank threshold 过滤；阈值下无结果就是无有效来源，不自动降阈假装命中。
- 调用 Trace 记录 model、latency、candidate_count、失败类型。rerank 计费单位未知时明确 unknown，不套用 LLM token 单价、不声称 cost=0 表示免费。
- Trace 持久化失败不作为普通可选增强降级吞掉。

## 4. F：知识点目录、范围与查询扩展

### 配置目录

`backend\app\rag\knowledge_points.yml`，可通过 RAG_KNOWLEDGE_CATALOG 指定其他可信目录。

- canonical_name / id / aliases / parent_id / related / subject / stage / language / status。
- 默认含一般现在时、现在进行时、一般过去时、主谓一致等少量种子配置；**不是完整英语知识图谱**，需按实际教研资料增补 YAML。
- 校验 duplicate ID、别名歧义、父级缺失/循环、关联缺失、禁用标签、长度和扩展范围。
- canonical 和多标签在 embedding 前验证；不能先扣费再发现非法标签。
- 知识点目录是全局公共分类配置，**不代表知识文档共享或授权**。
- 新入库以规范名称和 IDs/labels 存储；旧单标签和已配置 aliases 仍可召回，不强制重嵌入。

### 召回范围

| 模式 | 范围 |
|---|---|
| exact（默认） | 同一 canonical ID/已配置别名，不引入关联知识点 |
| ancestor | 当前节点 + enabled 祖先 |
| descendant | 当前节点 + enabled 子孙；新增明确的子孙模式，不混淆 ancestor 含义 |
| related | 当前节点 + 显式 related 节点 |
| semantic | 不按知识点标签硬过滤，但 tenant/document/source/section 权限和限制仍生效 |

最大扩展范围默认 128 个节点，超出拒绝，不偷偷截断或扩大范围。

### Query expansion

- 默认 `aliases`：原查询始终保留，可加规范名和配置别名，最多 4 个变体；没有 chat LLM 调用。
- `off`：只保留原查询；标签 canonical 匹配仍由 scope 层负责。
- `llm`：使用两个独立 `.st` prompt + Pydantic 严格 JSON 验证，每条替代 query≤128 字符、数量有界；输出 tokens/超时/SDK 重试有上限。失败回退原查询/别名。
- LLM 返回的 query **只能改检索文本，不能改固定的 scope**；无论返回什么，不得跨租户、document 或知识点范围。
- Prompt 文件版本 hash、输入 query/point、合规输出、usage/cost、latency 和失败均入 Trace；这是一项单步结构化转换，没有复制 WeKnora 完整 Agent/Skill/Chat 运行时。

## 5. API 与前端

- 上传 JSON 增加 knowledge_points，多标签；multipart knowledge_points 使用 JSON 字符串数组，primary knowledge_point 兼容。
- GET `/api/knowledge/points`：公开分类与默认策略（不暴露 provider keys），ops:read。
- GET `/api/knowledge/retrieve`：query、knowledge_point、scope_mode、top_k、document_ids、source_name、section_path；诊断包含所有阶段与降级。
- preview 展示父子组和小块；列表只展示 leaf；React 支持多标签、别名选择、scope/source 过滤和 retrieval/citation 诊断。
- 配色/布局使用原 App.css，原文与诊断以 React 文本输出，不执行源 HTML。

## 6. 迁移与默认配置

**新单 head**：`rag_p1_def`，上一 head `rag_p0_abc`。完整升链 603 行离线 SQL、本次降级 48 行离线 SQL 已生成，**没有对真实库执行**。

上线前必须备份并执行 Alembic。新增 HNSW/pg_trgm/GIN 需要实际数据库具备相应扩展与权限；建索引和回填可能占用锁/资源，不能直接承诺零停机。development create_all 不能替代这次迁移及索引创建。

降级先断开 parent/neighbor 指针，只删除本功能的无向量 parent，保留叶子正文/向量；若存在异常无向量叶子，恢复 NOT NULL 会失败而不是删除它。降级不 DROP 共享 extension。生产回退前仍须备份新快照。

| 默认策略 | 值 |
|---|---|
| Parent-Child | enabled；min 1024，parent 2048；child 沿用 512/80 |
| 邻接 | 前后各最多 1 个；最多 24 个扩展对象 |
| Context budget | 8192 字符；可选 UTF-8 字节保守预算 |
| 召回 | hybrid，pool=30，top_k=3，RRF k=60 |
| Rerank | off；optional/required 显式开启并配置 model/URL/key |
| 知识点 scope | exact，max nodes=128 |
| Expansion | aliases，max queries=4；LLM 模式显式开启，输出预算 512 token、timeout 8s、max_retries=0 |

新功能只改新入库布局。旧文件不会自动拥有父子块、多标签或修复历史丢表格；需要原文件重新导入或等待 P2 的受控 reindex 工具。

## 7. 验证证据

- 新增 **65 条**纯回归，D/E/F 定向 65 passed。
- 全量非集成 **486 passed / 8 deselected**，原有 P0/质量/状态机/来源门控回归保持。
- P1 相关 130 条定向回归，限定模块 coverage **95%**（不是全 app ≥80% CI 证据）。
- 新/改动 RAG + KnowledgeService 共 22 个文件 scoped mypy 通过；修改代码 Black/isort/flake8 通过。
- 前端 TypeScript + Vite build 通过；未做真实浏览器端到端。
- 单 Alembic head 和离线升降级 SQL 通过；未证明真实 FK/index/query-plan/并发效果。
- Fake provider 覆盖无效 JSON、漏/重复/越界 rank、NaN、optional fallback/required fail closed、计费/Trace、scope 不被 LLM 变体扩大、源位置/预算/去重/跨租户拒绝。

## 8. 不应宣称

- 已验证真实 pgvector ANN 计划、Recall@K/MRR/NDCG、供应商模型收益或规模性能。
- PostgreSQL FTS/substring 就是 BM25。
- RRF 分数、keyword rank score、rerank score或来源命中率等于事实正确率。
- query expansion 可自动建立完整知识点图谱、自动标注文档。
- PDF/OCR、复杂排版、表格视觉还原已解决。
- 源程序与 WeKnora 全量行为等价、已经完成 P2 运营治理与生产准入。
