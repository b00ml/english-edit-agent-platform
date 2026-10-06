# MinerU OCR-1/2 接入与验收

> **2026-10-05 OCR-3 运行态更新（OPT-065）**：后台文档/页任务、独立OCR队列/scheduler、hash缓存、取消/重试/租约与断点续跑已部署原Compose；新head rag_ocr_jobs。真实名词选页取消续跑、87,315,331字节动词PDF经代理上传202→completed并缓存命中通过。新增38项回归，全量632通过；主库原task3/content4/user1不变，知识表仍0，新OCR任务3/页检查点4是有意保留的真实验收数据。无付费模型或知识入库，网页界面/确认流程待OCR-4。详见 `docs\现状文档\OCR-3后台任务与大文件验收.md`。

**日期**：2026-10-05
**对应记录**：OPT-064
**结论**：本地 MinerU V1 适配、显式 PDF 逐页路由、结构桥接及父子切块预览已落地；后台任务、网页 OCR 操作和整批资料入库尚未实现。

## 1. 当前能做什么，不能做什么

已可通过本项目的显式 CLI/可复用 Python 入口完成：

`原 PDF → 指定物理页 → 文字/扫描/空页判定 → 本地 MinerU → Pydantic 原生 JSON 校验 → DocumentBlock/Table → 现有 child/parent 切块 → JSON 预览`

这一流程**不调用 embedding、不访问业务数据库、不调用云 OCR/VLM、不自动入库**。生产 API/worker 不安装 MinerU 或模型权重，只声明轻量 HTTP 客户端依赖。

普通 `/api/knowledge/preview` 和上传入口仍走原生解析，**不会因设置 OCR 环境变量而突然跑整本 OCR**。这避免将重资源处理直接塞进同步 HTTP 请求，但也意味着目前网页上传扫描教材仍不能自动识别。OCR-3/4 将负责后台任务及页面接入。

本轮没有重建/重启主 Compose 镜像。代码和 CLI 已测试；现有运行容器仍为此前镜像，不能称“新版 OCR 已部署至网页”。测试时启动本机已有 MinerU 解释器与缓存模型的 API helper，完成后仅停止该 helper，原核心 Docker 服务保持运行。

## 2. 实现与边界

### 本地 V1 客户端

- `backend\app\rag\ocr\mineru.py`：按实际安装 MinerU 4.0.10 的 V1 协议，健康探测、创建上传、PUT 原始页 PDF、完成上传、提交 basic/ocr/middle_json 任务、轮询并下载结构化 JSON。
- 显式本机/私网端点，不使用 SDK 默认云地址，不跟随重定向，不使用系统代理；上游上传资源不得跨源。认证从 config 环境变量读取，错误不回显响应正文/密钥。
- 单页输入最多 10MiB、单页整体时限、响应字节上限；失败/取消/partial 不产生可用文档；超时尽力取消已提交任务。
- Pydantic 校验 `docvortex.middle / schema_version=2.0`、MinerU 4.x producer、单页/page_idx=0、唯一顶层 block index、规范化 bbox。未知/不兼容协议失败关闭，不伪装为 3.x 兼容。
- 固定支持本轮验收的 basic profile，不声称已支持所有 MinerU tier/导出格式。

### 逐页路由与来源

- `backend\app\rag\ocr\pipeline.py`：原文件 SHA-256，1-based 物理页号映射；用当前 PDF 图像变换矩阵估算直接图像覆盖面积，结合文字量区分普通文字/扫描/空页；有文字层但图像覆盖全页仍走 OCR。
- Form 内图像覆盖面积不能可靠测定时，保守走 OCR；inline 图像也纳入信号。面积是启发式估计，重叠/裁剪/复杂 PDF 不保证精确。
- 只抽出需要 OCR 的单页 PDF 上传；文字页不构造 OCR 客户端，空页不调用 OCR。按源页顺序合并结果，OCR 页失败不悄悄降级为只有前几页的“成功”文档。
- stats 中记录 parse_trace_id、页路由、OCR 版本、输入/输出 hash 和调用耗时；此处是本地解析观测，不冒充 LLM TraceLog/付费账单。
- 显式选页结果记录 partial_document、selected_pages。只解析部分页时预览 indexable=false，知识服务在 embedding 前拒绝正式入库。

### 原生结构桥接

