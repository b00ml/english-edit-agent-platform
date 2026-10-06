# RAG P0 移植来源与许可证清单

**日期**：2026-10-04
**源目录**：`<本地WeKnora源码目录>`
**本地 VERSION**：0.8.0
**上游 commit 状态**：未确认。该目录没有独立 `.git`；此前读到的 `128b3224…` 属于父目录 `<本机开发目录>`，不得作为 WeKnora 上游 commit。采用下列 SHA-256 固定实际参考源码。

## 采用范围与方式

| 本项目落点 | 本地 WeKnora 参考文件 | 采用方式 |
|---|---|---|
| `backend/app/rag/document.py`、`parser.py` | DOCX/Excel parser | Python 结构模型、body-order 和行/列语义；不复制 monkey patch、图像或并发服务 |
| `backend/app/rag/tables.py`、`chunking/strategy.py` | splitter、header_tracker | 转义 pipe、表头上下文、预算内按完整行分组，超大行显式降级 |
| `backend/app/rag/chunking/models.py` | Chunk/ContextHeader 配置 | 原文坐标与零宽上下文分离；offset 指向规范化文本，不是 PDF 页面视觉坐标 |
| `backend/app/rag/chunking/validator.py` | validator、strategy | 校验原文非空字符覆盖、顺序、预算；失败进入下一策略，耗尽失败关闭 |
| `backend/tests/test_rag_p0_abc.py`、`test_rag_p0_contracts.py` | DOCX/切块测试场景 | 重新构造本项目 fixture，不复制第三方测试资料 |

**差异必须保留**：本轮不是 WeKnora 全量代码或行为等价移植。尚未包含 Parent-Child、关键词/RRF/rerank、OCR、PDF 布局解析。token 预算采用完整 embedding 输入的 UTF-8 字节保守预算，不是供应商精确 tokenizer。WeKnora 未在本轮启动或跑测试，不称“在本项目验证过的完整上游实现”。

## 许可证处理

- 本地主体 LICENSE 声明 MIT；版权及许可原文保留于 `licenses\WeKnora-MIT.txt`。
- 项目级声明：`THIRD_PARTY_NOTICES.md`。
- 本轮未复制 Go 运行时、OCR/模型资源或其他第三方组件，不把主仓 MIT 覆盖其全部依赖。
- `python-docx`、`pypdf` 为已有解析依赖；新增 `openpyxl>=3.1.5` 用于 XLSX，按其发行包许可证管理。
- 后续任何直接复制须逐文件核对源版本、许可和修改范围，并更新本清单。

## 本地源码指纹

| 源文件（相对 WeKnora-main） | SHA-256 |
|---|---|
| `VERSION` | `a66780da23103beaf12432b418508966b31a2a3a787f9468a5b6cdaa8667c1ef` |
| `LICENSE` | `25cfeca2c3eee245313b1106cf1b27244e47b682802a02cc536f8f1beb6a29a6` |
| `THIRD_PARTY_NOTICES.md` | `9352de8c0f6219562bd89e1641283c28e44e9e11f5e3af23c2e2a834e1bb0b6f` |
| `docreader/parser/docx_parser.py` | `c5ad355a202f56d99272afb4e6fbcbbf9a169b51c36fcfe3c7c19262ef30eb7b` |
| `docreader/parser/excel_parser.py` | `8acdb16ccd5a81dfd656aec6ac380d24379bc276cba739ac217f854d554db1b5` |
| `internal/infrastructure/chunker/strategy.go` | `bbc6e821c7107558e1d9adee7701ec3996d6ae1e491dd944e578b3ef32cecd6e` |
| `internal/infrastructure/chunker/splitter.go` | `82952d830f0a27388a4ef78b683a9309594b73cfb1a5fe9de5e413fd7daaee46` |
| `internal/infrastructure/chunker/header_tracker.go` | `95b1f7fb058c87f5ce65d1dc494e66a330792d79154bd4bb0aa105b77c685d1f` |
| `internal/infrastructure/chunker/validator.go` | `81c0e26819240cb817ad152a78a5f62273797868c776c4050baf1200de954463` |
| `internal/infrastructure/chunker/tokens.go` | `f64d54d7a329db1a1b1822622c12f8655cc4ec0938683de71b31b6c6aaaed847` |
| `docreader/tests/test_docx_tables.py` | `611ffb2ee6a9d62ece3fee3a9cb758629b6bd633d912dbce555f05c7bc1e5fab` |
| `internal/infrastructure/chunker/splitter_test.go` | `b2dca9115f86692dc17d10e592c5d8d0298250280d743ef324bd65757379d081` |


## P1 D/E/F 追加（2026-10-04）

P0 上述“未包含 Parent-Child/关键词/RRF/rerank”为当时快照；P1 现已以本项目 Python 数据契约移植这些设计。

| 本项目 | WeKnora 参考 | 差异 |
|---|---|---|
| chunking/parents.py、indexer.py、context.py | strategy.go / knowledge_process.go / search results | 从已校验 children 按源跨度聚合 parent，非 Go 服务整体复制 |
| retrieval/fusion.py | knowledgebase_search_fusion.go | ID 去重、rank map、RRF；本阶段等权配置，不声称完整 upstream 算法等价 |
| retrieval/rerank.py | internal/models/rerank/jina_reranker.go | Cohere/Jina JSON adapter、完整结构校验、optional/required 与 Trace |
| retrieval/expansion.py、knowledge_points.py | search pipeline 的 query expansion/scope 设计 | YAML 教研 taxonomy、独立 .st 与 Pydantic、固定 tenant/point scope；并非上游自动知识图谱 |

上游主体 MIT 原文/版权继续保留；无 Go 运行时、第三方模型/服务复制，未启动 WeKnora。新增参考源码 SHA-256：

| 文件 | SHA-256 |
|---|---|
| `internal/application/service/knowledge_process.go` | `f6d99a7c79b3f95ac7965e046d932c669da6218923bec372951e72ae8adec64a` |
| `internal/application/service/knowledgebase_search_fusion.go` | `a4a56ac1d8531692ca9a83b2ce71a703509c470b59ae3349043fda633843d498` |
| `internal/application/service/knowledgebase_search_results.go` | `2c5b5074ed8a756ef9b07edd1fc99228dc2d7147a52ff24a7159c85c3da3a8dd` |
| `internal/models/rerank/jina_reranker.go` | `a7a5cbc08502b97f723b954c7cdd7b2eddf657758b39b3028e62b2f5e64a383a` |
