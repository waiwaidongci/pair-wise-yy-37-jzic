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
- `POST /api/items/{id}/records/{record_id}/status`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/queue?day=YYYY-MM-DD`：查看日容量队列（批次、条目、名额）
- `POST /api/queue/dispatch` `{day}`：按优先级与当前负荷派发日容量队列
- `POST /api/queue/capacity` `{inspector,day,capacity}`：设置检查员当天名额
- `GET /api/queue/capacity?day=YYYY-MM-DD`：查看当天名额与剩余
- `POST /api/queue/items/{id}/claim`：认领检查条目（先到者占用，后到者见剩余名额）
- `POST /api/queue/items/{id}/execute`：执行已认领条目并保留原依据
- `GET /api/audit`

允许角色：applicant, inspector, compliance_manager, viewer。申报量超过许可量或检查发现高严重度问题时提高优先级；存在未关闭整改时不能批准。

日容量队列把许可、整改事项与检查批次接成按日排期的容量队列：严重度、申报量与未关闭整改共同决定优先级，检查员当天名额按当前负荷负载均衡分配。整改事项变化后，未执行批次立即按新优先级重算并退回待派，已执行结果保留原依据。派发写入失败后从最后完成的许可继续，已占用名额的许可重试时跳过，不重复占名额。认领采用原子条件更新，两名检查员同时认领时先到者占用，后到者在 409 响应中看到剩余名额。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
