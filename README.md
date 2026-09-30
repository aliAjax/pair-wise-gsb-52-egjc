# 特殊教育支持计划合规

纯Python标准库实现的特殊教育支持计划合规原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 服务流水账模型

服务记录是**只追加的流水账**，解决“同一节课被同步/重送算两遍”的问题：

- **凭据幂等**：每笔登记必须带服务凭据 `credential`（计划内唯一）、服务日期、分钟和提供者。
  断网恢复后重送、两人并发重送同一凭据，都只返回第一次登记的原结果，分钟不翻倍、主单版本不抬。
- **计划版本授权池**：登记只挂在“登记当时”的计划版本上，按登记先后占用该版本授权分钟；
  超出授权的部分不报错、不丢弃，进入**待处理区**并写明缺口（如“超出第1版计划授权（100分钟），缺口20分钟”）。
- **版本变更**：修订时可发布新授权版本（`service_minutes`）。旧流水始终**按原版本重算**，
  新授权只约束新版本下的新流水；每个版本独立给出有效分钟与缺口。
- **错报冲销**：错报不允许修改原流水，只能追加一笔带原因的冲销（`reverse_service`），再用新凭据重新登记。
  冲销释放出的授权会按登记顺序吸收同版本后续待处理分钟；冲销也幂等。
- **复查/结案闸门**：`review` 与 `close` 在同一数据库事务内校验全部版本的待处理分钟已清零，
  否则 409 拒绝；因此两人并发提交后，累计有效分钟与缺口始终一致。
- **审计时间线**：`audit` 按时间记录登记、冲销、版本变更（`plan_version`）等事件，可按凭据还原全过程。
- **页面三档分钟**：有效分钟、待处理分钟、已冲销分钟分别展示。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、流水账输入校验、授权分配/重算纯函数与汇总。
- `src/repository.py`：SQLite建表、`BEGIN IMMEDIATE`事务、流水/版本/闸门操作。
- `src/service.py`：用例编排、权限检查与幂等入口。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：流水账演示页面（登记、重送、冲销、修订、闸门、时间线）。
- `tests/`：完整流程、规则计算、失败场景与流水账专项测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8328
```

默认端口为`8328`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情（payload内含有效/待处理/已冲销分钟缓存）。
- `GET /api/records/{id}/ledger`：流水账视图：版本授权表、逐笔登记/冲销、待处理缺口、分钟合计。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，`{"reference":"...","data":{...}}`，创建即写入第1版授权。
- `POST /api/records/{id}/actions/{action}`：
  - `consent` / `activate` / `review` / `amend` / `close`：状态动作；
  - `log_service`：登记服务，`data`为
    `{"credential":"SVC-001","service_date":"2026-09-10","session_minutes":60,"provider":"SP-1"}`，
    重复凭据返回原结果并带`"idempotent_replay": true`；
  - `reverse_service`：冲销错报，`{"credential":"SVC-001","reason":"日期登记错误"}`；
  - `amend`可带`service_minutes`发布新授权版本，不带则沿用当前版本。
  - 请求体形如`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。
角色：`case_manager`、`specialist`、`parent_rep`、`administrator`、`admin`（全权）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖：凭据幂等（重送/并发）、超额拆分与缺口说明、复查/结案闸门、冲销不改原流水且顺序吸收待处理、
版本变更只约束新流水、并发提交累计一致、审计时间线还原。
