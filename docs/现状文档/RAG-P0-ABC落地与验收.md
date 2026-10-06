# RAG P0 A/B/C 落地与验收

> **2026-10-05 最新验证补充**：已修复 Compose RAG 全参数透传，并直接复用已有 `english-edit-ci-postgres` 实际迁移到 `rag_p1_def`；PostgreSQL专项13项、集成目录21项通过。新增20项单测，非集成506项，全量527项断言通过；本地全app覆盖率约77.45%，80%门槛未过。没有新建隔离环境、清库或付费模型调用，教材未导入，主全栈/真实队列和真实语义质量仍待。详情见 `RAG-部署与PostgreSQL验收.md`。**下文2026-10-04“未运行真实数据库”的描述是历史快照，不再代表所有当前验证状态。**


**日期**：2026-10-04
**状态**：A → B → C 已完成代码实现与离线/单元回归；真实部署未验证。
**范围约束**：没有启动 Docker，没有连接真实 PostgreSQL/Redis，没有付费模型调用。未落地 P1 Parent-Child、混合检索、rerank、知识点层级/别名。

## 1. 阶段交付

| 阶段 | 交付 | 验收边界 |
|---|---|---|
| A：基线与兼容 | 保留旧 string parser/chunk API、旧 chunk list/delete/citation、tenant 与 knowledge_point exact filter、RAG 模式；来源清单/MIT；nullable 增量迁移 | 不回填、不重嵌入旧向量；真实迁移未运行 |
| B：解析 | Structured Blocks；DOCX 正文+表格顺序；Markdown/HTML 表格；PDF 页码/空页/失败 warning；XLSX/CSV sheet、列、真实行号 | 图片未 OCR；PDF 未还原布局；复杂 HTML spans 保留快照并告警；Excel 公式保留文本未求值 |
| C：切块 | heading/heuristic/recursive/legacy、auto profile、每层内容/预算验证与 fallback；标题/表头上下文；源坐标；长度/overlap 护栏；diagnostics | 超大原子单元仍可受控硬切；结构边界短块明确告警；不声称语义永不切断 |

## 2. 数据与迁移

新增迁移：`backend\alembic\versions\rag_p0_abc.py`。

- 上一 head：`p1_09_trace_ledger`；新单 head：`rag_p0_abc`。
- 新表 `knowledge_document`：租户、source/content hash、规范化文本、可用时原始文本、block 快照、parser 版本、warning、stats。
- 新 chunk 可空字段：document_id、chunk_index、content_start/end、page_no、context_header、section_path、parser/chunker version、embedding model/dimension/input hash。
- `meta` 继续保留格式-specific locator、table_id、row_range、诊断，不污染题目输出 Schema。
- 老数据不会被删除或重嵌入，NULL document/version 字段按 legacy 处理。
- 删除最后一个新文档 chunk 时清理该文档快照；旧 chunk 删除行为保持。
- 文档快照不是 DOCX/PDF/XLSX 二进制备份，不能代替原文件保管。
- downgrade 保留旧 content/vector/meta，但会删除新文档表和增量字段，生产回退前应备份这些新快照。

**部署注意**：上线新代码前，备份数据库并执行 `alembic upgrade head`；此处只生成了 SQL，没有对真实 DB 执行。production/staging ready 检查会要求新 head；不得把旧 schema 与新 ORM 混用。

## 3. 原文、上下文和坐标契约

```text
Document.normalized_text[start:end] == Chunk.content
EmbeddingInput == ContextHeader + '\n\n' + Chunk.content（有 header 时）
```

- 坐标以规范化文本的 Python Unicode 字符数为单位，不是原文件字节偏移，也不是 PDF bbox。
- `ContextHeader` 是独立上下文：章节路径，以及后续表格块的表头。重复表头不加入原文坐标。
- citation 保留原始 chunk snapshot/hash，同时追加文档、页码、章节、表格行范围和版本；来源状态仍为 unverified。
- 校验以源区间并集计算非空字符覆盖率，不用所有 chunk 长度求和代替覆盖率。
- 相同正文在不同位置出现时不去掉必要内容；不得只凭文本相同删除 chunk。
- 表头无法在预算内完整容纳时显式告警并保留原文；上下文标题连一个正文字符都容不下时失败关闭。

## 4. 配置

环境变量都位于 `backend/app/config.py` 与两个 `.env.example`：

