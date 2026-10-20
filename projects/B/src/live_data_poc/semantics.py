"""Small, explicit S1 algorithms; unsupported derivations fail closed."""
from decimal import Decimal

from .models import ContractError, canonical, current_records, timestamp


def exact(value, unit):
    if value["status"] != "known" or value["relation"] != "eq" or value["unit"] != unit or value["precision"] != "exact":
        raise ContractError("EXACT_DERIVATION_VALUE_REQUIRED")
    return Decimal(value["decimal"])


def validate_semantics(data, maps):
    identity_fields = {
        "events": ("source_id", "account_scope_id", "app_scope_id", "session_key", "event_type", "source_event_id"),
        "links": ("source_id", "account_scope_id", "app_scope_id", "left_id", "right_id"),
        "metrics": ("source_id", "account_scope_id", "app_scope_id", "grain", "session_key", "source_subject_key", "population", "metric_code", "period")}
    for kind, fields in identity_fields.items():
        successors = set()
        for r in data[kind]:
            if r["revision"] == 1:
                continue
            prior = maps[kind].get(r["supersedes_id"])
            if not prior or r["revision"] != prior["revision"] + 1 or any(r[f] != prior[f] for f in fields):
                raise ContractError("REVISION_PREDECESSOR")
            if prior["record_id"] in successors:
                raise ContractError("REVISION_FORK")
            successors.add(prior["record_id"])
            if kind == "events" and r["source_event_id"] is None:
                raise ContractError("NO_ID_REVISION_NOT_SUPPORTED")
    stable = set()
    for r in data["events"]:
        if r["source_event_id"] is not None:
            key = canonical([r[f] for f in identity_fields["events"]] + [r["revision"]])
            if key in stable:
                raise ContractError("DUPLICATE_LOGICAL_EVENT")
            stable.add(key)

    events = {r["record_id"]: r for r in current_records(data["events"])}
    for r in data["watch_segments"]:
        for field, event_type, at in (("enter_record_id", "enter", "enter_at"), ("leave_record_id", "leave", "leave_at")):
            if r[field] is None:
                continue
            e = events.get(r[field])
            if not e or e["event_type"] != event_type or any(e[f] != r[f] for f in ("source_id", "account_scope_id", "app_scope_id", "session_key", "source_subject_key")):
                raise ContractError("WATCH_EVENT_REFERENCE")
            if e["occurred_at"] is None or r[at] is None or timestamp(e["occurred_at"]) != timestamp(r[at]):
                raise ContractError("WATCH_EVENT_TIME")

    for r in current_records(data["metrics"]):
        if r["value_semantics"] != "derived":
            continue
        ids = r["input_record_ids"]
        if len(ids) != len(set(ids)):
            raise ContractError("DUPLICATE_DERIVATION_INPUT")
        algorithm = r["derivation_version"]
        if algorithm == "paired-interval/1":
            if r["metric_code"] != "watch_derived" or r["grain"] != "user_session" or len(ids) != 1:
                raise ContractError("DERIVATION_ALGORITHM_TARGET")
            w = maps["watch_segments"].get(ids[0])
            if not w or w["interval_status"] != "paired" or any(w[f] != r[f] for f in ("source_id", "source_subject_key", "session_key")):
                raise ContractError("DERIVATION_WATCH_INPUT")
            expected = exact(w["derived_watch"], "second")
            actual = exact(r["value"], "second")
        elif algorithm == "distinct-observed-sessions/1":
            if r["metric_code"] != "observed_session_count" or r["grain"] != "user_period":
                raise ContractError("DERIVATION_ALGORITHM_TARGET")
            selected = [events.get(i) for i in ids]
            if any(e is None or e["source_id"] != r["source_id"] or e["source_subject_key"] != r["source_subject_key"] or e["occurred_at"] is None for e in selected):
                raise ContractError("DERIVATION_EVENT_INPUT")
            start, end = timestamp(r["period"]["start"]), timestamp(r["period"]["end"])
            if any(not start <= timestamp(e["occurred_at"]) < end for e in selected):
                raise ContractError("DERIVATION_PERIOD")
            expected = Decimal(len({e["session_key"] for e in selected}))
            actual = exact(r["value"], "count")
        else:
            raise ContractError("DERIVATION_ALGORITHM_UNSUPPORTED")
        if actual != expected:
            raise ContractError("DERIVATION_RESULT")


def coverage_domain(kind, row):
    if kind == "events":
        return {"events", {"comment": "comments", "gift": "gifts", "like": "likes", "enter": "events", "leave": "events"}[row["event_type"]]}
    if kind == "watch_segments":
        return {"watch"}
    if kind == "boards":
        return {"ranks"}
    if kind == "metrics":
        return {"watch"} if row["metric_code"].startswith("watch_") else {"cross_session"} if row["metric_code"] in {"observed_session_count", "companion_count"} else {"events", "comments", "gifts", "likes"}
    return set()
