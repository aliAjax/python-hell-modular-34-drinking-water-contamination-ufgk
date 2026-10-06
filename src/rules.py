from datetime import datetime, timezone

from .domain import DomainError


def now_iso():
    return datetime.now(timezone.utc).isoformat()


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
    "judge": {"coordinator", "regulator"},
    "restore": {"coordinator", "regulator"},
    "cancel": {"coordinator"},
}
ENFORCE_REGION = False
REGION_SENSITIVE_ACTIONS = set()
ACTION_REQUIRES_VERSION = {"advise", "switch_source", "flush", "disinfect", "sample", "judge", "restore", "cancel"}


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


def batch_effective(batch):
    """批次的有效浓度：已判定取判定来源；单一来源取该值；多来源待判定取最保守（最大）值。"""
    results = batch.get("results", {})
    if not results:
        return None
    judgment = batch.get("judgment") or {}
    adopt = judgment.get("adopt_source")
    if adopt and adopt in results:
        return float(results[adopt]["concentration"])
    return max(float(result["concentration"]) for result in results.values())


def _effective_results(current):
    """返回 {批次号: 有效浓度}，优先使用归集后的 sampling_batches，兼容旧扁平 sample_results。"""
    batches = current.get("sampling_batches")
    if batches:
        effective = {}
        for batch_id, batch in batches.items():
            value = batch_effective(batch)
            if value is not None:
                effective[batch_id] = value
        return effective
    legacy = current.get("sample_results", [])
    return {("legacy-%d" % index): float(result.get("concentration", 0)) for index, result in enumerate(legacy)}


def recompute_conclusion(current):
    """根据当前有效结果重算恢复结论，写入 recovery_conclusion，返回 (是否可恢复, 原因列表)。"""
    limit = max(float(current.get("limit", 0)), 0.000001)
    effective = _effective_results(current)
    reasons = []
    for batch_id, concentration in effective.items():
        if concentration > limit:
            reasons.append("批次 %s 有效浓度 %s 超过限值 %s" % (batch_id, concentration, limit))
    eligible = bool(effective) and not reasons
    current["recovery_conclusion"] = {
        "eligible": eligible,
        "effective": effective,
        "reasons": reasons,
        "computed_at": now_iso(),
    }
    return eligible, reasons


def _revoke_restoration(current, actor, reasons):
    """晚到结果推翻结论：把已完成恢复退回待复检，原恢复记录写入 history 并写明原因。"""
    restoration = current.get("restoration") or {"actor": actor, "note": ""}
    record = dict(restoration)
    record["revoked_at"] = now_iso()
    record["revoked_by"] = actor
    record["revoke_reason"] = "；".join(reasons) if reasons else "有效复检结果变化，恢复结论不再成立"
    current.setdefault("restoration_history", []).append(record)
    current["restoration"] = None
    return {
        "revoked_at": record["revoked_at"],
        "revoke_reason": record["revoke_reason"],
        "original_actor": restoration.get("actor"),
    }


def _source_type_for(role, payload):
    source_type = payload.get("source_type") or ("lab" if role == "lab" else "field")
    if source_type not in ("lab", "field"):
        raise DomainError("invalid_source_type", "来源类型必须是 lab 或 field")
    return source_type


