# 固件成组放行 HTTP API

本地运行的固件**成组放行（grouped rollout）**服务：FastAPI + SQLite，单文件数据库，
Docker Compose 一条命令启动。

## 它做什么

1. **登记设备组**：一组 2～8 台设备，每台有唯一设备 ID 和当前固件版本。
2. **创建放行批次**：为整组创建一个带唯一批次 ID 的批次，逐台指定目标版本；
   同一设备组只要还有未结束（`open`）的批次，就不能开新批。
3. **设备提交回执**：设备按批次提交 `ready`（已准备好切换）或 `failed`（升级失败）回执。
   - 同一设备、**同一结果**重放 → 幂等成功（响应体 `replayed: true`）；
   - 同一设备**改报另一种结果** → `409 Conflict`；
   - **任一 `failed`** → 批次立即失败（`failed`），所有设备当前版本保持原样；
   - **全部 `ready`** → 在**同一个数据库事务**内切换所有设备版本，并把批次标记为
     `effective`。任何查询都不会读到"只切了一半"的中间状态。
4. **批次结束后**：只接受已有回执的幂等重放；旧批次迟到的**新**回执被拒绝（`409`），
   不会影响旧批结论，也不会影响后续批次。
5. **并发提交**：所有写操作走 `BEGIN IMMEDIATE` 事务，SQLite 写锁全局串行化，
   并发结果必然等价于某个串行顺序。

## 快速启动

需要 Docker（含 Compose 插件）。

```bash
docker compose up --build -d
```

- 服务端口：**`8080`**
- 数据库：SQLite 文件 `/data/firmware_release.db`，保存在 Docker 命名卷
  `firmware-data` 中，容器重建数据不丢。
- 健康检查：`GET http://localhost:8080/health`
- 交互式 API 文档（Swagger UI）：`http://localhost:8080/docs`

停止：`docker compose down`（加 `-v` 会同时删除数据卷）。

### 不使用 Docker，本地直接运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
# 数据库路径可用环境变量覆盖：
# FIRMWARE_DB_PATH=/var/lib/firmware/fw.db uvicorn app.main:app --port 8080
```

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/groups` | 登记设备组（2～8 台，设备 ID 唯一） |
| `GET` | `/groups` | 列出所有设备组 |
| `GET` | `/groups/{group_id}` | 查询设备组及每台设备**当前版本** |
| `POST` | `/groups/{group_id}/batches` | 为整组创建放行批次（逐台目标版本） |
| `GET` | `/groups/{group_id}/batches` | 列出某组的全部批次 |
| `GET` | `/batches/{batch_id}` | 查询批次状态、逐台目标版本与全部回执 |
| `POST` | `/batches/{batch_id}/receipts` | 设备提交 ready / failed 回执 |

批次状态：`open`（收回执中）→ `effective`（全部 ready，版本已切换）或
`failed`（出现 failed）。

## 调用示例

### 1. 登记设备组（3 台，均在 1.0.0）

```bash
curl -s -X POST http://localhost:8080/groups \
  -H 'Content-Type: application/json' \
  -d '{
        "name": "line-a",
        "devices": [
          {"device_id": "dev-1", "current_version": "1.0.0"},
          {"device_id": "dev-2", "current_version": "1.0.0"},
          {"device_id": "dev-3", "current_version": "1.0.0"}
        ]
      }'
```

返回（`201`）：

```json
{
  "id": "grp_68e7fe8135d34174812ff58507c15bf8",
  "name": "line-a",
  "created_at": "2026-09-29T22:23:05+00:00",
  "devices": [
    {"device_id": "dev-1", "current_version": "1.0.0"},
    {"device_id": "dev-2", "current_version": "1.0.0"},
    {"device_id": "dev-3", "current_version": "1.0.0"}
  ]
}
```

把组 ID 存到变量：`GID=grp_68e7fe8135d34174812ff58507c15bf8`

> 设备数量不在 2～8 之间、组内设备 ID 重复 → `422`。

### 2. 创建放行批次（逐台目标版本，必须覆盖整组）

```bash
curl -s -X POST http://localhost:8080/groups/$GID/batches \
  -H 'Content-Type: application/json' \
  -d '{
        "targets": [
          {"device_id": "dev-1", "target_version": "2.0.0"},
          {"device_id": "dev-2", "target_version": "2.0.0"},
          {"device_id": "dev-3", "target_version": "2.1.0"}
        ]
      }'
```

