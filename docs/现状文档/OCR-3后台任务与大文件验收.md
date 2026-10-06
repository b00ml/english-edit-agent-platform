# OCR-3 后台任务、大文件与运行态验收

**日期**：2026-10-05
**对应记录**：OPT-065
**当前结论**：后台 OCR 已部署到原 Compose，并通过真实 Redis→独立worker→本地MinerU→持久化预览验收。**没有自动向量化或知识入库，网页工作台交互仍待 OCR-4。**

## 1. 实际架构

```text
鉴权API：大文件上传 / 配置根目录内的本地批次
    → PostgreSQL ocr_job + ocr_page + 共享源文件快照
    → Redis（ocr队列）
    → 单并发 ocr-worker（每条消息处理一页）
    → 宿主本地 MinerU basic/ONNX V1（Bearer鉴权）
    → 页检查点 / 按tenant+源hash+页+版本+代码签名的缓存
    → 全部所选页完成后组装 DocumentBlock / child / parent 预览
```

- 原生成worker明确只消费 `celery`；新增OCR worker只消费 `ocr`，并发1。CPU重任务不占满原5个生成槽位。
- `ocr-scheduler` 独立Celery beat周期触发扫描，负责待投递/延迟重试与过期租约回收。提交先持久化数据库，即使broker短暂失联，也能由后续扫描补发。
- 原PDF以流式方式复制、计算hash，任务消息只有job ID，不把大文件放入Redis。API只做接收/文件校验/建任务，不执行OCR或embedding。
- 复用原Postgres/Redis/数据卷；仅新增OCR共享文件卷及两个服务。没有新建业务隔离环境。

## 2. 数据、幂等与恢复

新增迁移 `rag_ocr_jobs`，下接 `rag_p1_def`，单一head。只新增两张表，不重嵌入或改写旧知识/生成记录：

- `ocr_job`：tenant/creator、源hash/文件键、页选择、状态、dispatch/retry时刻、租约token/截止、取消标记、预览键/hash及错误信息。
- `ocr_page`：job内唯一物理页、状态/尝试数、结果键/hash、cache_hit、引擎版本/耗时、错误以及attempt_history/trace_id。

页状态与文件产物分开：

1. worker用SQL条件更新领取文档租约，一次只处理一个未完成页；重复消息不能重复领取。
2. 成功页先原子写临时文件→fsync→replace，再带租约token提交页结果和hash；attempt文件名包含token，旧worker不能覆盖新尝试文件。
3. 取消立即失效token；客户端在上游请求/轮询间检查取消，尽力取消MinerU任务。**不保证底层模型计算立即释放资源**，但旧结果不能再提交成成功。
4. 续跑保留有效已完成页；损坏/丢失检查点会重新标为待处理，只修复对应页。输入快照hash不一致则明确失败，不偷偷切换资料。
5. worker失联后由周期扫描回收过期租约；新token阻止迟到写入。数据库/worker恢复和重投幂等有真实PostgreSQL测试；没有冒称已完成全部生产故障演练。
6. 暂时性本地服务/传输故障按后台重试上限和backoff处理；输入、schema等失败不会无限重试。失败/取消任务可人工续跑。

**注意**：正常重投/进程重启跳过已提交的成功页；若worker在“模型已完成、页检查点尚未提交”之间被杀，仍可能重新推理该页。这里不是跨服务严格exactly-once保证。

## 3. 缓存与预览

- 缓存分tenant（含默认null租户），按源文件hash、物理页、服务真实版本、adapter/pipeline/table代码hash和路由配置签名命中，不跨租户复用教材文字。
- 缓存digest不对时重新解析；已提交页检查点hash不对时不能当作有效结果。缓存命中不会把原推理耗时伪装为新推理耗时。
- 全部所选页结束后组装预览，保持实际页码/源切片、合并格、连续章节与不连续选页reset。跨页续表/所有题型原子组仍需后续处理。
- 单次任务默认最多500页，本地/后台输入128MiB、最终预览64MiB。大文件没有无限读取或不受控输出；默认值是本项目参数，不代表所有大文档均能处理成功。
- 即使任务completed，`ready_for_indexing=false`、`indexing_performed=false`。只选部分页时最终预览indexable=false；完成整文件时预览可表示结构上有可索引内容，仍不代表OCR事实正确/人工确认已完成。