def _apply_sample(status, current, payload, actor, role):
    if status not in {"disinfected", "sampled", "restored"}:
        raise DomainError("invalid_state", "当前状态 %s 不允许执行该操作" % status)
    batch_id = _text(payload, "batch_id")
    sample_id = _text(payload, "sample_id")
    zone_id = _text(payload, "zone_id")
    concentration = float(payload.get("concentration", 0))
    if concentration < 0:
        raise DomainError("invalid_concentration", "浓度不能为负数")
    source_type = _source_type_for(role, payload)

    batches = current.setdefault("sampling_batches", {})
    batch = batches.get(batch_id)
    replaced = False
    if batch is None:
        batch = {"batch_id": batch_id, "zone_id": zone_id, "results": {}, "status": "consistent", "judgment": None}
        batches[batch_id] = batch
    else:
        if batch.get("zone_id") != zone_id:
            raise DomainError(
                "zone_mismatch",
                "批次 %s 已登记在区域 %s，不能登记到 %s" % (batch_id, batch.get("zone_id"), zone_id),
            )
        if source_type in batch["results"]:
            replaced = True
    batch["results"][source_type] = {
        "sample_id": sample_id,
        "zone_id": zone_id,
        "concentration": concentration,
        "observed_at": payload.get("observed_at"),
        "recorded_at": now_iso(),
    }
    # 同批次同来源后到顶掉先到；不同来源并存标成待判定
    batch["status"] = "pending_judgment" if len(batch["results"]) >= 2 else "consistent"

    eligible, reasons = recompute_conclusion(current)
    new_status = status
    recovery_revoked = None
    if status == "disinfected":
        new_status = "sampled"
    elif status == "restored":
        if eligible:
            new_status = "restored"
        else:
            new_status = "sampled"
            recovery_revoked = _revoke_restoration(current, actor, reasons)

    event = {
        "batch_id": batch_id,
        "source_type": source_type,
        "sample_id": sample_id,
        "zone_id": zone_id,
        "concentration": concentration,
        "replaced": replaced,
        "batch_status": batch["status"],
        "recovery_eligible": eligible,
    }
    if recovery_revoked:
        event["recovery_revoked"] = recovery_revoked
    return new_status, current, event


def _apply_judge(status, current, payload, actor, role):
    if status not in {"sampled", "restored"}:
        raise DomainError("invalid_state", "当前状态 %s 不允许执行该操作" % status)
    batch_id = _text(payload, "batch_id")
    adopt_source = _text(payload, "adopt_source")
    if adopt_source not in ("lab", "field"):
        raise DomainError("invalid_source_type", "来源类型必须是 lab 或 field")
    batches = current.setdefault("sampling_batches", {})
    batch = batches.get(batch_id)
    if batch is None:
        raise DomainError("batch_not_found", "采样批次 %s 不存在" % batch_id)
    if adopt_source not in batch.get("results", {}):
        raise DomainError("source_not_found", "批次 %s 没有 %s 来源的结果" % (batch_id, adopt_source))
    batch["judgment"] = {"adopt_source": adopt_source, "by": actor, "at": now_iso(), "note": payload.get("note", "")}
    batch["status"] = "consistent"

    eligible, reasons = recompute_conclusion(current)
    new_status = status
    recovery_revoked = None
    if status == "restored" and not eligible:
        new_status = "sampled"
        recovery_revoked = _revoke_restoration(current, actor, reasons)

    event = {"batch_id": batch_id, "adopt_source": adopt_source, "recovery_eligible": eligible}
    if recovery_revoked:
        event["recovery_revoked"] = recovery_revoked
    return new_status, current, event


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
        return _apply_sample(status, current, payload, actor, role)

    if action == "judge":
        return _apply_judge(status, current, payload, actor, role)

    if action == "restore":
        _need_status(item, {"sampled"})
        if not payload.get("all_zones_cleared"):
            raise DomainError("zones_not_cleared", "仍有区域未完成水质恢复", 409)
        eligible, reasons = recompute_conclusion(current)
        if not eligible:
            raise DomainError("quality_not_met", "复检结果未全部达到限值：" + "；".join(reasons), 409)
        current["restoration"] = {"actor": actor, "note": payload.get("note", ""), "at": now_iso()}
        return "restored", current, {"restoration": current["restoration"], "conclusion": current["recovery_conclusion"]}

    if action == "cancel":
        _need_status(item, {"detected", "verified"})
        reason = _text(payload, "reason")
        current["cancellation"] = {"reason": reason, "actor": actor}
        return "cancelled", current, {"reason": reason}

    raise DomainError("unknown_action", "不支持的操作")
