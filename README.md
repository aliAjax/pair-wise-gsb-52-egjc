# 特殊教育支持计划合规

纯Python标准库实现的特殊教育支持计划合规原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、同意、服务履约、复查期限和计划版本和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8328
```

默认端口为`8328`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面（有效/待处理/已冲销分钟、版本授权、流水表、登记与冲销表单）。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/ledger`：服务流水账视图：`totals`（有效/待处理/已冲销/缺口、按版本缺口、当前授权剩余）、`versions`（计划版本授权快照）、`entries`（逐笔有效/待处理拆分与冲销信息）。
- `GET /api/records/{id}/plan-versions`：计划版本授权列表。
- `GET /api/records/{id}/service-entries`：原始服务流水与冲销记录。
- `GET /api/records/{id}/audit`：审计时间线，按凭据还原登记、冲销和版本变更。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`，同时写入v1授权版本。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

### 服务流水账规则

- 每笔服务是一条**不可变流水**：`credential`（服务凭据，全局唯一）、`service_date`、`minutes`、`provider`，并锁定登记时的计划版本。
- **断网重送/两人并发**提交同一凭据：只产生一笔，重复请求返回原结果（`duplicate:true`），不重复计费。
- 超出登记时计划版本授权的分钟留在**待处理区**，逐笔返回缺口分钟，合计按版本给出`gap_by_version`。
- 计划修订（`amend`带`new_service_minutes`）生成新授权版本：**旧流水永远按原版本重算**，新授权只约束新流水。
- 错报不能改原流水，只能`void_service`写原因冲销，再用新凭据重新登记；冲销释放的授权让后续流水前移。
- `review`（复查）和`close`（结案）要求待处理分钟为零，否则返回409并写明各版本缺口。
- `log_service`、`void_service`按凭据幂等，`expected_version`可省略；其余动作仍需携带最新版本号。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