## 4. API（全部代码鉴权与tenant scope）

| 方法与路径 | 用途 |
|---|---|
| POST `/api/knowledge/ocr/jobs/upload` | 后台PDF上传，multipart file/pages，202 |
| POST `/api/knowledge/ocr/jobs/import-local` | 配置根目录内相对路径批次，202，逐文件accepted/errors |
| GET `/api/knowledge/ocr/jobs` | 分页任务列表 |
| GET `/api/knowledge/ocr/jobs/{id}` | 文档进度及分页页状态/尝试记录 |
| POST `/api/knowledge/ocr/jobs/{id}/cancel` | 取消，不丢弃成功页 |
| POST `/api/knowledge/ocr/jobs/{id}/resume` | 失败/取消后的受控续跑 |
| GET `/api/knowledge/ocr/jobs/{id}/preview` | 校验hash后下载完成的JSON预览 |

读需要ops:read，写需要ops:write；非admin仍按原tenant政策限制列表、详情、取消、续跑和预览。接口不暴露服务器存储路径或OCR密钥。本地导入拒绝绝对路径、配置根外路径和逃逸symlink，不能读取任意宿主文件。

批次示例请求体：

```json
{
  "files": ["二、词法篇/第二章 名词.pdf"],
  "pages": [2, 16]
}
```

pages不传/null表示选择全文件，仍受后台任务页上限约束；不同文件需要不同页选择时分别提交。这里没有把“受控路径批次API”称为拖拽整个目录的网页界面。

### 上传限制必须分开

- 原知识上传及同步预览仍是后端10MiB、Nginx body11MiB，网页现有控件不变。
- 只有精确路径 `/api/knowledge/ocr/jobs/upload` 允许Nginx body129MiB，给默认128MiB文件留multipart空间；后端流式复制仍实际校验字节上限。
- 修改输入上限时应同步代理配置。不要把增加后台上传路径当作旧知识上传已无大小限制。

## 5. 部署与启动

当前实际服务：原postgres/redis/backend/worker/frontend + `english-edit-ocr-worker` + `english-edit-ocr-scheduler`。API与前端镜像已重新构建，原卷保留，nginx-t与ready通过。

```powershell
# 项目根目录，复用原环境；不要另建/清空数据库。
docker compose --env-file .env `
  -f deploy/docker-compose.yml --profile ocr up -d
```

- `deploy\start-local-ocr.ps1`：使用已有本机MinerU解释器/模型缓存，隐藏窗口启动；需要根.env配置至少32字符的私有RAG_OCR_API_KEY。停止参数只处理已登记且身份匹配的helper进程树，不删除资料。
- 服务本次为本机CPU basic/ONNX，绑定0.0.0.0供Docker host gateway访问，强制Bearer鉴权；容器配置使用 `http://host.docker.internal:16580`。没有向公网/云服务上传教材。若使用其他平台，需配置真正可达的内部地址。
- 不是Windows系统服务/自动开机项。重启电脑后需要再次启动本地OCR服务；仅启动Docker不代表宿主推理服务在线。服务失联会有失败/重试状态，修复后可resume。
- 根.env仅追加OCR开关/地址/鉴权与资料挂载；没有更换现有模型供应商、JWT或管理员密码。当前宿主服务、OCR worker和scheduler留运行供下一阶段复用。
- Docker共享卷默认 `/var/lib/english-edit/ocr`，导入资料只读挂载 `/course-materials`；根.env.example为Docker路径，backend/.env.example为宿主开发路径，不能让两个容器各写自己的相对目录。
- 新的RAG_*参数全部在API/worker共享anchor透传，OCR profile服务继承同一配置。重模型/权重不进入API/worker镜像。

