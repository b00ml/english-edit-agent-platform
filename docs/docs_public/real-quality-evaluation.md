# 真实质量评测协议与本地清单

`freeze/resume` 只冻结、核对本地评测材料，不调用模型、数据库或 Redis。
`execute-*` 显式连接数据库核验档案并调用供应商，可能产生费用；`evidence` 仅导出本地材料。现有
`structured_retry_eval.py run/review` 仍是确定性合成回归，不能用作真实质量结果。

## 数据边界

仓库内 `backend/evaluation/real_quality_cases.json` 是空骨架，不包含教学用例。
调用方须在本地独立提供已确认的数据集；程序不会自动生成或补齐用例。
结构由相邻的 `real_quality_cases.schema.json` 校验，运行时还校验数量、题型均衡和跨集去重。

- 探索集 30 条、确认集 30 条：每集单选、完形、阅读各 10 条。
- 独立冒烟集 3 条：每种题型各 1 条，不与两集共享输入或标识。
- 高一、难度中、每次生成 1 份；完形 5 空，阅读 3 问。
- 每条保留 `knowledge_point`、`constraints`，通过模板声明、生成提示词和参考评审上下文传递。

## 冻结与续跑检查

从 `backend/` 执行：

```powershell
python scripts/structured_retry_eval.py freeze --dataset <local-cases.json> --config <safe-runtime.json> --output <manifest.json> --run-id <run-id> --seed 21
python scripts/structured_retry_eval.py resume --dataset <local-cases.json> --config <safe-runtime.json> --manifest <manifest.json>
```

配置须显式提供 `profiles.generation/judge/reference`，每个档案含
`name/provider_id/provider_config_hash/model_name/model_family/profile_hash`；凭据和端点原文不进入清单。
`prices` 以模型名索引，记录 `prompt_per_1k/completion_per_1k/currency/verified`，未知价格用 `null`。
`generation` 含 `temperature/timeout_seconds`；`judge` 含
`temperature/sample_rounds/max_attempts/calibration`（按题型的维度权重覆盖）。
参考模型家族须同时区别于生成模型和 Judge 模型家族。

清单冻结完整数据、模板、提示词、技能、档案、价格、参数、Judge 权重和模板阈值。
它包含稳定 `run_id`、各部分身份哈希、随机种子及有序冒烟或探索计划。
探索计划的三臂共享每条输入的 `first_draft_id`，物理调用账目留在 `provider_calls`，
路径状态留在 `execution_plan`，本地证据索引留在 `artifacts`。
所有路径起始为 `planned`；允许后续执行器记录 `in_flight/completed/failed/unresolved`。

任何冻结输入、资产、配置或执行计划变化均拒绝续跑，须建立新运行。
`resume` 仅验证并返回兼容信息，保留现有调用、证据、进行中与待裁决状态，不重放请求。
新冻结拒绝覆盖已有清单；服务层写入采用同目录临时文件替换。

RAG、few-shot、档案降级、Judge 改版固定关闭，结构化尝试上限 3，SDK 隐藏重试为 0。
传输层额外尝试上限 2，但服务商接收/完成状态不明时保留待裁决，禁止自动重发。
确认集此时只冻结输入，确认执行计划由后续基于探索证据的单因素选择流程建立。

## 冒烟、人工检查与正式探索

确认本地数据、冻结档案和价格后，先独立冻结并执行 3 条冒烟输入（9 条逻辑路径）：

```powershell
python scripts/structured_retry_eval.py freeze --dataset <local-cases.json> --config <safe-runtime.json> --output <smoke.json> --run-id <smoke-run-id> --seed 21 --set-name smoke
python scripts/structured_retry_eval.py execute-experiment --dataset <local-cases.json> --config <safe-runtime.json> --manifest <smoke.json>
```

逐项检查生成上下文、参考解题与复核的盲态输入、Trace、usage 和本地落盘。
执行器要求全部冒烟路径及 Judge、参考评审完成，物理调用 Trace 均已持久化，
再接受操作者的明确检查回执；质量不通过仍保留为有效负面结果。

