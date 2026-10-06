# 市政饮用水污染响应

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8334`。

- `app.py`：服务生命周期和依赖组装。
- `src/domain.py`：水源、污染物、区域和来源校验。
- `src/rules.py`：污染评分、通知去重、停水、切换水源、冲洗、消毒、复检和恢复状态机。
- `src/repository.py`：SQLite、事务、重复保护、乐观版本和审计链。
- `src/service.py`：身份、角色和用例编排。
- `src/http_api.py`：JSON 接口与静态首页。
- `src/audit.py`：审计哈希。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8334
python3 -m unittest discover -s tests -v
```

接口包括 `GET /health`、`GET /api/state`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions`、`POST /api/items/<id>/draft`、`GET /api/items/<id>/drafts/<client_draft_id>` 和审计查询。测试覆盖完整响应流程、重复事件、重复通知、复检阈值、权限、版本冲突、复检批次归集、结论重算与恢复退回、外勤草稿幂等续传和历史批次升级。内置规则不替代真实水质模型、法定通报渠道或供水控制系统的联锁。

## 复检批次归集与结论重算

复检结果按采样批次归集到 `payload.sampling_batches`，不再只挂一条：

- 同批次同来源（`lab` / `field`）后到顶掉先到，记录 `replaced`。
- 同批次不同来源并存，批次置为 `pending_judgment`（待判定），有效浓度取最保守（最大）值；协调员/监管员用 `judge` 操作采纳某一来源后批次回到 `consistent`。
- 有效结果一变就重算 `recovery_conclusion`；恢复完成后若晚到结果推翻结论（有效浓度超过限值），已完成恢复退回 `sampled`（待复检），原恢复记录进 `restoration_history` 并写明 `revoke_reason`。

`sample` 操作需带 `batch_id`、`sample_id`、`zone_id`、`concentration`，来源由角色决定（`lab`→实验室，`field_operator`→现场），也可显式传 `source_type`。

## 外勤断网草稿

外勤断网先存草稿，回网后用 `POST /api/items/<id>/draft` 一次提交多条结果，按批次合并。请求带 `client_draft_id`，同一草稿重复提交幂等（已提交的批次/来源跳过），失败后可凭同一 `client_draft_id` 从该批次续传；`GET /api/items/<id>/drafts/<client_draft_id>` 返回到目前为止已落库的批次，供断点续传。

## 历史数据升级

旧数据没有批次号。服务初始化时自动把旧的扁平 `sample_results` 按条补齐为历史批次（`lab` 来源、标记 `legacy`），原恢复记录 `restoration` 保留可查。
