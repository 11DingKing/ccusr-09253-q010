# 实训学时合规与冻结服务

该服务汇聚学员签到、导师确认和请假修正事件，按培养方案与时区重放学时状态，并保存可追溯的学期冻结快照。项目还提供导师分配、证明材料、豁免复核、规则版本、名额、通知和数据留存等领域模块，供后续业务扩展时复用统一的状态与审计约束。

## 运行方式

默认数据保存在项目目录的 SQLite 文件中。安装依赖后执行 `uvicorn app.main:app --host 127.0.0.1 --port 8000`，健康检查地址为 `/health`，业务接口位于 `/api`。

## 测试

```bash
python3 -m pytest -q
```

## 编译检查

```bash
python3 -m compileall -q app tests
```

测试覆盖事件幂等导入、跨时区与跨日学时合并、实习确认、负向修正、冻结快照和差异查询；运行过程中不需要单独的数据库或网络服务。

## 分项占比规则与规则版本

新版培养方案可以在总学时之外，按活动类别（如校内 `on_campus`、企业 `enterprise`、公益 `public_welfare`）分别限制最低与最高占比。占比以千分比整数配置，按 `required_seconds * permille // 1000` 向下取整换算为秒，避免浮点误差。

- 规则以不可变版本保存，创建方案时会自动注册隐式旧版规则 `rv-default`（无类别、无授权区间，保持既有重放语义）。
- 导入事件时把方案当前生效的规则版本固定到事件上；重放时每个事件按各自固定的版本解析类别映射与授权区间，方案当前生效版本决定分项阈值。规则升级不会改写历史事件的口径。
- 重放内核先按授权区间截断签到（区间外时长记为不可计入），再合并重叠区间并按类别优先级分配时长，最后按类别上限截顶，产出各分项的计入/缺口/溢出与不可计入原因。
- 冻结快照记录规则版本与各分项依据（千分比、换算阈值、计入与截顶秒数），冻结后不受后续事件影响。

### 相关接口

- `PUT /api/plans/{plan_version}/rules/{rule_version}` 创建规则版本（`activate=true` 时同时启用；同版本不同内容返回 409）
- `POST /api/plans/{plan_version}/rules/{rule_version}/activate` 启用已有版本
- `GET /api/plans/{plan_version}/rules`、`GET /api/plans/{plan_version}/rules/{rule_version}` 查询规则
- `GET /api/plans/{plan_version}/students/{student_id}/progress` 学生进度（含分项明细与不可计入原因）
- `GET /api/plans/{plan_version}/warnings` 批量预警（总缺口与分项缺口，`include_compliant=true` 时含已达标学生）
