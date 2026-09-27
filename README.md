# 公共交通中断改道发布服务

一个仅使用 Python 标准库实现的线路、站点、班次、施工绕行、站点迁移和无障碍变化发布服务。方案按草稿、复核、批准、发布流转；路径计算会应用停运、跳站、绕行、站点迁移和无障碍限制。

代码按职责分层：`rules.py` 只放纯领域规则（窗口判定、图构建、最短路和迁移校验，无存储依赖），`app.py` 负责 SQLite 保存、快照和 HTTP，`static/index.html` 只负责页面与接口调用。

## 运行

```bash
python app.py --init
python app.py --port 8010
```

打开 <http://127.0.0.1:8010>。`--init` 会导入两条示例线路、六个站点和一个 23:50 发车的跨日班次。数据库默认是 `transit_disruption.db`，可用 `--db` 或 `TRANSIT_DB` 修改。

## 业务能力

- 基础数据导入会一次性检查线路、站点经纬度、连续站序、重复站点、站间行驶时间和班次时间。错误批次写入 `import_errors` 后整体拒绝，不留下半批数据。
- 中断事件可以包含 `stop_closure`、`skip_stop`、`detour`、`accessibility_change`、`stop_relocation`，可以设置服务日分钟窗口。
- 路径使用 Dijkstra 算法比较基线与方案版本；跳站时车辆可继续通过，但乘客不能在跳站上下车，经过省略路段的行驶时间会计入下一段。
- `stop_relocation` 登记原站 `stop_id`、临时站 `to_stop_id`、步行分钟 `travel_minutes` 和生效窗口。窗口内车辆按原站序在临时站停靠，到/发原站的查询会把原站↔临时站的 `walk` 步行边计入耗时（穿越该站的乘车行程不步行）；窗口外自动恢复原站停靠。
- 临时站不具备无障碍条件时，无障碍查询不会把乘客导向该临时站：以该原站为起讫点的行程返回 `unreachable`，穿越该站的行程仍给出可走路线；窗口结束后原站恢复可用。
- 班次时间以服务日零点起算，允许超过 1440 分钟。例如 1430 分发车、21 分钟到达会显示为次日 `00:21`。
- 修改只允许发生在草稿版本；创建新版本会复制父版本变更，已发布快照继续保留。
- 发布在一个 SQLite 事务内写入方案快照和 SHA-256，旧发布版本不会被覆盖。

## API

使用 `X-User`、`X-Role` 身份头，角色包括 `planner`、`editor`、`reviewer`、`admin`。

- `POST /api/import`：导入基础数据。
- `POST /api/disruptions`：创建中断事件及第一版草稿。
- `POST /api/disruptions/{id}/versions`：从指定父版本复制出新草稿。
- `POST /api/versions/{id}/changes`：向草稿添加停运、跳站、绕行、无障碍变化或站点迁移（`{"kind":"stop_relocation","stop_id":2,"to_stop_id":7,"travel_minutes":6,"effective_start_minute":480,"effective_end_minute":1200}`）。
- `POST /api/versions/{id}/submit|approve|reject|publish`：完成复核发布流程。
- `GET /api/route?from=1&to=5&version_id=1&at_minute=1430&accessible=true`：查询路径、耗时和到达时间。
- `GET /api/trips/{id}`：查看跨日班次各站时间。
- `GET /api/import-errors`：查看被隔离的错误批次。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖基线/改道路径、版本复制与发布隔离、审批冲突、无障碍路径、站点迁移步行与窗口恢复、临时站无台阶时的无障碍绕行、跨日时刻和坏数据整批隔离。
