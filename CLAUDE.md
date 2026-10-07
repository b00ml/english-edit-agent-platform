# CLAUDE.md —— 行为准则

## Think Before Coding
- 动手前先确认改动会影响哪一层（api / workflow / engine / rag / worker / frontend）。
- 涉及生成、质检、路由、状态机逻辑的改动，先想清楚重试、降级、失败路径。
- 不确定现有行为时先读 `docs/docs_public/英语内容生产工作台-当前实现与系统架构.md` 与对应模块源码，不凭猜测改。

## Simplicity First
- 只做被要求的事，不做无关「优化」或超前抽象。
- 一个函数只做一件事，输入/输出 Schema 明确。
- 新增逻辑前先确认是否可复用 engine/rag/workflow 已有实现。

## Surgical Changes
- 只触碰必须改动的文件与行，避免大范围重构已跑通的代码。
- 不因「顺手」改无关命名、格式或依赖。
- 结构化输出 / 质检 / 状态机是核心，改动必须最小且可验证。

## Goal-Driven Execution
- 每个改动说明目的与验证方式；涉及模型调用给出可复现的验证入口（如单测或 API）。
- 完成标准：改动不破坏现有 `生成→质检→入库→人工质检` 闭环，成本与质量口径不变。

## Project Constraints（MUST）
- MUST 配置走 `app/config.py`（环境变量），禁止硬编码。
- MUST 题型扩展通过新增 `backend/app/templates/*.yaml`，禁止改代码加题型。
- MUST 模型输出经 Pydantic 二次校验，禁止直接信任模型返回。
- MUST 每次 LLM 调用带 `trace_id`，记录模型/输入输出/耗时/成本。
- MUST 遵循 `.ai/rules/coding.md` 与 `.ai/rules/architecture.md`，冲突时以本文件为准。
