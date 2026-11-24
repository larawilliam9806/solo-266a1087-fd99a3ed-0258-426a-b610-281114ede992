# 园区小时电量对账服务

FastAPI + SQLite 的小时电量对账接口。电表树固定登记，按 UTC 整点报送非负整数瓦时；报送、订正、撤回后只重算该表及其上级，并把报数与核算结论在同一个 SQLite 事务中提交。

## Docker Compose 启动

```bash
docker compose up --build
```

服务监听 <http://localhost:8000>：

- 健康检查：<http://localhost:8000/health>
- Swagger 文档：<http://localhost:8000/docs>
- SQLite 数据保存在 compose 卷 `meter_ledger` 内的 `/data/ledger.db`（实际卷名带项目名前缀）

也可以本地运行：

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
# 或 uvicorn main:app
```

## 内置固定电表树

首次启动且 `SEED_DEMO_TREE=true`（compose 默认）时登记：

```text
PARK-001 园区总表
├── BLDG-A A 栋总表
│   ├── A-1F
│   └── A-2F
├── BLDG-B B 栋总表
│   ├── B-1F
│   └── B-2F
└── EV-01 充电站直属表
```

同级子表计量范围互不重叠，所有末级表合起来覆盖上级表。也可以通过接口登记测试或现场的固定树。

## 核算规则

对每个**当前有报数且有子表**的电表：

1. 从它的每个直属子表开始向下找。
2. 某个分支一旦遇到有当前报数的电表，就取该表的当前报数，并停止继续向下。
3. 因此同一个核算中取到的表构成互不相叠的一层“报数边界”。
4. 若所有末级表都被这层边界覆盖：
   - `difference = 本表报数 - sum(所用表号的报数)`
   - 返回 `used_meter_ids` / `used_readings`
5. 若尚未覆盖齐全：
   - `complete=false`
   - `difference=null`
   - `missing_meter_ids` 列出仍缺报的末级表。

只有有本表报数且有子表的表才会产生结论。叶子表或未报表本身没有核算结论，但其报数会进入上级核算。

## 时间与数值

- `hour` 必须是 UTC 整点，例如 `2026-01-01T00:00:00Z`。
- 接受带 `Z`、`+00:00` 的 ISO 8601、`YYYYMMDDHH`、Unix 秒/毫秒；naive 时间按 UTC 处理。
- 非整点返回 `422`。
- `amount` 单位为瓦时（Wh），必须是非负整数。

## 主要 HTTP 接口

### 1. 登记电表树

```http
POST /meters/tree
Content-Type: application/json
```

支持平铺邻接表：

```json
{
  "meters": [
    {"meter_id": "P", "parent_id": null},
    {"meter_id": "A", "parent_id": "P"},
    {"meter_id": "A1", "parent_id": "A"},
    {"meter_id": "A2", "parent_id": "A"}
  ]
}
```

也支持嵌套 `children`。加 `?replace=true` 可清空旧账并整树替换，便于测试初始化。

查询：

```http
GET /meters
GET /meters/tree?hour=2026-01-01T00:00:00Z
```

### 2. 报送、订正、撤回

报送（已有有效报数时返回 `409`）：

```bash
curl -X POST http://localhost:8000/readings \
  -H 'Content-Type: application/json' \
  -d '{"meter_id":"A1","hour":"2026-01-01T00:00:00Z","amount":120}'
```

订正 / 幂等写入当前值：

```bash
curl -X PUT http://localhost:8000/readings \
  -H 'Content-Type: application/json' \
  -d '{"meter_id":"A1","hour":"2026-01-01T00:00:00Z","amount":135}'
```

撤回：

```bash
curl -X DELETE http://localhost:8000/readings \
  -H 'Content-Type: application/json' \
  -d '{"meter_id":"A1","hour":"2026-01-01T00:00:00Z"}'
```

等价动作路径：

- `POST /readings/report`
- `POST /readings/correct`
- `POST /readings/withdraw`
- `POST /report`、`POST /correct`、`POST /withdraw`

也支持路径参数：

```http
PUT /readings/A1/2026-01-01T00:00:00Z
DELETE /readings/A1/2026-01-01T00:00:00Z
```

### 3. 按小时查询已存结论

集合接口直接返回数组：

```bash
curl 'http://localhost:8000/reconciliations?hour=2026-01-01T00:00:00Z'
```

对象接口包含 `results`、`conclusions`、`by_meter` 和带结论的 `tree`：

```bash
curl 'http://localhost:8000/reconcile?hour=2026-01-01T00:00:00Z'
```

单表结论：

```bash
curl 'http://localhost:8000/reconciliations/P?hour=2026-01-01T00:00:00Z'
```

结论示例（完整覆盖）：

```json
{
  "meter_id": "P",
  "hour": "2026-01-01T00:00:00Z",
  "status": "complete",
  "complete": true,
  "own_reading": 1000,
  "sum_used": 950,
  "used_meter_ids": ["A1", "A2", "B"],
  "used_readings": [
    {"meter_id": "A1", "amount": 100},
    {"meter_id": "A2", "amount": 200},
    {"meter_id": "B", "amount": 650}
  ],
  "missing_meter_ids": [],
  "difference": 50,
  "balanced": false
}
```

覆盖不完整时：

```json
{
  "meter_id": "P",
  "status": "incomplete",
  "complete": false,
  "difference": null,
  "used_meter_ids": ["D1"],
  "missing_meter_ids": ["L1", "L2"]
}
```

查询只读取已入库结论，不会在 GET 时临时补算。即使上级 `difference=0`，`/reconciliations?hour=...` 与树接口仍会照常列出所有下级已存结论。

## 增量与并发一致性

- 报数写入后，只加载该小时快照，重算“该表 → 父 → 祖父 → …”这一条链。
- 未受影响的旁支结论不执行 UPSERT，其 `version`、`computed_at` 保持不变。
- SQLite 写事务使用 `BEGIN IMMEDIATE`，进程内另有写锁串行化事务；compose 中 uvicorn 使用单 worker。
- 因此并发报数等价于某个全部成功请求依次执行的顺序，账与结论一致。
- 报数、受影响结论在同一事务提交；任一步失败均 `ROLLBACK`，保留此前账目。

## 测试

```bash
pip install -r requirements-dev.txt
pytest -q
```

测试覆盖跨层取数、缺末级表、订正/撤回、零差额仍列下级、旁支不重算、失败回滚和线程并发。