| 配置 | 默认值 | 含义 |
|---|---:|---|
| RAG_CHUNK_STRATEGY | auto | auto/heading/heuristic/recursive/legacy；只作用于新入库 |
| RAG_CHUNK_SIZE | 512 | 完整 embedding 输入的字符预算（含 context_header） |
| RAG_CHUNK_OVERLAP | 80 | 相邻正文切片重叠；最高限制为 size/2 |
| RAG_CHUNK_MIN_CHARS | 80 | 防止重复边界小片段；页、表和章节边界不能强行合并时告警 |
| RAG_CHUNK_TOKEN_LIMIT | 未设置 | 可选完整输入 UTF-8 字节保守预算，非供应商精确 token 数 |
| RAG_EMBEDDING_BATCH_SIZE | 32 | 分批向量化；全部批次校验通过后一次事务写入 |

现有 RAG_MODE/RAG_MIN_SIMILARITY、模板来源必审/人工门控保持原行为。Embedding batch size 必须按实际供应商限制调整；默认值不代表供应商已经实测兼容。

## 5. API 与前端

- `POST /api/knowledge/preview`：multipart file；ops:write；解析并切块，不 embedding、不写知识表。
- `GET /api/knowledge/{chunk_id}/diagnostics`：ops:read + 资源 tenant scope；旧记录返回 legacy=true，新记录显示文档/block/诊断快照。
- 原上传响应保持 chunks/source_type/source_name/knowledge_point，并追加 warnings/diagnostics。
- 原列表增加 nullable 来源/版本信息；旧客户端字段保持兼容。
- React 知识库页面支持新格式、知识点标签、无模型调用预览、解析 warning 和规范化原文区间展示，使用既有 App.css 令牌。

原文/HTML 快照只以 JSON/React 文本展示，不作为 HTML 执行。

## 6. 测试证据

### 非集成回归

在 `backend`：

```powershell
python -m pytest -m "not integration" -o addopts="-q"
```

**421 passed / 8 deselected**。项目 `backend/.venv` 补齐已有 requirements 中缺失的解析、认证、multipart、psycopg/checkpointer 依赖后，也完成同一结果的非集成回归；安装依赖未运行真实数据库或模型。相比 P1 末尾 365 passed，新增 **56 条** RAG P0 回归（8 条基础 + 48 条契约测试）。测试包括：

- DOCX body-order、空列、合并单元格、pipe 转义。
- Markdown/HTML 表格、表头切换、真实 row_range、超长行。
- 真实内存 PDF 的两页文本/空页、模拟 page extraction 失败。
- 真实内存 XLSX 的 sheet、空行、日期、公式、合并和空 sheet。
- 换行病态、长句、中英文 token 保守预算、代码/公式、策略耗尽/fallback。
- 100 组固定 seed 的随机文本覆盖和预算检查。
- embedding 部分返回、跨批失败不入库、DB commit 失败全量 rollback。
- 旧 NULL-version chunk 列表/引用/删除，tenant 诊断隔离。
- API 预览/上传/诊断、viewer 写权限拒绝；无真实 DB/模型。

### Scoped coverage

RAG P0 相关 **86 passed**；覆盖 `chunking`、`parser`、`document`、`tables`、`indexer`、`knowledge_service`，**约 91%（限定模块）**。这是限定模块覆盖率，**不是全 app ≥80% 的 CI 证据**。

### 静态、构建与迁移

- 修改代码 Black/isort/flake8 通过。
- 新/改动 RAG + KnowledgeService 共 10 个源码文件 `mypy --follow-imports=silent` 通过；全 app strict mypy 历史问题不在本次范围。
- 前端 `npm run build`（TypeScript + Vite）通过；未进行真实浏览器端到端验收。
- Alembic 单 head 通过；全链 `upgrade head --sql` 和 `rag_p0_abc:p1_09_trace_ledger` 离线 downgrade SQL 通过；无真实 schema/DML 执行。

## 7. 来源与许可

详见 `docs\现状文档\RAG P0移植来源与许可证.md`。

WeKnora 本地 VERSION=0.8.0；该目录继承父目录 Git，**未核实上游 commit**，因此记录逐文件 SHA-256 而不冒称 parent commit 为上游版本。本轮按当前 Python 架构移植算法契约，不复制整个服务。版权及 MIT 全文保留于 THIRD_PARTY_NOTICES.md / licenses/WeKnora-MIT.txt。

## 8. 仍然不能声称

- WeKnora 全量功能或行为等价。
- PDF/扫描教材/复杂排版 OCR 已可靠解析。
- 表格/公式永远不会切断。
- 知识点别名、层级、自动标签已经实现。
- 大规模 pgvector ANN 性能或真实召回率达标。
- Chunk 覆盖率 100% 等于检索召回率/题目正确率 100%。
- 真实 PostgreSQL、Redis、Celery、供应商 embedding/LLM 的生产准入已完成。