```powershell
python scripts/structured_retry_eval.py review-smoke --dataset <local-cases.json> --config <safe-runtime.json> --manifest <smoke.json> --inspected
python scripts/structured_retry_eval.py execute-experiment --dataset <local-cases.json> --config <safe-runtime.json> --manifest <manifest.json> --reviewed-smoke <smoke.json>
```

`--inspected` 是实际检查后的人工确认，程序不会代替操作者确认。正式探索必须匹配
冒烟清单的数据、配置与资产哈希，且回执绑定实际执行结果；材料变化须重新冻结并冒烟。
`execute-generation/execute-judge/execute-reference` 支持逐阶段续跑，同样要求正式清单
携带 `--reviewed-smoke`。Judge 失败保留结果，独立参考评审继续执行。

入口先执行相同的冻结一致性检查，再从数据库读取绑定档案，重新计算实际
Provider 配置与模型档案身份；任何漂移或缺失均拒绝发送。执行使用冻结温度和超时，
SDK 重试固定为 0，教学参数进入生产生成器和校验器；不会填入官方空数据骨架。
完整执行器按生成、生产 Judge、独立两阶段参考评审顺序执行，确认集实验另行冻结。

三臂共用一次首稿响应，各臂独立通过同一确定性校验。单次臂最多一次，两个重试臂
各最多三次语义尝试；首稿通过即无额外发送。只有明确未接收、未完成且暂时失败的
传输证据才允许最多两次额外发送；超时和 HTTP 状态本身不构成这种证据。

每次物理请求前后写入本地清单，保存 messages、原文、usage、延迟、验证结果与成本状态。
已落盘响应续跑时只在本地重验；未知接收/计费或中断中的请求保留 `unresolved`，不会自动重发。
Trace 可能已写入后发生中断，因此不重复写入，覆盖缺口显示在 `trace_coverage`。
清单包含完整教学输入与输出，应保存在操作者控制的本地位置。

`generation_accounting.physical_cost` 按物理请求计一次，各臂的 `logical_cost` 是独立运行
费用估计；缺 usage 或未核验价格时保持 `null`。生成成功仅代表结构化与确定性校验通过，
不能作为教学质量改进或人工验收结论。

每次发送前，从冻结计划推导并核对语义请求和物理发送硬上限。N 条输入最多 5N 次
生成请求、2N 份不同有效终稿；Judge 上限为 `2N × sample_rounds × max_attempts`，
每个参考阶段最多 6N 次请求。每个语义请求最多 3 次物理发送，明确未接收的重传也占
物理额度。续跑累计既有账目，已完成响应的本地复用不占新额度；达到上限先停止再发送。

## 本地证据导出

```powershell
python scripts/structured_retry_eval.py evidence --dataset <local-cases.json> --config <safe-runtime.json> --manifest <manifest.json> --output <local-report.json>
```

命令无需数据库或模型连接，输出 JSON 和同名中文 Markdown 面试简报。报告保留全部
90 条探索路径的各阶段计划、提交、完成、成功、失败、取消、缺失、待裁决与待复核计数，
按臂和题型展开；提交数只认已有物理发送。`pending_review` 是待裁决的补充标记，不能
和其他状态相加。实际物理账目去重，各臂逻辑账目分配共享调用，不能再相加当供应商账单。

费用给出已知小计、币种、usage/Trace 覆盖和未知调用标识；证据有缺口时完整费用为
`null`，不能按零处理。延迟为每次供应商发送的观测值，不能据此宣称任务端到端 P95、
吞吐或 MTTR。Judge 与参考模型一致率、Kappa 只描述模型间判断；原始坏例、分歧和
未决状态保存在本地报告中。模型参考不等于人工金标准，探索结果不等于已确认质量提升。
本地清单含完整输入输出；导出不会发布或上传这些内容。
