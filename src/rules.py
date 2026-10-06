from datetime import datetime, timezone

from . import batches
from .domain import DomainError

ENTITY_TYPE = "water_contamination"
INITIAL_STATUS = "detected"
CREATE_ROLES = {"analyst", "dispatcher"}
SOURCE_ROLES = {"analyst", "dispatcher", "field_operator", "lab"}
ACTION_ROLES = {
    "verify": {"analyst", "dispatcher"},
    "advise": {"coordinator", "dispatcher"},
    "switch_source": {"coordinator"},
    "flush": {"field_operator"},
    "disinfect": {"field_operator"},
    "sample": {"lab", "field_operator"},
    "adjudicate_batch": {"analyst", "coordinator", "regulator"},
    "restore": {"coordinator", "regulator"},
    "cancel": {"coordinator"},
}
ENFORCE_REGION = False
REGION_SENSITIVE_ACTIONS = set()
ACTION_REQUIRES_VERSION = {"advise", "switch_source", "flush", "disinfect", "sample", "adjudicate_batch", "restore", "cancel"}

REINSPECTION_STATUS = "reinspection_pending"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def assess(payload):
    concentration = float(payload.get("concentration", 0))
    limit = max(float(payload.get("limit", 0.000001)), 0.000001)
    ratio = concentration / limit
    population = int(payload.get("population", 0))
    score = min(100.0, ratio * 35.0 + min(population / 1000.0, 40.0))
    if score >= 80:
        level = "critical"
    elif score >= 50:
        level = "high"
    elif score >= 20:
        level = "medium"
    else:
        level = "low"
    return {"score": round(score, 2), "level": level, "ratio": round(ratio, 3)}


def _need_status(item, allowed):
    if item["status"] not in allowed:
        raise DomainError("invalid_state", "当前状态 %s 不允许执行该操作" % item["status"])


def _text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def _effective_snapshot(payload):
    """恢复时记录的有效结果快照。"""
    return [
        {
            "batch_id": r.get("batch_id"),
            "source": r.get("source"),
            "sample_id": r.get("sample_id"),
            "zone_id": r.get("zone_id"),
            "concentration": r.get("concentration"),
        }
        for r in batches.effective_results(payload)
    ]


def _apply_sample(current, sample, actor, role):
    """把复检结果并入批次；若推翻已完成恢复，退回待复检并写明原因。"""
    source = batches.ROLE_SOURCE.get(role)
    if source is not None and sample.get("source") and sample["source"] != source:
        raise DomainError("source_role_mismatch", "当前角色只能提交来源为 %s 的复检结果" % source)
    sample["source"] = sample.get("source") or source
    if sample["source"] not in batches.SOURCES:
        raise DomainError("invalid_source", "复检来源必须为 lab 或 field")

    sample = dict(sample)
    sample["submitted_by"] = actor
    submitted_at = sample.pop("submitted_at", None) or now_iso()

    batch, result, superseded = batches.merge_sample(current, sample)

    event = {
        "sample_result": {k: result[k] for k in ("batch_id", "sample_id", "zone_id", "concentration", "source")},
        "batch_id": batch["batch_id"],
        "batch_status": batch["status"],
        "superseded": superseded,
    }

    # 向后兼容：旧代码与旧规则仍读 sample_results（保留全部结果）
    current.setdefault("sample_results", []).append({
        "sample_id": result["sample_id"],
        "zone_id": result["zone_id"],
        "concentration": result["concentration"],
        "batch_id": batch["batch_id"],
        "source": result["source"],
    })

    invalidation = None
    if current.get("restoration"):
        blockers = batches.restoration_blockers(current)
        if blockers:
            reason = batches.describe_invalidation(blockers, current.get("limit", 0))
            revoked = batches.revoke_restoration(current, submitted_at, reason, [{
                "batch_id": result["batch_id"],
                "source": result["source"],
                "sample_id": result["sample_id"],
            }])
            invalidation = {
                "reason": reason,
                "restoration": {k: revoked[k] for k in ("actor", "note")} if revoked else None,
            }
            event["restoration_invalidated"] = invalidation

    return result, batch, event, invalidation, submitted_at