- `backend\app\rag\ocr\adapter.py`：优先使用引擎 block index 顺序，不盲目按 x/y 排序；保留原生快照、bbox、父/子 native index 和版本。
- bbox 是 `normalized_page`、`mineru_rendered_page` 坐标帧；缺坐标时为 None，不拿父块框冒充脚注/子块自己的框。尚未实现网页原页定位器，旋转/crop 到原始 PDF 显示框的变换需后续处理。
- 章/节标题适配层级；章节性页眉作为候选章标题保留，普通页眉/页脚/印刷页码不进正文，但完整原生快照保留。
- 同章连续页的重复章节性页眉不重置更深层上下文；**不连续选页显式重置 section context**，避免第2页的节标题污染第16页。
- “【答案】/【解析】”即使被引擎标为 paragraph_title，也保留为题目正文角色，不作为章节标题。尚未实现对所有题型的完整题干/选项/答案原子组识别。
- 表格 caption、footnote 分别保留并关联 table_id；图像/图表不自动解释成知识，未知文本类型保留并告警；标题晚于正文等阅读顺序异常可见，不自动宣称已纠正。

### 表格与切块

- `backend\app\rag\tables.py` 新增独立 HTML span 展开，未替换其他历史格式解析器。
- rowspan/colspan 展开为逻辑网格，使“专有名词”等纵向合并标签在每一条数据行保留；保存原始 HTML、原生单元格起点/跨度、原始 grid、header_rows 和规范化行到源行映射（0-based）。
- 多级 th/thead 表头合成层级列标签，保留空单元格和管道转义；td-only 表格首行仅作为**候选表头**，明确告警，不能断言它一定是真表头。
- 非法/超限/重叠/嵌套表格不制造网格；适配器保留原生文字和降级告警，不抛弃可读内容。不是所有表格错误都能自动恢复。
- `backend\app\rag\preview.py` 抽出原有纯预览；知识服务和 OCR CLI 复用相同 split_document/configured_chunking/parent_plans。现有表格按行切块、补表头、页码/源切片/超长行告警继续生效。
- `backend\app\rag\chunking\strategy.py` 仅增加显式 reset_section_context 处理，其他切块策略没有整体重写。

## 3. 配置和使用

`.env.example`、`backend/.env.example` 和 Compose backend/worker 共享 anchor 已补齐全部新增参数；RAG 参数一致性及实际 Compose 渲染回归通过。没有改变根 .env 中现有模型/密钥配置。

| 配置 | 默认 | 说明 |
|---|---:|---|
| RAG_OCR_ENGINE | off | 显式本地入口开关；非网页自动 OCR 开关 |
| RAG_OCR_URL | 空 | 本机/私网 MinerU V1 地址 |
| RAG_OCR_API_KEY | 空 | 自部署服务可选 Bearer key，不输出日志 |
| RAG_OCR_PAGE_TIMEOUT | 180 | 单页总时限秒数 |
| RAG_OCR_MAX_PAGES | 3 | 单次显式调用所选页上限 |
| RAG_OCR_MAX_INPUT_BYTES | 134217728 | 本地原 PDF 最大读取量，默认128MiB |
| RAG_OCR_MAX_RESULT_BYTES | 4194304 | 每次服务响应最多4MiB |
| RAG_PDF_MIN_TEXT_CHARS | 10 | 有效文字量阈值 |
| RAG_PDF_SCAN_IMAGE_RATIO | 0.5 | 直接图像面积估计的扫描路由阈值 |

**这不是调高网页上传限额**：Nginx 11MiB/后端文件 10MiB 保持不变。本地 CLI 可从大文件取少量页；它不是整书后台批次，超过所选页上限明确拒绝。

本次使用既有 `.local-eval\ocr-2026-10-05\mineru-venv` 和 mineru-home，已有模型缓存，没有新下载模型。启动同样的本机服务时，先设置 MINERU_HOME、MINERU_MODEL_SMALL_BACKEND=onnx 和线程数；在后台/隐藏窗口运行已有 mineru-api，以下为参数示意（没有开放局域网/公网）：

