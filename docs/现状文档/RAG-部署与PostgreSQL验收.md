# RAG 部署透传与 PostgreSQL 集成测试修复验收

**日期**：2026-10-05
**范围**：修复启动前的两项缺口：Compose RAG 配置透传、真实迁移/非零向量/数据库专项测试。
**环境**：直接复用已有 `english-edit-ci-postgres`（已有 `english_edit_test` 数据库及原数据卷），未新建隔离环境，未清库。API/worker/frontend/Redis 全栈未启动，教材尚未导入。

## 1. 修复内容

### Compose

文件：`deploy\docker-compose.yml`。

- 新增 `x-rag-environment` YAML anchor，backend/worker 同时合并引用。
- 透传当前 Settings 中全部 RAG_* 配置和 EMBEDDING_DIM/EMBEDDING_TIMEOUT：切块、父子块、候选池、context budget、rerank URL/model/key、知识点目录/scope、query expansion。
- 不改密钥的来源要求，不写入示例 key 到真实 .env；默认值与 Settings 对齐。
- `RAG_CHUNK_TOKEN_LIMIT`、`RAG_CONTEXT_TOKEN_LIMIT` 在 Compose 未配置时会成为空字符串；新增 before validator 转为 None，避免容器启动 ValidationError。
- 单测执行实际 `docker compose config --format json`，确认非默认配置在两服务中一致，并验证空值的 Settings 类型转换。这个测试只做 config 渲染，不启动服务。

### 集成装配

文件：`backend\tests\integration\conftest.py`。

- `_pg_ready` 实际运行 `alembic upgrade head`，查询数据库实际 revision 并比对代码 head；不再用 `create_all` 代替迁移，不失败后偷偷 stamp。
- Embedding stub 改为确定性的词项 hash 向量：1024 维、单位范数、有限数、非零，输入变化时向量有差异。
- 专项排序测试另外使用已知相似度的精确/近似/正交向量，直接断言 PostgreSQL 余弦排序及阈值，不能仅靠 hash 向量证明语义正确。
- 基础 RAG 集成测试一律禁用付费 rerank/LLM query expansion；其他生成/质检用脚本化 fake provider，不调用真实模型。
- 新增 `pg_session`/API savepoint 会话：沿用同一数据库，测试只回滚自己写入的数据。移除旧 pipeline 的 TRUNCATE；临时用户/模板用 UUID，不依赖现有管理员密码，不覆写现有灰区模板。
- 修正 Celery shared_task 代理的 inline fixture，让同步测试不会因 current-app 切换误发真实 broker；这不等于真实 Redis/Celery 分布式验收。
- 同进程 MemorySaver pipeline 显式设置允许内存，staging 启动仍校验 Alembic head、不运行 create_all；持久化证明由独立的跨进程 PostgresSaver 三项测试承担。
- 跨进程测试清理仅其自身 UUID task/template/thread，不删除已有业务/checkpoint。

## 2. PostgreSQL 专项断言

新文件：`backend\tests\integration\test_rag_postgres.py`，**13 项通过**：

1. 实际 Alembic head、vector/pg_trgm 扩展、索引有效性及 parent/prev FK。
2. PostgreSQL 余弦排序、阈值，纯上下文 parent 不进向量召回。
3. 真实 EXPLAIN 中 HNSW 可用。
4. 真实 EXPLAIN 中英文 FTS GIN 可用。
5. 真实 EXPLAIN 中 trigram GIN 可用。
6. 真实 EXPLAIN 中标签 JSONB GIN 可用。
7. 英文 runs→running 词干、中文、符号/数字、LIKE 字符转义。
8. JSONB 多标签/别名和 NULL/外租户过滤。
9. 真实父子 FK、源坐标、parent 去重、wrapper/字节预算。
10. 各 recall lane 真正执行 PostgreSQL，不能让 SQL 错误被 fallback 掩盖成绿灯。
11. 人为坏 SQL 后 savepoint 回滚，健康关键词 lane 继续满足 required RAG。
12. 部分 embedding 返回不写文档/块。
13. 删除 child 后 parent/邻接 FK/document 清理。

**计划检查的口径**：索引 EXPLAIN 测试在事务内 `SET LOCAL enable_seqscan=off`，只证明索引可用；不声称 tiny fixture 上默认 planner 选该索引，更不证明大规模耗时/Recall@K。