def apply_action(item, action, payload, actor, role):
    status = item["status"]
    current = dict(item["payload"])

    if action == "verify":
        _need_status(item, {"detected", "verified"})
        sample_count = int(payload.get("sample_count", 0) or 0)
        if sample_count < 1:
            raise DomainError("sample_required", "需要至少一份复检样本", 409)
        current["assessment"] = assess(current)
        current["verification"] = {"sample_count": sample_count, "note": payload.get("note", "")}
        return "verified", current, {"assessment": current["assessment"], "verification": current["verification"]}

    if action == "advise":
        _need_status(item, {"verified", "advisory"})
        notice_id = _text(payload, "notice_id")
        notice = {
            "notice_id": notice_id,
            "kind": _text(payload, "kind"),
            "message": _text(payload, "message"),
        }
        notices = current.setdefault("notifications", [])
        if any(existing.get("notice_id") == notice_id for existing in notices):
            raise DomainError("duplicate_notification", "同一通知编号不能重复发送", 409)
        notices.append(notice)
        return "advisory", current, {"notice": notice}

    if action == "switch_source":
        _need_status(item, {"verified", "advisory", "flushing", "disinfected", "sampled", "switched"})
        alternate = _text(payload, "alternate_source_id")
        current["alternate_source_id"] = alternate
        return "switched", current, {"alternate_source_id": alternate}

    if action == "flush":
        _need_status(item, {"advisory", "flushing", "switched"})
        zone_id = _text(payload, "zone_id")
        current.setdefault("response_actions", []).append({"type": "flush", "zone_id": zone_id})
        return "flushing", current, {"zone_id": zone_id, "type": "flush"}

    if action == "disinfect":
        _need_status(item, {"flushing", "disinfected"})
        if not payload.get("completed"):
            raise DomainError("disinfection_incomplete", "消毒尚未完成", 409)
        zone_id = _text(payload, "zone_id")
        current.setdefault("response_actions", []).append({"type": "disinfect", "zone_id": zone_id})
        return "disinfected", current, {"zone_id": zone_id, "type": "disinfect"}

    if action == "sample":
        _need_status(item, {"disinfected", "sampled", "restored", REINSPECTION_STATUS})
        result, batch, event, invalidation, submitted_at = _apply_sample(current, payload, actor, role)
        result["submitted_at"] = submitted_at
        if invalidation:
            new_status = REINSPECTION_STATUS
        elif status == "restored":
            # 恢复后到达的结果仍然合格：结论维持有效
            new_status = "restored"
        else:
            new_status = "sampled"
        return new_status, current, event

    if action == "adjudicate_batch":
        _need_status(item, {"sampled", REINSPECTION_STATUS})
        batch_id = _text(payload, "batch_id")
        chosen_source = _text(payload, "chosen_source")
        batch = batches.get_batch(current, batch_id)
        if batch is None:
            raise DomainError("batch_not_found", "复检批次不存在", 404)
        if batch["status"] != batches.BATCH_PENDING:
            raise DomainError("batch_not_pending", "该批次不在待判定状态")
        at = now_iso()
        rejected = batches.adjudicate(
            batch, chosen_source, actor, payload.get("note", ""), at, float(current.get("limit", 0)) or 0.0
        )
        event = {"batch_id": batch_id, "chosen_source": chosen_source, "rejected_samples": rejected}

        blockers = batches.restoration_blockers(current)
        invalidation = None
        if current.get("restoration") and blockers:
            reason = batches.describe_invalidation(blockers, current.get("limit", 0))
            revoked = batches.revoke_restoration(current, at, reason, [
                {"batch_id": batch_id, "adjudication": chosen_source}
            ])
            invalidation = {"reason": reason, "restoration": {k: revoked[k] for k in ("actor", "note")} if revoked else None}
            event["restoration_invalidated"] = invalidation

        # 恢复已作废或本就待复检：只要仍有阻断项（超标/待判定）就继续待复检
        if not current.get("restoration") and blockers:
            new_status = REINSPECTION_STATUS
        elif invalidation:
            new_status = REINSPECTION_STATUS
        else:
            new_status = "sampled"
        return new_status, current, event

    if action == "restore":
        _need_status(item, {"sampled", REINSPECTION_STATUS})
        if not payload.get("all_zones_cleared"):
            raise DomainError("zones_not_cleared", "仍有区域未完成水质恢复", 409)
        blockers = batches.restoration_blockers(current)
        for blocker in blockers:
            if blocker["code"] == "batch_pending_judgement":
                raise DomainError("batch_pending_judgement", blocker["message"], 409)
            if blocker["code"] == "no_effective_result":
                raise DomainError("quality_not_met", "尚无有效复检结果可支撑恢复", 409)
            if blocker["code"] == "quality_not_met":
                raise DomainError("quality_not_met", blocker["message"], 409)
        record = {
            "actor": actor,
            "note": payload.get("note", ""),
            "at": now_iso(),
            "effective_results": _effective_snapshot(current),
        }
        current["restoration"] = record
        history = current.setdefault("restoration_history", [])
        history.append({k: v for k, v in record.items()} | {"revoked": None})
        return "restored", current, {"restoration": record}

    if action == "cancel":
        _need_status(item, {"detected", "verified"})
        reason = _text(payload, "reason")
        current["cancellation"] = {"reason": reason, "actor": actor}
        return "cancelled", current, {"reason": reason}

    raise DomainError("unknown_action", "不支持的操作")
