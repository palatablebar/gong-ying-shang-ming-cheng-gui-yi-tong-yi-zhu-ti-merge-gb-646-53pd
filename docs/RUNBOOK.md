# 运行手册 供应商主数据台账

## 启动

```bash
docker compose up -d --build
curl -s http://127.0.0.1:18205/api/health
```

首次打开任意页面会按内置样例自动采信一次，随后逐级计算归一名、分块、候选对与合并簇。

## 重建数据库

```bash
docker compose down -v
docker compose up -d --build
```

`-v` 会删除命名卷 `merge-data`，批次表与复核决定一并清空。

## 重跑与幂等

- `POST /api/suppliers/import` 带 `force: true` 时按同一批文件重算。
- 批次号由输入摘要（按文件名排序的 sha256 拼接）与规则版本共同决定，同一批文件得到同一个 `run_id`；
  命中已有批次时直接复用结果，不新增记录。
- 复核决定写入后，簇与金额会立即重算，不需要手工触发。
- 下一月名录到达后，先看 `/carry` 的沿用/待复核/退出；待复核项去 `/reviews` 判定。
- 发布：`POST /api/versions/publish`（页面 `/versions` 上的"发布版本"）。内容未变时
  返回已有版本、不新增；写入失败返回 `V_PUBLISH_FAILED`，上一版本保持不变。
- 已发布版本仍可继续判定并再次发布：新版本以旧版本为父版本，旧版本标记为"已被替代"，
  对比用 `/api/versions/diff?before=V-0001&after=V-0002`。

## 常用请求

```bash
curl -s -X POST http://127.0.0.1:18205/api/suppliers/import -H 'Content-Type: application/json' -d '{}'
curl -s http://127.0.0.1:18205/api/reconcile
curl -s http://127.0.0.1:18205/api/carry
curl -s -X POST http://127.0.0.1:18205/api/versions/publish
curl -s http://127.0.0.1:18205/api/export/master.csv -o master.csv
curl -s http://127.0.0.1:18205/api/export/manifest.json -o manifest.json
curl -s http://127.0.0.1:18205/api/versions/V-0001/master.csv -o master-V0001.csv
```

判定一张复核单：

```bash
curl -s -X POST http://127.0.0.1:18205/api/reviews/P-00105/decision \
  -H 'Content-Type: application/json' \
  -d '{"decision":"merge","expected_version":0}'
```

## 失败码处理

| 原因码 | 处理 |
|---|---|
| `E_FILE_MISSING` | 检查 `sample_data/` 下的文件名与大小写 |
| `E_MISSING_COLUMN` | 表头缺必填列，按列契约补列后重试 |
| `E_DECODE_FAILED` | 文件既非 UTF-8 也非 GB18030，需转码 |
| `Q_ROW_SHORT` / `Q_ROW_LONG` | 该行进隔离区，修正分隔符后重跑 |
| `V_STALE_DECISION` | 页面停留过久，刷新后按新版本再提交 |
| `V_BAD_DECISION` | 税号冲突的候选对不允许合并 |
| `V_UNKNOWN_PAIR` | 候选对编号不属于当前批次，刷新页面 |
| `V_PUBLISH_FAILED` | 发布自检未过或写入失败；旧版本未受影响，检查日志后重发 |
| `V_UNKNOWN_VERSION` | 版本号不存在，或该版本没有父版本可供对比 |
| `V_NO_RUN` | 还没有可发布的批次，先触发一次采信 |
| `R_PENDING_KEY_FIELD` | 关键字段变化，需在 `/reviews` 重新判定承接关系 |
| `R_PENDING_IDENTITY` | 两个以上关键字段同时变化或身份键不唯一，按身份冲突处理 |
| `R_PENDING_NEW_MEMBER` | 主体新增成员，确认后才能并入原主体 |

## 测试

```bash
docker compose exec -T app python -m pytest -q
```
