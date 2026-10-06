# 扫描教材 OCR 与文档解析选型实测

**日期**：2026-10-05
**结论状态**：三引擎同样本本地实测完成；仅形成接入候选，不等于生产 OCR 已交付。
**资料来源**：`<本机课程资料目录>`
**证据目录**：`.local-eval\ocr-2026-10-05`（Git 忽略，仅本机保留）。

## 1. 结论与建议

针对本次英语扫描教材，**优先把 MinerU basic/ONNX 作为首个接入候选**：所选 36 个锚点中 35 个规范化完全匹配、36 个近似匹配；12 个同表行关系检查通过；合并表格样本保留了 HTML rowspan/colspan。本配置 CPU 页耗时也显著低于本次 Paddle 配置。

Docling 本配置速度更快，但 S04 出现章/节标题落到正文和表格之后、章编号缺失；应先补标题/阅读顺序质检，再考虑作为快速路径或备选。PaddleOCR 本次 CPU 配置较慢，**未测试 GPU，不能据此否定其 GPU 或其他模型配置**。

这里比较的是指定配置、指定教材页，不是开源项目的全能力排名。未评测端到端检索、答案正确率、整书跨页表格或 GPU 吞吐；不要按 12 页时间直接承诺 341 页处理时长。

## 2. WeKnora 到底在哪里做 OCR

依据本机 WeKnora 源码，而非项目名称推断：

- `<本地WeKnora源码目录>\docreader\parser\pdf_parser.py`：按页面判断文字页/扫描页，结合图像面积与有效文本信号；文字页提取文本，扫描页渲染为图像并标记 `image_source_type=scanned_pdf`。这一内置 Python PDF parser 本身不运行 OCR。
- `<本地WeKnora源码目录>\internal\application\service\image_multimodal.go`：在 OCR 开启时调用配置的 VLM，扫描 PDF 使用专门提示；识别文字建立 `image_ocr` 子块，保留父块关系，caption 另行处理。
- `<本地WeKnora源码目录>\docreader\parser\registry.py`：解析器注册与按文件类型的默认/回退路由。

因此，**本机 WeKnora 有 OCR 链路，但不能说它必然是 PaddleOCR，或只要复制 Python PDFParser 就能得到 OCR**。可移植的是逐页路由、引擎适配、子块来源和失败可见等设计；本项目可以用实测合适的本地解析引擎完成同一目标，不必把 VLM 云调用一并搬过来。

## 3. 样本与实验边界

此前全量资料预检：15 份 PDF，共 341 页，无可抽取文字层，12 份超过当前 10 MiB 上传上限。本轮选取 4 份文件的 12 页，覆盖双语正文、标题、例句、题干/选项/解析、边框注释、简单表格及合并表格。

| 样本 | 相对资料路径 | PDF 物理页号（1-based） |
|---|---|---|
| S01–S03 | 一、句法篇/第二章 并列句.pdf | 1、3、6 |
| S04–S07 | 二、词法篇/第一章 动词.pdf | 1、8、24、45 |
| S08–S10 | 二、词法篇/第二章 名词.pdf | 2、8、16 |
| S11–S12 | 一、句法篇/第三章 主从复合句.pdf | 4、15 |

统一输入是 Poppler 渲染、最长边 1800 px 的同一组 PNG 页面，**不是直接让三套引擎导入整个 PDF**；因此不代表它们的原生 PDF 批处理、跨页关联或混合 PDF 路由效果。原 PDF 未修改，未把完整教材或识别全文纳入 Git。

本机 RTX 4060 Laptop（约 8 GiB 显存）已验证 Docker GPU 透传，但三套实际评测均为 **CPU**；各配置尽量使用 2 线程。峰值内存/显存没有记录，不做内存排名。评测解释器和模型缓存与 backend 运行依赖分开；没有新建业务数据库、清库、付费推理或 embedding。

## 4. 实际配置与版本

版本是本次已安装、已运行版本，不宣称为各项目的最新版本。

| 引擎 | 本次配置 |
|---|---|
| PaddleOCR 3.7.0 / PaddlePaddle 3.3.1 | PPStructureV3；PP-OCRv6 medium det/rec；CPU 2 线程，MKLDNN off；表格开；公式/图表/印章/方向/文档展平关 |
| MinerU 4.0.10 | basic tier，ONNX 本地 managed parse server；CPU；intra-op 2、inter-op 1、OMP 2；remote off，telemetry off；CLI `mineru parse`、`--force` 禁用缓存结果复用 |
| Docling 2.133.0 | CPU 2 线程；full-page 中文 RapidOCR ONNX；Heron layout；显式 TableStructureOptions（legacy TableFormer accurate）；批大小 1；remote services off；本地 artifacts，HF_HUB_OFFLINE=1 |