返回（`201`）中含唯一批次 ID（`bat_...`），状态为 `open`。

- 目标设备少一台 / 多一台 / 含陌生设备 → `422`（响应中给出 `missing_devices`、
  `unknown_devices`）。
- 该组已有 `open` 批次时再开 → `409`：

```json
{"detail": {"message": "该组存在尚未结束的批次，不能开新批",
            "open_batch_id": "bat_10e19d236d044bf2a815c459d4502b23"}}
```

### 3. 设备提交回执

```bash
BID=bat_7aa0a9dbef924f75bc2ec1c9f95aac9b

# 前两台 ready（批次仍 open，版本不变）
curl -s -X POST http://localhost:8080/batches/$BID/receipts \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"dev-1","result":"ready","version":"1.0.0"}'
curl -s -X POST http://localhost:8080/batches/$BID/receipts \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"dev-2","result":"ready","version":"1.0.0"}'

# 同结果重放 → 200，"replayed": true，不产生重复回执
# 改报 failed → 409
```

最后一台也 `ready` 时，响应中批次变为 `effective`，
`receipt_counts` 为 `{"ready":3,"failed":0,"total":3}`，
同一事务内版本已全部切换：

```bash
curl -s -X POST http://localhost:8080/batches/$BID/receipts \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"dev-3","result":"ready","version":"1.0.0"}'
```

失败分支——任一台 `failed` 即 `failed`，**版本一台都不会变**：

```bash
curl -s -X POST http://localhost:8080/batches/$BID/receipts \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"dev-2","result":"failed","version":"1.0.0"}'
# -> status: "failed"；GET /groups/$GID 中所有设备仍是旧版本
```

回执规则与状态码：

| 情况 | 状态码 |
|---|---|
| 首次提交成功 | `200`，`replayed: false` |
| 同设备同结果重放（批次结束前后均可） | `200`，`replayed: true` |
| 同设备改报另一种结果 | `409` |
| 批次结束后，此前没回执过的设备迟到提交 | `409` |
| 设备不在该批次名单内 | `422` |
| 批次不存在 | `404` |

### 4. 查询

```bash
# 设备组当前版本
curl -s http://localhost:8080/groups/$GID

# 某组的批次列表
curl -s http://localhost:8080/groups/$GID/batches

# 批次详情：状态 + 逐台目标版本 + 逐台回执 + 计数
curl -s http://localhost:8080/batches/$BID
```

批次结束（`effective`/`failed`）后即可为该组开下一批；历史批次和回执永久保留可查。

## 并发正确性说明

- SQLite 开启 **WAL**；所有写路径使用 `BEGIN IMMEDIATE`，进入事务即拿写锁，
  写事务全局串行（配合 `busy_timeout` 等待，极端锁竞争时应用层整体重试整个事务）。
- 一次回执在**同一个写事务**里完成：插入回执 → 判定是否全部 ready / 出现 failed
  → 批量更新 `devices.current_version` → 更新批次状态。因此：
  - 全 ready 时的"逐台切换 + 批次生效"对外是一个原子动作，不存在部分切换；
  - 出现 failed 时根本不会执行版本更新；
  - 任意并发交错的最终状态，都等价于把各回执按某个顺序串行执行的结果。
- 查询使用只读快照事务（`BEGIN`），同一请求内读到的版本与批次状态自洽一致。
- 数据库层还有一道兜底：`batches` 上 `status='open'` 的部分唯一索引，
  保证即使应用层竞争，同一组也最多只有一个未结束批次。

## 测试

```bash
pip install pytest httpx
python3 -m pytest tests/ -q
```

测试覆盖：2～8 台约束、目标必须整组覆盖、开批互斥、幂等重放、改报冲突、
failed 版本不动、全 ready 原子生效、结束后迟到回执拒收，以及多线程并发下
"只可能有一个 open 批"、"失败时绝不部分切换"、"并发读到的版本集合只能是
全旧或全新"等串行化不变量。

## 项目结构

```
app/
  main.py       # FastAPI 路由
  schemas.py    # 请求模型与校验
  services.py   # 业务逻辑（全部写操作在单事务内）
  db.py         # SQLite 连接、建表、事务工具
tests/
  test_api.py   # 端到端 + 并发测试
Dockerfile
docker-compose.yml
requirements.txt
```
