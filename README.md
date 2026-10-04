# 空气污染源许可与合规检查

管理设施、排放口、治理设备、现场检查、整改和许可续期。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8314
```

默认端口为`8314`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`
- `POST /api/inspectors`（compliance_manager）、`GET /api/inspectors`
- `POST /api/schedule/{day}/build`：构建/续建某日检查队列（检查员、监管员）
- `GET /api/schedule/{day}`：当日队列、各检查员负荷与剩余名额
- `POST /api/batches/{id}/claim`：检查员认领（`inspector_id`）
- `POST /api/batches/claim-next`：按队首自动认领（可带`day`）
- `POST /api/schedule/{day}/dispatch-next`：按当前负荷自动派单
- `POST /api/batches/{id}/execute`：认领检查员执行，结果与依据冻结
- `POST /api/records/{id}/close`：关闭整改事项

允许角色：applicant, inspector, compliance_manager, viewer。申报量超过许可量或检查发现高严重度问题时提高优先级；存在未关闭整改时不能批准。

## 日容量检查队列

- 待检查许可（submitted/correction）按优先级入当日队列，优先级由**严重度、申报量/许可量比、未关闭整改数**共同决定，同级按创建先后；紧急许可不会再被一般许可挤出。
- 检查员有每日名额（`daily_capacity`）；自动派单优先给**剩余名额最多（并列时当前负荷最低）**的检查员。
- 认领通过条件更新（`WHERE status='queued'`）+ 库锁保证先到先得；后到者收到409并可在`GET /api/schedule/{day}`看到扣减后的剩余名额。
- 整改事项新增（open）或关闭后，该许可**未执行批次立即重算优先级并退回待派**、释放名额；已执行批次的依据快照（priority/severity/quantity/threshold/open_records）与结果永久保留。
- 建队按许可逐条提交并记录游标（`schedule_cursors`）；写入失败后重入从**最后完成许可**继续，`UNIQUE(item_id,day)`与游标保证重试不重复、不重复占名额。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