完整配置保存在证据目录的 `engine-config.json`。各引擎并非同一 OCR 模型；模型下载/首次初始化与页面转换计时分开。

## 5. 最终结果

| 引擎/CPU 配置 | 转换成功 | 规范化完全匹配锚点 | 近似匹配锚点 | 同表行关系 | 第 2–12 页耗时中位数 |
|---|---:|---:|---:|---:|---:|
| PaddleOCR PPStructureV3 | 12/12 | 35/36 | 36/36 | 12/12 | 92.415 s/页 |
| MinerU basic ONNX | 12/12 | 35/36 | 36/36 | 12/12 | 13.307 s/页 |
| Docling 指定配置 | 12/12 | 34/36 | 35/36 | 12/12 | 4.813 s/页 |

| 补充指标 | PaddleOCR | MinerU | Docling |
|---|---:|---:|---:|
| 12 页转换计时之和 | 1173.232 s | 163.393 s | 73.565 s |
| Markdown 导出字符数 | 12502 | 11601 | 12456 |

字符多不表示更准确，字符少也不自动表示丢失正文。部分准备/预热过程存在重叠；以上时间是本次配置的描述性观测，不是严格隔离条件下的性能基准。第 2–12 页中位数排除首个测量页，但不能保证所有延迟加载均已排除。

### 评分定义与盲区

- 共 36 个人工按原页复核的锚点；使用 NFKC、大小写归一、空白/格式移除、弯引号归一。完全匹配与最佳子串字符编辑率 ≤0.1 的近似匹配分别计数。
- **不是全页字符准确率/CER，也不是“教材准确率 97%/100%”**。标题与正文的先后顺序、全页漏段、英语词边界不能由这 36 个点充分检出。
- S04/S08/S10 共 12 个表格关系检查：解析 HTML/Markdown 表行，要求标签在同一行，而非只在页面任意处出现。没有覆盖全部单元格、合并格继承、列位置及跨页续表，不代表整表正确率 100%。
- 初始小图人工记录存在误读，已对照原始渲染大图修正共同金标，三引擎统一重评分。以最终 report/summary 为准，不使用早期控制台分数。
- 最终 gold SHA-256：`2ba973c50a619b7a759cf6aba10d48ab2c6f8134ed7d10a64fcd242f32fbf6f3`。

## 6. 人工核对发现的 RAG 风险

### 6.1 标题识别与阅读顺序

Docling S04 的 Markdown 从“一、动词的概述”开始，正文/表格结束后才出现“动词”、开篇教师寄语及“第一节|动词的分类”；“第一章”未导出。按这一输出建立 section_path 会给内容挂错或漏掉章节上下文。此结论限定为本次配置/这一页，不泛化为 Docling 所有输入都会失败。

### 6.2 合并单元格与纯 Markdown 的信息边界

MinerU S08 保留 `rowspan=5`、表头 `colspan=2`；Docling Markdown 把合并格展开为重复“专有 名词”，并重复“类别”表头。Markdown 展开不等于其原生 JSON 必然丢失合并信息。

Paddle 也在 Markdown 中包含 HTML 表格。当前应用的普通 Markdown 解析不能假定自动等价处理所有嵌入 HTML 表格。接入应优先用原生结构化元素/表格 JSON；若引擎只导出 HTML 表格，需要适配 rowspan/colspan、继承表头、行列坐标，再转本项目 Table/DocumentBlock，**不是把 output.md 整段拿去 embedding**。

### 6.3 源资料疑似错误与识别错误必须分开

S10 原扫描表中 family 行写的是住所/感情色彩，home 行写的是家庭成员，疑似教材释义对调。三引擎都按原页识别，不应判为 OCR 错误，也不能悄悄改写原文。后续需保留原文/页码，单独标为“源资料疑似错误，待人工审核”；用户确认的勘误另建有来源的版本。

### 6.4 英语词边界与格式噪声

Paddle/MinerU 在 S10 都出现 `dowith`，而原页为 `do with`。空白归一的锚点评分不会发现这种错误。真实验收还需英文词边界、缩写/否定词、题号、选项、答案关联，以及不应进入正文的页眉页脚/插图占位检查。

## 7. 安装与运行问题（已解决，未掩盖）

- Paddle 首次模型初始化/下载约 536 秒；初始化仍加载了多组布局/表格模型。下载时间不归入识别质量，也不作为稳定页吞吐。
- MinerU 4.x 使用 `mineru parse`，旧 `-p/-o` 示例不能直接照搬；最初本地服务未就绪导致 smoke 失败，预热后最终 12 页成功。增加 `--force` 避免缓存伪装为推理时间。评测 managed 服务已停止，未停止主 Compose。
- Docling 评测 venv 先遇到 NumPy/scikit-learn ABI 问题，仅在评测 venv 安装 scikit-learn 1.9.1 修复；随后 Hugging Face 模型下载超时，改用 ModelScope 对应模型镜像，下载 safetensors/ONNX 并记录哈希，显式 artifacts 路径离线转换成功。
- Paddle 聚合 Markdown 初版会把已有 output.md 也再次读入，S01 形成重复导出。已排除该文件、从原生输出重建聚合、统一重评分，并补回归测试；没有手工修饰识别正文。

