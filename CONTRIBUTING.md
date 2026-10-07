# Contributing

## 开发前提

- Python 3.12
- Node.js 22+
- Docker Desktop
- PostgreSQL 16 + Redis

## 本地启动

后端：

~~~powershell
cd backend
$env:PYTHONPATH='.'
pytest -q -o addopts="" -m "not integration"
~~~

前端：

~~~powershell
cd frontend
npm ci
npm run build
~~~

## 提交要求

- 不提交真实密钥、数据库密码、用户数据、模型调用日志
- 备份脚本、临时修复脚本、日志文件不进入公开仓库
- 提交前先确认 git diff --cached --stat

## 代码规则

- 配置走环境变量，不硬编码模型名、数据库串和 API Key
- 模型输出必须经过服务端校验
- 变更应补充测试，尤其是工作流、结构化输出、权限和恢复逻辑

## 文档范围与变更记录

公开仓库的 `docs/` 只接受 `docs_public/` 下的公开维护文档。其余优化记录、任务清单、专题、历史报告与私有证据在维护者本机保留，不要求贡献者拥有，也不应强制加入Git。

请在提交或PR说明中记录问题/动机、具体改动、验证命令与结果及仍未验证的边界。若修改现役机制，同步公开主文档；维护者仍按本机规则追加优化记录和任务进度。不上传env、教材、登录态、截图、dump或完整模型调用日志。
