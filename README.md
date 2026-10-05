# 活动取消补办管理

本项目维护活动取消补办管理的领域约定、角色边界与样例数据，供后端服务、接口和自动化验证统一使用。当前契约覆盖活动统筹员、讲解员、学校联系人、场馆管理员，并明确取消补办关联、参与者原子迁移、多次改期版本、统计关系去重等关键约束。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/makeup_backend/`：取消补办后端（纯标准库，无第三方依赖）。
  - `models.py`：资源、场次、预约、取消登记、补办候选等领域对象。
  - `store.py`：带全局锁与快照回滚的内存存储，写操作天然具备事务边界。
  - `services.py`：全部确定性规则（登记/确认/退出/改期/恢复/通知/报表）。
  - `api.py`：基于 `http.server` 的 JSON 接口与可直接启动的服务。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约与后端规则回归测试。

## 确定性规则

1. **登记取消**：记录取消原因（编码+说明）与原场次；原场次进入 `cancelled`，资源占用
   “冻结”不释放（防止他人抢占），原签到保留在预约对象上，全部预约挂到同一条补办链。
2. **补办候选**：一条登记可挂多个候选时间/容量/资源方案，确认时二选一，其余置为
   `superseded`。
3. **确认补办（原子转移）**：同事务内校验补办容量与新时间槽的有限资源，随后建立补办
   场次、迁出原场次冻结占用、把链上全部有效参与者迁到补办场次；任一步失败整体回滚，
   不产生半成品场次或预约。
4. **重复确认**：同一候选重复确认幂等返回；已确认后再确认其他候选报冲突（改期需走
   专用流程）。
5. **学生退出不可逆**：退出后不再被任何确认/改期迁移；原场次恢复也不自动回归。
6. **多次改期**：每次生成单调递增的候选版本，旧补办场次置 `cancelled` 并指向新版本，
   参与者与有限资源整体迁移；容量/资源不足整笔回滚，版本号不留空洞。
7. **原场次恢复**：仅允许在尚未确认（`open`）时执行；候选全部作废，有效预约留在原
   场次；已确认后不可恢复，已恢复的登记不可再次取消。
8. **报表去重**：每条补办链至多一个“最终归属场次”计入完成量（已确认链认当前补办，
   未确认/恢复链认原场次），被取代的原场次与旧补办场次永不重复计数；参与人数按
   学生全局去重。接口 `GET /cancellations/{id}/participants` 展示每位参与者的最终
   归属与 `origin → makeup_v1 → makeup_v2 …` 迁移路径。

## 接口

`POST /resources`、`POST /events`、`POST /events/{id}/bookings`、
`POST /bookings/{id}/checkin`、`POST /bookings/{id}/withdraw`、
`POST /events/{id}/complete`、
`POST /cancellations`、`GET /cancellations/{id}`、
`POST /cancellations/{id}/candidates`、`POST /cancellations/{id}/confirm`、
`POST /cancellations/{id}/reschedule`、`POST /cancellations/{id}/restore`、
`POST /cancellations/{id}/notifications`、
`GET /cancellations/{id}/participants`、`GET /report/completions`。

错误码：`400` 参数错误、`404` 对象不存在、`409` 状态/资源冲突。

启动服务：

```bash
python3 -m makeup_backend.api --host 127.0.0.1 --port 8080   # 需将 src 加入 PYTHONPATH
```

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