## 8. 后续接入顺序与验收门槛（尚未实施）

1. **解析 adapter + 逐页路由**：文字 PDF 保留当前廉价文本路径，扫描/混合页进入本地 MinerU adapter；可配置引擎、版本、超时与显式错误。原始文件 hash、物理页码、页内 bbox/块顺序、标题路径和原生结构快照可追溯。没有真实源坐标时标为未知，不制造坐标。
2. **结构桥接与切块联测**：原生表格/HTML 合并格转统一 Table；小表不拆，大表按行拆并携带表头/合并标签；定义/例句/题目/解析不随字符数任意拆。标题顺序异常或超大行应告警，不承诺任何输入都绝不切断。
3. **异步页级批次与大文件策略**：当前 10 MiB 限制仍在；新增可控本地路径批次或审慎配置上传上限。OCR 放后台有界任务，逐页状态/失败重试/缓存 hash/取消/重启续跑；重资源解析不直接塞入现有同步 HTTP 预览。
4. **导入前真实内容预览**：原页→识别块→表格→父子块逐层可核对；维护 section_path 跨页继承、跨页续表识别/告警与页眉页脚去噪。OCR 失败、缺标题、顺序异常、疑似源错误不得显示成“已完整入库”。
5. **小量真实索引与召回评测**：待上述通过且明确使用现有付费 embedding 配置后，再导入小批真实教材、测知识点/例句/表格行/题目检索与引用。不能拿零向量或这次 OCR 锚点分数当 Recall@K/MRR/事实正确率。

接入前还应核对锁定版本的代码与模型许可证、保留版权说明；本轮未把第三方模型/源码抄入应用，不宣称已完成后续对外分发的许可审查。

## 9. 复现、回归与工作区状态

脚本：`backend\scripts\ocr_benchmark.py`，依赖按引擎惰性导入，无业务数据库/embedding 调用。测试：`backend\tests\test_ocr_benchmark.py`。

在项目根目录用任意具备标准库的 Python 可重评分，不下载模型、不重跑推理：

```powershell
python backend/scripts/ocr_benchmark.py `
  --engine mineru --score-only `
  --gold .local-eval/ocr-2026-10-05/samples/gold.json `
  --output .local-eval/ocr-2026-10-05/results/mineru
```

将 engine/output 分别换为 paddle 或 docling 可复核其他结果。正常推理另外需要 `--manifest` 与 `--config`，并使用对应评测解释器；不要重跑全量下载来重复本次已完成结果。`rescore_report` 与 `summarize_report` 已逐项复现原 summary；金标 hash 写入各报告，缺输出/缺金标不会覆盖旧报告。

- 新增 9 条纯单测：归一化、锚点评分、HTML/Markdown 同行检查、重评分 hash/失败/计时、无引擎初始化 CLI、重复导出回归。
- 最终项目 backend venv 非集成回归：**521 passed / 21 deselected**；修改 Python 文件 Black/isort/flake8 通过。
- 初次误用系统 Python，3 个旧 fixture 单测因缺少 alembic.config 失败；没有改代码绕过，改用项目已有 backend venv 后通过。系统解释器不是本项目完整测试环境。
- 本轮不重跑数据库迁移/21 项集成测试，不声称重验 80% CI 覆盖率；前轮的真实数据库测试证据继续有效。
- backend 直连和 frontend 反代 `/api/health/ready` 均 healthy/ready=true；主库 rag_p1_def；task=3、content=4、user=1，knowledge_chunk=0、knowledge_document=0。

| 事实面 | 收尾状态 |
|---|---|
| 代码 | changed-and-verified：评测工具/评分回归，生产 parser 未改 |
| 运行态 | verified-current：原核心服务健康、业务计数不变 |
| 文档 | changed-and-verified：本报告、RAG 现状/计划/风险、OPT/tasks 同步 |
| 规则 | verified-current：按仓库规则记录变更，未改工程约束 |
| 记忆 | out-of-scope：未读取或修改外部共享记忆 |
| 工作区 | verified-current：保留原未提交 P0/P1 改动；评测原页/全文/模型在 .local-eval 且被忽略，未清场 |

**最终边界**：OCR 选型实验完成 ≠ 上传扫描 PDF 已能 OCR ≠ 教材已入库 ≠ RAG/批量内容质量已验收。