```powershell
# 后台 helper 启动参数；服务启动后再运行项目解释器的 OCR CLI。
# mineru-api --tier basic --host 127.0.0.1 --port 16580 --no-flash --no-advanced --preload-models
$env:RAG_OCR_ENGINE = "mineru"
$env:RAG_OCR_URL = "http://127.0.0.1:16580"
backend/.venv/Scripts/python.exe `
  backend/scripts/ocr_preview.py `
  --input "<本机课程资料目录>" `
  --pages 2,16 `
  --output .local-eval/ocr-2026-10-05/integration/my-preview.json
```

服务启动示意不是现在仍在线的地址；本轮 helper 已关闭。Docker 内 127.0.0.1 指容器自身，后续 worker 接入需真实可达的内部服务地址，不能照抄宿主回环地址。OCR JSON 预览包含私有教材内容，应放本机忽略目录，不加入 Git。

## 4. 真实资料验证

复用用户原文件，调用真实本地 V1 API，不使用 MockTransport：

| 原文件物理页 | 内容 | 结构桥接后表格 |
|---|---|---|
| 第一章 动词.pdf 第1页 | 章/节标题、双语说明、动词分类 | 1张，5×2逻辑网格 |
| 第二章 名词.pdf 第2页 | 专有名词、合并分类格、表格脚注 | 1张，6×3逻辑网格；rowspan=5/colspan=2 |
| 第二章 名词.pdf 第16页 | family/home/house等辨析、例句 | 4张表格 |

- 原始真实 CLI 分别产生动词4个、名词14个预览块，均为选页/partial，indexable=false。最终上下文修正后，复用这些 live native JSON 重放适配器，仍为4/14块；没有把重放耗时算成 OCR 推理耗时。
- 这3页共9个源页复核锚点规范化完全匹配、12个同行关系检查全部通过；不是全页准确率或整表正确率。
- 合并表格5条数据行都携带“专有名词”；每个 chunk.content 都等于规范化 document.text 的真实 start:end 切片，table/child 来源保留原物理页号和引擎 bbox。
- 第2到16页的选页空档生成明确上下文重置；原教材 family/home 疑似释义对调仍保留原文，未擅自“修正”。
- 另外重放上轮12页缓存原生JSON：12/12适配成功，51个child预览块、6张表格；规范化文本切块覆盖率1.0。**这个1.0只说明适配后文本被切块覆盖，不代表扫描原页内容全部识别正确。**
- 实证目录：`.local-eval\ocr-2026-10-05\integration`，包含 live CLI JSON/log、final replay、verification、cache-replay 和全量测试日志；教材识别全文仅本机保留。

## 5. 回归、静态检查与运行态

- 新增 `backend\tests\test_rag_ocr.py`：52条回归，覆盖混合PDF/扫描文字层/空页/Form图像、限制/部分导入拒绝、V1上传与失败/超时取消/跨源拒绝、协议/bbox、合并格/多表头/空格/异常span、标题/答案/gap上下文、source slice/CLI。
- 项目 backend venv 非集成 **573 passed / 21 deselected**；复用原 english-edit-ci-postgres 的全量 **594 passed**，含21项真实PostgreSQL/跨进程恢复。无新库、清库或迁移脚本变更。
- 114条相关回归的 OCR/表格/纯预览限定模块 coverage **89.15%**；这不是全 app/CI 80%门禁通过。
- 修改 Python Black/isort/flake8 通过；9个限定生产模块/脚本 strict mypy通过；实际 Compose config --quiet 与RAG key parity通过。
- 原API及前端反代ready=true；主库task3/content4/user1，knowledge_chunk/document均0，未新增课程知识或伪造向量。
- 主容器仍为此前镜像；没有将本次工具“部署后网页可用”当成已完成事项。

## 6. 下一阶段

- OCR-3：本地服务部署/worker连通，后台文档与页任务、可控本地大文件批次、进度/重试/缓存/取消/重启续跑。原文件不应重复上传整书，工作状态与可索引状态要分离。
- OCR-4：网页触发/轮询、原页→识别→表格→父子预览和人工确认；跨页续表、英文词边界、源疑点与质量 warning 的处理策略。
- 随后才进行小量真实 embedding 和知识点/表格行/题目召回、引用、内容质量金标评测。未配置/未验收费率的推理不偷偷开启。
- GPU/整书吞吐、全部文档格式布局、所有题目原子组和第三方模型/对外分发许可审查尚未验证。

**当前交付边界：可复用本地解析链路已完成；自动化网页生产导入尚未完成。**
