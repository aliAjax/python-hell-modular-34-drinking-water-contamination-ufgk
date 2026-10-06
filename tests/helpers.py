import itertools


def create_disinfected_item(service, payload=None):
    """创建事件并推进到 disinfected（可提交复检）状态。"""
    if payload is None:
        seq = next(create_disinfected_item._counter)
        payload = {
            "source_id": "SRC-%d" % seq,
            "contaminant": "nitrate",
            "detected_at": "2026-10-06T06:%02d:00+00:00" % (seq % 60),
            "concentration": 20,
            "limit": 10,
            "zone_ids": ["Z-1"],
            "population": 5000,
        }
    item = service.create_item(payload, "analyst-1", "analyst")
    item = service.act(item["id"], "verify", {"sample_count": 1}, "analyst-1", "analyst", item["version"])
    item = service.act(item["id"], "advise", {"notice_id": "N-%d" % item["id"], "kind": "boil", "message": "煮沸"}, "disp-1", "dispatcher", item["version"])
    item = service.act(item["id"], "flush", {"zone_id": "Z-1"}, "field-1", "field_operator", item["version"])
    item = service.act(item["id"], "disinfect", {"zone_id": "Z-1", "completed": True}, "field-1", "field_operator", item["version"])
    return item


def sample(service, item, batch_id, sample_id, concentration, role, source=None, client_actor=None):
    body = {"batch_id": batch_id, "sample_id": sample_id, "zone_id": "Z-1", "concentration": concentration}
    if source:
        body["source"] = source
    return service.act(item["id"], "sample", body, client_actor or ("%s-1" % role), role, item["version"])

create_disinfected_item._counter = itertools.count(1)