## 6. 真实资料与运行态验收

实际前端代理→鉴权API→Redis→OCR worker→宿主MinerU→Postgres/共享卷，没有mock HTTP、模型或消息队列：

1. 导入原名词PDF第2/16页；观察完成一页后取消，再resume；任务最终2/2页completed，已完成页尝试数不增加。
2. 读取完成预览，检查5行纵向合并标签均为“专有名词”，rowspan=5保留；child文本等于规范化原文的真实start:end slice；选页结果indexable=false。
3. 导入原动词PDF第1页，任务completed。
4. 将同一动词PDF **87,315,331字节**（约83.27MiB，87.3MB）经前端代理新后台路径上传：202→completed，缓存命中1页，不重跑该页OCR。原10MiB同步路径没有被放宽。
5. 最新镜像再次重建/部署后，服务ready仍健康；数据库新head与代码一致。未重跑整批341页。

当前主库：

| 表 | 验收后数量 |
|---|---:|
| generation_task | 3（不变） |
| content_item | 4（不变） |
| users | 1（不变，未修改原密码） |
| knowledge_chunk / knowledge_document | 0 / 0 |
| ocr_job | 3（有意保留的真实验收任务，全部completed） |
| ocr_page | 4（含上传缓存重复页） |

这些是OCR工作记录/检查点，**不是已入库知识或伪造向量**。真实OCR产物在共享卷，宿主验收日志/预览/迁移前备份在 `.local-eval\ocr-2026-10-05\background-runtime`，Git忽略。两个原数据库迁移前均pg_dump备份，没有清库。

## 7. 回归与证据边界

- 新增35条非集成（31个任务/存储/API/恢复 + 4个本地版本/取消协议）和3条真实PG专项，较OPT-064新增38条。
- 最新全量 **632 passed**，含24项真实集成；非集成计数608项。首次全量唯一失败是旧governance测试写死前一head，已更新到真实新head并全量复跑通过，没有放宽迁移安全检查。
- 87项相关回归限定 storage/assemble/service/runner/API coverage **92.20%**。不是全app/CI80%门禁通过。
- 修改Python Black/isort/flake8通过；7个限定生产模块strict mypy通过，Celery无类型注册decorator使用精确ignore，函数体继续检查。
- 实际镜像build、前端TypeScript/Vite build、Compose profile渲染、nginx-t、服务ready/真实任务/缓存验收通过；资料Root/字节上限/租户/损坏检查点/重投/租约/token/取消/续跑均有回归。
- 没有付费embedding/chat/rerank调用，没有知识入库、召回质量或答案事实正确率验收。CPU/GPU压测和全故障注入仍待。

## 8. 风险与下一阶段

- OCR-4仍待：工作台提交/进度/页级预览/取消/续跑界面，原页坐标可视核对、解析warning与疑似源错误确认，再决定小量真实索引。
- 原页bbox是引擎rendered-page规范化坐标，旋转/crop映射和可视定位尚未实现；复杂布局/跨页表格/题组并不保证自动正确。
- 缓存命中需要健康版本探测；已有完成预览可读，但新待处理页不因缓存存在就忽略服务版本/可用性。
- 文件快照/缓存/产物保留，没有偷偷自动清理；长期存储配额/归档/清理仍是运营事项。
- 宿主OCR服务需人工启动，模型和代码分发许可、GPU性能、全量资料吞吐、真实检索金标尚未审查/验证。
- 现有管理员账号沿用原凭据；初始化种子密码配置不等于已经修改现存账号密码，不能据此称账号安全治理已完成。

**当前状态：后台OCR API可用且已部署；网页生产操作与确认入库尚未完成。**
