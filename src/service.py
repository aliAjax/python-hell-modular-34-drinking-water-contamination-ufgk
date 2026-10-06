from . import domain, rules, batches
from .domain import DomainError, ConflictError


class Service:
    def __init__(self, repository):
        self.repository = repository

    def create_item(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        return self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role
        )

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)
        result = self.repository.add_source(
            item_id,
            normalized.pop("source_type"),
            normalized.pop("external_id"),
            normalized,
            normalized.pop("observed_at"),
            actor,
            role,
        )
        return result

    def _submit_sample(self, item_id, sample, actor, role, expected_version=None, region=None):
        """提交一份复检结果。

        断网回网按批次合并时，多份草稿基于同一版本，乐观版本可能冲突：
        复检结果是可追加、由领域层按批次归集的幂等语义，因此在存储层行锁内
        自动重读最新版本重试，避免草稿合并因先后提交而互相覆盖。
        """
        item = self.repository.get_item(item_id)
        if rules.ENFORCE_REGION and region and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)

        version = expected_version
        last_conflict = None
        for attempt in range(20):
            try:
                new_status, new_payload, event_payload = rules.apply_action(
                    item, "sample", sample, actor, role
                )
            except DomainError:
                raise
            try:
                return self.repository.apply_action(
                    item_id, "sample", actor, role, new_status, new_payload, event_payload, version
                )
            except ConflictError as exc:
                if exc.code != "version_conflict":
                    raise
                last_conflict = exc
                item = self.repository.get_item(item_id)
                version = item["version"]
        raise last_conflict

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        item = self.repository.get_item(item_id)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and region and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)

        if action == "sample":
            sample = domain.normalize_sample(payload)
            self._submit_sample(item_id, sample, actor, role, expected_version, region)
            return self.get_item(item_id)

        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

    # ---- 外勤断网：草稿暂存、按批次合并、失败后续传 ----

    def save_sample_draft(self, item_id, payload, actor, role):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.ACTION_ROLES["sample"]:
            raise DomainError("forbidden", "当前角色不能保存复检草稿", 403)
        sample = domain.normalize_sample(payload)
        client_id = domain.require_text(payload, "client_id")
        draft, created = self.repository.save_draft(
            item_id, client_id, sample["batch_id"], actor, role, sample
        )
        return {"draft": draft, "created": created}

    def sync_drafts(self, item_id=None):
        """回网后按采样批次合并草稿并提交，失败后可从该批次续传。

        处理顺序：待提交/失败的草稿按 (item_id, batch_id) 分组，组内按 id
        顺序（即外勤本地产生顺序）逐份合并；任一份失败立即中止，该批次剩余
        草稿与后续批次保留原状，下次调用从该批次续传。
        """
        drafts = self.repository.list_drafts(item_id=item_id)
        pending = [d for d in drafts if d["status"] != "synced"]

        grouped = []
        index = {}
        for draft in pending:
            key = (draft["item_id"], draft["batch_id"])
            if key not in index:
                index[key] = len(grouped)
                grouped.append((key, []))
            grouped[index[key]][1].append(draft)

        synced = []
        failed = None
        for (draft_item_id, batch_id), group in grouped:
            for draft in group:
                if draft["status"] == "synced":
                    continue
                try:
                    sample = domain.normalize_sample(draft["payload"])
                    self._submit_sample(draft_item_id, sample, draft["actor"], draft["role"])
                except DomainError as exc:
                    self.repository.mark_draft(draft["id"], "failed", "%s:%s" % (exc.code, str(exc)))
                    failed = {
                        "item_id": draft_item_id,
                        "batch_id": batch_id,
                        "client_id": draft["client_id"],
                        "error_code": exc.code,
                        "message": str(exc),
                    }
                    return self._sync_report(item_id, synced, failed)
                else:
                    self.repository.mark_draft(draft["id"], "synced")
                    synced.append({
                        "item_id": draft_item_id,
                        "batch_id": batch_id,
                        "client_id": draft["client_id"],
                    })
        return self._sync_report(item_id, synced, None)

    def _sync_report(self, item_id, synced, failed):
        remaining = self.repository.list_drafts(item_id=item_id, status="pending")
        remaining += self.repository.list_drafts(item_id=item_id, status="failed")
        resume_key = (failed["item_id"], failed["batch_id"]) if failed else None
        return {
            "synced": synced,
            "failed": failed,
            "resume_from": {"item_id": resume_key[0], "batch_id": resume_key[1]} if failed else None,
            "remaining": [
                {"item_id": d["item_id"], "batch_id": d["batch_id"], "client_id": d["client_id"], "status": d["status"]}
                for d in remaining
            ],
            "complete": failed is None,
        }

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        item["batch_summary"] = batches.batch_summary(item["payload"])
        item["effective_results"] = batches.effective_results(item["payload"])
        item["drafts"] = self.repository.list_drafts(item_id=item_id)
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()
