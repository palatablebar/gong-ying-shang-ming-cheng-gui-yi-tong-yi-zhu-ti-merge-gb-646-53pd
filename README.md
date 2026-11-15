# 供应商主数据台账（entitymerge）

集团采购中心把 6 个 ERP 导出的供应商名录并成一份主数据：名称归一、候选对判定、
合并同主体、人工复核边界对、导出主数据与证据行。界面为原生 HTML/CSS/JS，
单体 FastAPI + Jinja2 + SQLite，一条 `docker compose up` 即可演示。

## 能力

- 采信脏文件：GB18030 与 UTF-8 混用、BOM、重复表头、列数不齐、千分位与全角数字、
  空值哨兵、前导零编码、同号不同值、单边引用、空态三态。
- 名称归一：全角、括号、空白、法律后缀、地名标记、拉丁大小写、标点，共 7 条规则。
- 候选对：归一名的字符 2-gram 倒排桶，超大桶跳过并计数，候选对确定性编号。
- 相似判定：0.55 × bigram Dice + 0.45 × 编辑比率；税号硬冲突拒绝、税号相同强制合并、
  城市不同降级复核、得分分档 0.92 / 0.80。
- 合并簇：并查集合并，代表元取税号最小者，簇内税号冲突标记交复核，
  簇金额与成员分摊用 Decimal 逐分守恒。
- 复核：队列按得分降序，决定带版本乐观锁，写入后簇立即重算；引用异常四类可查。
- 跨批次承接：上一版确认的主体关系按业务身份键（集团编码）继承；关键字段变化、
  身份冲突、主体新增成员回到待复核，成员消失记为退出；同批重跑不重复记账。
- 版本发布：正式主数据与复核决定作为同一版本一次性切换，失败整体回滚保留旧版；
  同一批重跑不新增版本，可对比任意两版的主体沿用、待复核、退出与决定差异。
- 导出：主数据、成员分摊、隔离区、冲突、运行清单（含 `run_id` 与输入 sha256），
  以及按版本快照导出的主数据与决定 CSV。

## 模块清单

| 模块 | 入口 | 规则 |
|---|---|---|
| M1 名录采信 | `POST /api/suppliers/import` | 编码回退链、列数校验、同键冲突归因 |
| M2 名称归一 | `GET /api/normalize` | N01 到 N07 七条规则，行业词不剥离 |
| M3 分块候选 | `POST /api/blocks` | bigram 倒排桶，桶上限 120，候选上限 4000 |
| M4 相似判定 | `POST /api/pairs` | 加权得分与四级优先判定 |
| M5 等价类合并 | `POST /api/clusters` | 并查集、代表元、最大余额分摊 |
| M6 复核批次 | `POST /api/reviews/{pair_id}/decision` | 队列、版本锁、引用一致性 |
| M7 主数据导出 | `GET /api/export/master.csv` | CSV 与运行清单，UTF-8 BOM |
| M8 跨批次承接 | `GET /api/carry` | 业务身份键配对，沿用/待复核/退出三类去向 |
| M9 版本发布 | `POST /api/versions/publish` | 主数据与决定同版本切换、失败回滚、前后差异 |

## 页面

`/` 台账总览（含采信诊断与隔离区）、`/normalize` 名称归一、`/pairs` 候选对、
`/clusters` 合并簇、`/reviews` 复核队列、`/carry` 跨批次承接、`/versions` 版本与差异、
`/export` 主数据导出。接口文档在 `/docs`。

## 核心规则

1. 归一名 = 全角转半角 → 括号统一 → 去空白 → 去法律后缀 → 去地名标记 → 拉丁小写 → 去标点。
2. 得分 = 0.55 × bigram Dice + 0.45 × 编辑比率，量化到 6 位小数。
3. 判定优先级：税号冲突（拒绝） > 税号相同（自动） > 城市不同（复核） > 得分分档。
4. 代表元 = 簇内税号最小者；无税号取供应商号最小者。
5. 簇金额 = Σ成员金额（Decimal）；成员分摊按比例取整到分后最大余额补齐。
6. 跨批承接的身份键优先取集团编码 `supplier_no`；关键字段为税号、归一名、城市。
7. 待复核承接的判定不自动沿用；人工重判后记 `R_RECONFIRMED`，随下一版本固化。

字段类型、单位、空值与哨兵、时间与区间定义见 `docs/DATA-DICTIONARY.md`；
陷阱清单与对账规则见 `docs/DEPTH.md`；运维动作见 `docs/RUNBOOK.md`。

## 启动

```bash
docker compose up -d --build
curl -s http://127.0.0.1:18205/api/health
docker compose exec -T app python -m pytest -q
```

宿主机访问 <http://127.0.0.1:18205>。样例数据在 `sample_data/`，六个文件分别覆盖
主档、别名、GB18030、金额与引用、仅表头、全空行。

## 演示步骤

1. 打开 `/`：看到批次号、采信行数、主体数与对账结论。
2. `/` 下方：逐文件诊断与隔离区两行带原因码与原始行号。
3. `/normalize`：原名到归一名的对照，试算框可现场验证一条名称。
4. `/pairs`：按结论过滤候选对，看得分与依据字段。
5. `/clusters`：合并簇与成员分摊，`C-0009` 标税号冲突。
6. `/reviews`：三项带复核，判定后簇数立即变化；引用异常五条。
7. `/carry`：看相对上一版的沿用、待复核与退出，主体逐项对照。
8. `/versions`：发布前差异、发布按钮、版本清单与任意两版对比。
9. `/export`：下载主数据、成员分摊、隔离区、冲突与运行清单。

## 测试

```bash
docker compose exec -T app python -m pytest -q
```

108 条测试：M1 到 M7 每模块 8 到 13 条，M8 承接 11 条、M9 版本 17 条，
另有端到端 4 条与不变量 5 条。

## 目录

```text
app/main.py config.py errors.py reasons.py manifest.py store.py web.py
app/modules/m1_intake … m7_export/{handler.py service.py 规则与读写出}
app/templates/{base.html + 6 页}
app/static/{theme.css layout.css components.css app.js table.js charts.js}
docs/{DEPTH.md DATA-DICTIONARY.md RUNBOOK.md}
sample_data/ 六个脏样例
tests/{conftest.py test_intake … test_export.py test_e2e.py test_invariants.py fixtures/}
```
