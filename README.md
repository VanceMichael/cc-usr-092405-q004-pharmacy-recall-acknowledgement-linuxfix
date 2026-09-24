# 药房店铺身份服务

管理药房店铺身份事实、关系版本与药品紧急召回的触达、回执、异议与升级。Flask 负责 HTTP 边界，SQLAlchemy Core 连接 SQLite；默认数据库文件为 `data/pharmacy_identity.sqlite3`，可用 `DATABASE_PATH` 改址。

```bash
python -m pip install -r requirements.txt
python -m alembic upgrade head
pytest
flask --app 'pharmacy_identity:create_app()' run
```

## 要解决的问题

紧急召回中，质量负责人掌握的是**药品批次与当前门店**，但责任归属必须回到**进货当日**的事实：
不能用今天的经营主体、换发后的许可证或新的加盟关系倒推历史责任，否则换过主体的实体门店和线上入口会被漏触达，
或被错派给不相干的现主体。本服务据此扩展身份模型与召回流程。

## 核心模型：双时间轴

- 关系切片 `entity_relations` 带 `valid_from / valid_until`（业务生效期）与 `recorded_at / recorded_by`（系统记录期）。
  关系变更只**闭合旧切片、插入新切片**，绝不重写历史；许可证 `entity_licenses` 同样按有效期版本化。
- 召回建档（携带产品、生产批号、风险等级、生效时刻）时，对每个收过该批号的入口分别做两次 as-of 查询：
  - **供货发生时**的有效关系/许可证 → 历史责任主体（快照进 `supply_time_entity_id`、`supply_license_no`）；
  - **召回生效时**的有效关系 → 当前经营主体（快照进 `current_entity_id`）。
- 每个入口生成一条带理由的判定 `recall_targets`：

| 情形 | disposition | 触达对象 | reason_code |
|---|---|---|---|
| 两时点主体一致 | `included` | 该主体（直营/加盟关系连续） | `same_entity` |
| 主体已变更（直营↔加盟、换照、并购） | `transferred` | **当前主体**承担处置；供货时主体收到溯源告知 | `entity_changed` |
| 现无有效主体（关系结束/注销） | `included` | 供货时主体，避免漏触达实体门店 | `no_current_entity` |
| 供货当日即无任何责任关系 | `excluded` | 无（转人工溯源），绝不凭空派给今天的主体 | `no_attributable_entity` |

实体门店与**线上入口**（`stores.entry_type`）统一进入目标集合，只按身份事实区分，不按渠道漏掉。

## 回执、异议与改派

- 门店回报四类动作：`stopped_sale`（停售）、`quarantined`（隔离）、`not_found`（未发现）、`attribution_dispute`（归属异议）。
- **设备离线上传以自身流水去重**：`(device_id, client_seq)` 唯一，重放幂等回显。
- **迟到回执不得压过较新处置**：按回执声明的处置发生时刻 `action_at` 比较，较早的事件标记 `applied=0 / late_superseded`，
  仍留在 `receipts` 流水里供审计，但不回写目标当前状态。
- 异议进入 `disputes`；待裁期间该目标**暂停逾期升级**。复核规则：
  - 登记/变更过该门店关系的人（切片 `recorded_by`）**不能参与同一异议复核**（服务端强制 403）；
  - 总部复核人只能处理**所属品牌**；
  - 成立且给新主体 → `reassignments(kind=transfer)`：触达转交、旧处置清零、旧升级链**吊销留痕**并以改派时刻为新主体重排；
  - 成立且无承接主体 → 摘除（`exclude`）。

## 高风险升级与停机接续

- 仅高风险生成升级排期，截止时刻是相对召回版本基准时刻的**绝对时间**（店长 +4h、主体质量负责人 +12h、总部应急 +24h）。
  停机多久都不会重置；`POST /v1/ticks/run-due` 可注入 `as_of` 做时间推进与补发。
- 所有通知先落 `notifications(status=queued)`，传输成功才标 `sent`；发送失败的不标记、下次重试，
  恢复后按入队顺序补发——**逾期计时与未发通知都从原进度接续**。

## 批号纠错（新版本）

批号错误不开新召回单：`POST /v1/recalls/{id}/versions` 生成新版本号，按新批号重建目标，
并把上一版本各入口**已完成的处置结转**（`receipts.carried_from_version_id` 留痕），旧版本的目标、动作、时间线原样保留。

## 权限视图

- 总部看板 `GET /v1/recalls/{id}/dashboard`：限所属品牌；**按版本**给出覆盖率（分母只算 included/transferred）、
  风险入口（线上入口与高风险未完成项）、人工改派记录与每个入口的完整回执时间线，全部基于版本快照，不随后续关系漂移改写。
- 门店视图 `GET /v1/targets/{id}/briefing`：只返回完成处置所需信息（产品、批号、风险、生效时刻、动作清单、当前状态），
  不含其他主体、许可证号、品牌覆盖率。

所有接口用 `X-Actor-Id` 头标识调用方；管理端接口在 `/v1/admin/*`（品牌/主体/门店/账户/许可证/关系/供货登记）。

## 开发检查

- 编译检查：`python3 -m compileall -q src tests`
- 测试：`pytest`（覆盖双时点判定、设备去重与乱序、升级分层与停机恢复、批号纠错版本结转、品牌隔离/最小披露/复核回避）
