"""复检批次归集与恢复结论重算。

复检结果按采样批次（batch_id）归集：

- 同批次同来源：后到结果顶掉先到结果（先到结果保留并标记 superseded，审计可查）；
- 同批次不同来源：结果并存；结论一致时批次有效，结论冲突时批次标记为待判定，
  需人工裁定（adjudicate）后才恢复有效；
- 有效结果发生变化时，已完成的恢复结论必须重算：一旦有效结果超标或出现
  待判定批次，恢复结论被推翻，事件退回待复检并写明原因。
"""

from .domain import DomainError

SOURCE_LAB = "lab"
SOURCE_FIELD = "field"
SOURCES = (SOURCE_LAB, SOURCE_FIELD)

BATCH_EFFECTIVE = "effective"
BATCH_PENDING = "pending_judgement"

ROLE_SOURCE = {"lab": SOURCE_LAB, "field_operator": SOURCE_FIELD}


def active_results(batch):
    """批次中当前仍有效的结果（未被同来源新结果顶替、未被裁定驳回）。"""
    return [r for r in batch.get("results", []) if not r.get("superseded") and not r.get("rejected")]


def _verdict(results, limit):
    if not results:
        return None
    if all(float(r["concentration"]) <= limit for r in results):
        return "pass"
    if all(float(r["concentration"]) > limit for r in results):
        return "fail"
    return "mixed"


def get_batch(payload, batch_id):
    for batch in payload.get("sample_batches", []):
        if batch["batch_id"] == batch_id:
            return batch
    return None


def _recompute_status(batch, limit):
    active = active_results(batch)
    sources = {r["source"] for r in active}
    adjudication = batch.get("adjudication")

    if adjudication:
        # 已裁定批次：只有被选中来源仍有有效结果时才有效；
        # 其它来源又提交了新结果，需重新进入待判定。
        chosen = adjudication["chosen_source"]
        other_active = [r for r in active if r["source"] != chosen]
        if any(r["source"] == chosen for r in active) and not other_active:
            batch["status"] = BATCH_EFFECTIVE
        else:
            batch["status"] = BATCH_PENDING
        return batch["status"]

    verdict = _verdict(active, limit)
    if len(sources) >= 2 and verdict == "mixed":
        batch["status"] = BATCH_PENDING
    else:
        batch["status"] = BATCH_EFFECTIVE
    return batch["status"]


def merge_sample(payload, sample):
    """把一份复检结果并入对应批次，返回 (批次, 新结果, 被顶替结果列表)。"""
    batches = payload.setdefault("sample_batches", [])
    batch = get_batch(payload, sample["batch_id"])
    if batch is None:
        batch = {"batch_id": sample["batch_id"], "status": BATCH_EFFECTIVE, "results": []}
        batches.append(batch)

    seq = max((int(r.get("seq", 0)) for r in batch["results"]), default=0) + 1
    result = dict(sample)
    result["seq"] = seq
    result.setdefault("superseded", False)
    result.setdefault("rejected", False)

    # 同批次同来源：后到顶掉先到
    superseded = []
    for prior in batch["results"]:
        if (
            prior.get("source") == result["source"]
            and not prior.get("superseded")
            and not prior.get("rejected")
        ):
            prior["superseded"] = True
            prior["superseded_by_seq"] = seq
            superseded.append(prior["sample_id"])

    batch["results"].append(result)
    _recompute_status(batch, float(payload.get("limit", 0)) or 0.0)
    return batch, result, superseded


def adjudicate(batch, chosen_source, actor, reason, at, limit):
    """裁定待判定批次，驳回未选中来源的有效结果，返回被驳回 sample_id 列表。"""
    active = active_results(batch)
    if chosen_source not in {r["source"] for r in active}:
        raise DomainError("source_not_in_batch", "所选来源在该批次中没有有效结果")
    rejected = []
    for result in active:
        if result["source"] != chosen_source:
            result["rejected"] = True
            rejected.append(result["sample_id"])
    batch["adjudication"] = {
        "chosen_source": chosen_source,
        "actor": actor,
        "reason": reason or "",
        "at": at,
    }
    _recompute_status(batch, limit)
    return rejected


def effective_results(payload):
    """所有有效批次中的有效结果；兼容旧版只有 sample_results 的数据形态。"""
    batches = payload.get("sample_batches")
    if batches:
        results = []
        for batch in batches:
            if batch["status"] == BATCH_EFFECTIVE:
                results.extend(active_results(batch))
        return results
    return [
        {"batch_id": None, "source": "legacy", **result}
        for result in payload.get("sample_results", [])
    ]


def pending_batches(payload):
    return [b for b in payload.get("sample_batches", []) if b["status"] == BATCH_PENDING]


