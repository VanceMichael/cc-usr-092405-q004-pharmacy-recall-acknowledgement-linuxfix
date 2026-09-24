# 药房店铺身份服务

管理药房店铺身份事实、经营关系的时间版本，以及紧急药品召回的双时点定责、触达、回执与升级。Flask 负责 HTTP 边界，SQLAlchemy Core 连接 SQLite；默认数据库文件为 `data/pharmacy_identity.sqlite3`，可用 `DATABASE_PATH` 改址。

```bash
python -m pip install -r requirements.txt
python -m alembic upgrade head
pytest
flask --app 'pharmacy_identity:create_app()' run
```

## 核心问题：责任归属的时间错位

质量负责人掌握的是**药品批次**与**当前门店**，但召回责任既不能用今天的经营主体、许可证或加盟关系代替**进货当日**的归属，也不能只看历史而漏掉转手后的实体门店与线上入口。服务因此对每个物理入口（实体门店 `store` / 线上入口 `online_entry`）在召回版本创建时**冻结双时点结论**：

| 时点 | 取值方式 |
| --- | --- |
| 供货发生时 | 按每笔 `supplied_at` 回放 `outlet_relations` 的半开时间窗 `[valid_from, valid_to)` |
| 召回生效时 | 按版本的 `effective_at` 回放同一关系链（非查询当下——视图必须随版本冻结、可复核） |

定责结论逐入口留痕（`recall_targets.status` + `reason_code/reason_detail`）：

- `included`：两个时点责任主体一致 → 纳入触达；
- `transferred`：供货后转手新主体 → **转交新主体处置**，进货当日主体保留事实并收 `historical_fyi` 备查；
- `historical`：生效时入口已退网/关店 → 进货当日主体兜底，不漏单；
- `unresolved`：供货当日无有效关系、责任链断裂 → 不自动派发，列入风险入口等待总部**人工改派**；
- `excluded`：供货晚于生效时刻（逐笔判定，非整店一刀切）或当前主体已属**品牌外**（不静默丢弃，提交跨品牌协查风险项）。

每笔供货当日命中的关系同时冻结在 `recall_target_supplies`，时间线可逐笔追溯。

## 召回版本与批号纠错

召回记录携带产品、生产批号、风险等级（`low/medium/high`）与生效时刻。批号录入错误时调用纠错接口开启**新版本**：旧版本及其全部回执、通知、升级记录原样冻结保留；新版本按新批号重新定责，同一物理入口在前一版本的终态处置（停售/隔离/未发现）以 `disposition_carried` 方式沿用，原始回执不复制、不重放，待发通知作废。

## 回执

门店/设备回报 `stop_sale`（停售）、`quarantined`（隔离）、`not_found`（未发现）、`dispute`（归属异议）。

- **设备离线上传**以 `(device_id, journal_no)` 为幂等键，重放只回放首条结果；
- **迟到不压新**：以设备自报 `submitted_at` 判定，不晚于当前最新处置的回报（含迟到的异议）标记 `superseded`，留存但不覆盖状态、不重复开异议单；
- 异议进入复核队列：自提自审禁止；**发起过该入口任何一段关系变更的人必须回避**；复核可维持原归属（解除挂起、逾期计时未暂停过）或改派。

## 逾期升级与停服接续

高风险 SLA 锚定生效时刻：24h 截止；逾期当时 L1（责任主体质量负责人）、+12h L2（主体负责人）、+36h L3（品牌总部）。中/低风险只计截止、不自动升级。

全部截止与升级点是**绝对时刻**，通知走 outbox（`pending/sent/cancelled`，键含版本/入口/层级/类型/收件人）。服务停摆期间不计时、不丢消息：恢复后 `/sweep` 一次扫描即按当前时间补齐所有跨点升级与到期通知，再次扫描为空——进度从原处接续，而非重新计时。

## 可见域

- 总部账号只能看/操作**所属品牌**（召回、覆盖率、风险入口、目标清单、时间线、外发箱、关系变更均强制品牌域）；
- 门店与线上入口通过 `GET /me/tasks` 只获得完成处置所需信息：药品、批号、风险、截止、纳入原因与允许的回报类型，看不到其他入口或品牌全景；
- 经营主体成员只看到自己当前负责的入口（转交后归新主体）。

## 主要接口

| 方法与路径 | 说明 |
| --- | --- |
| `PUT /admin/facts/{brand\|operator\|actor\|store\|online_entry}` | 身份主数据 |
| `POST /relations` | 登记带时间窗的关系（新窗口自动收口旧窗口，总部、限本品牌） |
| `POST /supplies` | 供货事件（产品/批号/入口/时刻，由溯源系统写入） |
| `POST /recalls` | 创建召回（产品/批号/风险/生效时刻），同步冻结双时点定责 |
| `POST /recalls/{id}/corrections` | 批号/产品纠错，开启新版本 |
| `POST /receipts` | 门店/设备回执（可凭设备流水匿名上传） |
| `POST /disputes/{id}/review` | 异议复核（维持/改派，强制回避） |
| `POST /versions/{id}/targets/{type}/{tid}/reassign` | 总部人工改派 |
| `POST /sweep?at=` | 到期外发与逾期升级（调度器周期调用；`at` 可注入演练时钟） |
| `GET /versions/{id}/coverage` | 覆盖率、处置分布、逾期、各级升级、人工改派计数 |
| `GET /versions/{id}/risk-entries` | 定责断裂/品牌外移/升级未闭环风险入口 |
| `GET /versions/{id}/targets` | 入口清单与逐条纳入/排除/转交理由 |
| `GET /versions/{id}/targets/{type}/{tid}/timeline` | 定责、回执、改派、异议、升级时间线 |
| `GET /me/tasks` | 门店/主体的最小处置视图 |
| `GET /notifications` | 通知外发箱（总部） |

请求用 `X-Actor-Id` 标识操作者；`X-Current-Time` 可在演练/测试中重放时钟。

## 开发检查

- 编译检查：`python3 -m compileall -q src`
- 测试：`pytest`（内存 SQLite 建表，覆盖双时点定责、去重、迟到、纠错、升级接续与全部权限边界）
