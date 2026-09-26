# 实训学时合规与冻结服务

该服务汇聚学员签到、导师确认和请假修正事件，按培养方案与时区重放学时状态，并保存可追溯的学期冻结快照。项目还提供导师分配、证明材料、豁免复核、规则版本、名额、通知和数据留存等领域模块，供后续业务扩展时复用统一的状态与审计约束。

## 分项学时规则

新版培养方案除总学时外，还可分别约束校内（on_campus）、企业（enterprise）、公益活动（public_welfare）等类别的最低与最高占比：

- 规则按版本管理（草稿 → 发布 → 退休），同一方案至多一个已发布版本；发布新版本自动退休旧版本。
- 事件导入时钉住当时的规则版本（`pinned_rule_version`），之后规则升级不改变已入库事件的解释口径；未发布任何规则时导入的事件按旧版总量语义处理。
- 签到负载可携带 `category_windows` 类别授权区间；一次签到跨越多个授权区间时按区间切分归属，落在区间外的时长记为不可计入（`no_authorization_window`）。
- 重放时先合并重叠区间，再按（规则声明顺序、事件号）把每段时间分配给唯一类别，重叠时长绝不重复计数。
- 各类别按 `ceil(总学时 × 最低占比)`、`floor(总学时 × 最高占比)` 精确换算（分数运算，无浮点误差）；超出上限的时长不可计入（`category_cap_exceeded`）。
- 请假修正可带 `category` 定向到类别，负向修正把类别或总量钳制到 0。
- 学生进度给出各类别达标缺口（`shortfall_seconds`）、总缺口（`total_shortfall_seconds`）与不可计入原因（`exclusions`）；冻结快照记录判定所用规则版本及每次签到的类别片段依据。

## API 概览

- `PUT /api/plans/{plan}/rules/{rule}` 创建/覆盖草稿规则，`POST .../publish` 发布，`GET .../rules`、`GET .../rules/current` 查询。
- `GET /api/plans/{plan}/students/{student}/progress` 学生进度（含分项、缺口、不可计入原因）。
- `GET /api/plans/{plan}/warnings?at_risk_only=` 批量预警（总量/类别缺口、待确认、未归属时长）。
- 冻结、快照与差异查询沿用 `/api/plans/{plan}/freezes/...`。

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

测试覆盖事件幂等导入、跨时区与跨日学时合并、实习确认、负向修正、冻结快照和差异查询，以及分项规则的边界取整、跨类别授权区间切分、规则版本固定与升级、批量预警；运行过程中不需要单独的数据库或网络服务。