def restoration_blockers(payload):
    """恢复（或恢复结论维持）的阻断项，空列表表示当前有效结果支持恢复。"""
    limit = float(payload.get("limit", 0)) or 0.0
    batches = payload.get("sample_batches")

    if not batches:
        # 旧数据形态（无批次号）：直接按全部复检结果判定
        results = payload.get("sample_results", [])
        if not results:
            return [{"code": "no_effective_result", "message": "尚无可判定的复检结果"}]
        over = [r for r in results if float(r["concentration"]) > limit]
        if over:
            return [{"code": "quality_not_met", "message": "复检结果未全部达到限值", "results": over}]
        return []

    blockers = []
    for batch in pending_batches(payload):
        blockers.append({
            "code": "batch_pending_judgement",
            "batch_id": batch["batch_id"],
            "message": "批次 %s 不同来源复检结果不一致，待判定" % batch["batch_id"],
        })

    effective = effective_results(payload)
    if not effective:
        blockers.append({"code": "no_effective_result", "message": "暂无有效复检结果，批次均待判定"})
    else:
        over = [r for r in effective if float(r["concentration"]) > limit]
        if over:
            blockers.append({
                "code": "quality_not_met",
                "message": "复检结果未全部达到限值",
                "results": [
                    {
                        "batch_id": r["batch_id"],
                        "source": r["source"],
                        "sample_id": r["sample_id"],
                        "concentration": r["concentration"],
                    }
                    for r in over
                ],
            })
    return blockers


def describe_invalidation(blockers, limit):
    """把阻断项写成恢复退回原因。"""
    parts = []
    for blocker in blockers:
        if blocker["code"] == "quality_not_met":
            for result in blocker.get("results", []):
                parts.append(
                    "批次 %s 来源 %s 的复检结果 %s 超过限值 %s"
                    % (result["batch_id"], result["source"], result["concentration"], limit)
                )
        else:
            parts.append(blocker["message"])
    return "晚到复检结果推翻恢复结论：" + "；".join(parts)


def revoke_restoration(payload, at, reason, by_samples):
    """作废当前恢复结论，在恢复历史中写明退回原因，返回被作废的历史条目。"""
    history = payload.setdefault("restoration_history", [])
    current = None
    for entry in reversed(history):
        if not entry.get("revoked"):
            current = entry
            break
    if current is None:
        restoration = payload.get("restoration")
        if restoration:
            current = dict(restoration, revoked=None)
            history.append(current)
    if current is not None:
        current["revoked"] = {"at": at, "reason": reason, "by_samples": by_samples}
    payload["restoration"] = None
    return current


def migrate_legacy_item(item_id, payload):
    """升级迁移：为缺少批次号的旧数据补齐历史批次；原恢复记录保留可查。

    返回 (是否变更, 补齐的批次id或None)。
    """
    changed = False
    legacy_batch_id = None

    if not payload.get("sample_batches") and payload.get("sample_results"):
        legacy_batch_id = "LEGACY-%d" % item_id
        results = []
        for index, old in enumerate(payload.get("sample_results", []), start=1):
            results.append({
                "seq": index,
                "batch_id": legacy_batch_id,
                "sample_id": old.get("sample_id", "LEGACY-S-%d" % index),
                "source": "legacy",
                "zone_id": old.get("zone_id", ""),
                "concentration": float(old.get("concentration", 0)),
                "submitted_by": "legacy-migration",
                "submitted_at": None,
                "superseded": False,
                "rejected": False,
                "legacy": True,
            })
        payload["sample_batches"] = [{
            "batch_id": legacy_batch_id,
            "status": BATCH_EFFECTIVE,
            "results": results,
            "legacy": True,
        }]
        changed = True

    restoration = payload.get("restoration")
    if restoration and not payload.get("restoration_history"):
        snapshot = []
        if changed:
            snapshot = [
                {
                    "batch_id": legacy_batch_id,
                    "source": r["source"],
                    "sample_id": r["sample_id"],
                    "zone_id": r.get("zone_id"),
                    "concentration": r["concentration"],
                }
                for r in active_results(payload["sample_batches"][0])
            ]
        payload["restoration_history"] = [{
            "actor": restoration.get("actor"),
            "note": restoration.get("note", ""),
            "at": restoration.get("at"),
            "effective_results": restoration.get("effective_results", snapshot),
            "legacy": True,
            "revoked": None,
        }]
        changed = True

    return changed, legacy_batch_id


def batch_summary(payload):
    """供接口展示的批次归集视图。"""
    limit = float(payload.get("limit", 0)) or 0.0
    summary = []
    for batch in payload.get("sample_batches", []):
        active = active_results(batch)
        summary.append({
            "batch_id": batch["batch_id"],
            "status": batch["status"],
            "sources": sorted({r["source"] for r in active}),
            "verdict": _verdict(active, limit) if batch["status"] == BATCH_EFFECTIVE else None,
            "total_results": len(batch.get("results", [])),
            "adjudication": batch.get("adjudication"),
            "legacy": bool(batch.get("legacy")),
        })
    return summary
