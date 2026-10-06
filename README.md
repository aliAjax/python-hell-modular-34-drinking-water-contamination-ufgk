# 市政饮用水污染响应

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8334`。

- `app.py`：服务生命周期和依赖组装。
- `src/domain.py`：水源、污染物、区域和来源校验。
- `src/rules.py`：污染评分、通知去重、停水、切换水源、冲洗、消毒、复检和恢复状态机。
- `src/batches.py`：复检结果按采样批次归集、有效结果计算、批次待判定/裁定、恢复结论重算与历史数据迁移。
- `src/repository.py`：SQLite、事务、重复保护、乐观版本、审计链和外勤离线草稿表。
- `src/service.py`：身份、角色、用例编排、版本冲突自动重试和草稿按批次合并续传。
- `src/http_api.py`：JSON 接口与静态首页。
- `src/audit.py`：审计哈希。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8334
python3 -m unittest discover -s tests -v
```

接口包括 `GET /health`、`GET /api/state`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions`、`POST/GET /api/items/<id>/sample-drafts`、`PUT /api/sample-drafts/sync` 和审计查询。

## 复检批次归集

复检（`sample`）必须带 `batch_id`，来源由提交角色确定（`lab` 实验室 / `field_operator` 现场，也可显式传 `source`，但不能与角色冲突）。

- **同批次同来源**：后到结果顶掉先到结果；先到结果保留并标记 `superseded`，审计链可查，不是物理覆盖。
- **同批次不同来源**：结果并存。结论一致（都合格/都超标）时批次 `effective`；结论冲突时批次 `pending_judgement`，`restore` 被拒绝，需协调员/分析员/监管通过 `adjudicate_batch` 裁定采信哪个来源，或由落后来源重测消除分歧。
- 有效结果只统计有效批次中未被顶替、未被裁定驳回的结果，可通过 `GET /api/items/<id>` 的 `effective_results`、`batch_summary` 查看。

## 恢复结论随有效结果重算

- `restore` 依据当前全部有效结果判定：有超标或待判定批次即拒绝。
- 恢复后仍可收到新复检结果：有效结果一变立即重算。若晚到结果（含批次冲突进入待判定）推翻恢复结论，事件退回 `reinspection_pending`，当前恢复作废，原结论进入 `restoration_history` 并写明退回原因（批次、来源、样本、超限值情况），审计事件携带 `restoration_invalidated`。合格的晚到结果维持恢复有效。
- 待复检状态下重新复检合格后可再次恢复，历史中每次恢复及作废均可查。

## 外勤断网与回网合并

- 断网时 `POST /api/items/<id>/sample-drafts`（带幂等 `client_id`）先存草稿。
- 回网后 `PUT /api/items/<id>/sample-drafts/sync`（或全局 `PUT /api/sample-drafts/sync`）按采样批次分组、按本地产生顺序逐份合并；合并基于领域层追加语义并在乐观版本冲突时自动重读重试，不会互相覆盖。
- 任一份失败立即中止，失败草稿标记 `failed` 并记录原因，响应给出 `resume_from`（item+批次）和 `remaining`；修复后再次同步即从该批次续传，已同步草稿不会重复入库。

## 历史数据升级

服务初始化时自动迁移：旧数据缺少批次号的复检结果补齐为 `LEGACY-<事件id>` 历史批次（标记 `legacy`，继续参与有效结果计算），原恢复记录保留在 `restoration_history` 可查，迁移幂等并写入 `batches_backfilled` 审计事件。

内置规则不替代真实水质模型、法定通报渠道或供水控制系统的联锁。