## 3. 实际使用旧库与迁移审计

- Docker 原有两个相关 Postgres：`english-edit-test-pg`（55432，老 ORM 表，无 alembic_version）和 `english-edit-ci-postgres`（5433，有 revision）。短暂检查前者后恢复停止，仅使用后者。
- 验收库原版本 `m3_03_model_hash`，现已实际升级至 `rag_p1_def`；不是只生成离线 SQL。
- 迁移首次被历史 tenant 检查阻断：只有两条 queue_item Trace 是 `tenant_id=default`，对应 task/content 都是 NULL 默认租户。
- 已核对 task/template（single_choice_hr）和记录链，再备份、仅将这两条 Trace 归属改为其 task 归属；没有跳过归属校验或给 migration 加自动宽松分支。
- 两条记录 ID：`44fbf536-ec24-47ef-a6d0-12ac0574d949`、`02d6af0b-f2dc-4d3f-8ad2-c230be6d343d`；实际 UPDATE 2。
- 备份：`<本机私有目录>`（243,668 bytes，含已有数据库信息，仅本机保存，不进 Git）。
- 原库有 1 个 task、1 个 content、1 个 user、0 个 RAG chunk。测试后这些数量保持；新增 document 表 0 行。未把 mock 教材/向量留在个人库。
- 实际数据库 PostgreSQL 16.14、pgvector 0.8.5；HNSW/FTS/trigram/tag 索引全部存在且 valid。

## 4. 回归与门禁

| 项目 | 结果 | 边界 |
|---|---|---|
| 配置/stub/迁移装配单测 | 20 passed | 含真实 Compose 渲染，但不证明服务运行配置 |
| 新 PostgreSQL 专项 | 13 passed | 真实 PG + fake embedding，无付费 API |
| 集成目录 | 21 passed | 13 RAG + 5 pipeline + 3 跨进程恢复 |
| 全量非集成 | 506 passed / 21 deselected | 相比前轮 486 新增 20 |
| 全量含真实集成 | 527 passed | 20 单测 + 13 集成新增，原 8 项现实际运行 |
| 修改代码 Black/isort/flake8 | 通过 | 不宣称全工作区历史副本卫生已修复 |
| 全 app 覆盖率门槛 | **未通过** | 本地测得约 77.45% < 80%，测试断言通过不等于 CI 通过 |

全量 coverage 会统计本地未跟踪/忽略的 routes_original.py、routes_refactored.py、tasks_backup.py，还报告 routes_step1/2 解析失败；这些历史副本在正式入口之外，本轮未删文件、未降低 80% 门槛或用排除规则粉饰。正式 checkout 的 CI 覆盖率仍需独立证据。

## 5. 可复跑入口

在 `backend`，把 TEST_DATABASE_URL 指向现有 PostgreSQL（密码使用本机配置，不抄进文档）：

```powershell
# $env:TEST_DATABASE_URL = 现有 PostgreSQL SQLAlchemy URL
.\.venv\Scripts\python.exe -m pytest tests/integration -o addopts="-q"
.\.venv\Scripts\python.exe -m pytest -m "not integration" -o addopts="-q"
# 完整覆盖率门禁（本地历史副本当前会导致此命令非零）
.\.venv\Scripts\python.exe -m pytest --cov-fail-under=80
```

不提供自动 drop/create/reset 数据库逻辑。`TEST_DATABASE_URL` 未配置时集成目录跳过。

## 6. 剩余事项

- 本次两项前置修复完成；Compose 主服务尚未 up。当前项目根 .env 仍缺 POSTGRES_PASSWORD 等 Compose 强制启动项，需要在下一轮启动原全栈服务时补齐；本轮 config 测试占位值没有部署或写入真实 .env。
- API/worker/frontend 实际容器启动、真实 Redis/Celery 消息/故障恢复、浏览器端到端仍待。
- `<本机课程资料目录>` 本轮只识别到 15 份 PDF，未导入或调用 embedding；下一轮可从解析预览开始选样。
- 真实语义 embedding/重排/LLM 改写收益、OCR/PDF 排版、独立金标/规模性能/P2 G 仍待，不把本轮 hash 向量当作教材召回质量证明。
