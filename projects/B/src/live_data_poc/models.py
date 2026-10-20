"""Closed JSON contract and its deliberately small standard-library validator.

The schema uses only the documented keyword subset implemented below. It is
not advertised as a general-purpose JSON Schema implementation.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

from . import CONTRACT


class ContractError(ValueError):
    def __init__(self, code: str, location: str = "$"):
        self.code, self.location = code, location
        super().__init__(f"{code} at {location}")


def canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(obj) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


def text(max_length=256, *, pattern=None):
    s = {"type": "string", "maxLength": max_length}
    if pattern:
        s["pattern"] = pattern
    return s


def enum(*values):
    return {"enum": list(values)}


def integer(minimum=0, maximum=10**9):
    return {"type": "integer", "minimum": minimum, "maximum": maximum}


def nullable(s):
    return {"anyOf": [s, {"type": "null"}]}


def array(s, max_items=10000):
    return {"type": "array", "items": s, "maxItems": max_items}


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


KEY = text(100, pattern=r"^syn:[A-Za-z0-9_.:-]+$")
ID = text(256, pattern=r"^[A-Za-z0-9_.:-]+$")
TIME = text(40, pattern=r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)$")
DEC = text(80, pattern=r"^(?:0|[1-9]\d*)(?:\.\d+)?$")
SCOPE = {"source_id": KEY, "account_scope_id": KEY, "app_scope_id": KEY}
FACT = {"record_id": KEY, **SCOPE, "evidence_id": KEY, "occurred_at": nullable(TIME),
        "observed_at": TIME, "time_basis": enum("platform", "local", "unknown"),
        "time_precision": enum("second", "minute", "month", "unknown"),
        "revision": integer(1, 100000), "supersedes_id": nullable(KEY)}
PERIOD = obj({"kind": enum("single_session", "calendar_day", "rolling_window", "lifetime", "unknown"),
              "start": nullable(TIME), "end": nullable(TIME), "timezone": enum("Asia/Shanghai", "UTC"),
              "label_raw": text(128)})
VALUE = obj({"status": enum("known", "unknown", "not_provided", "not_applicable", "redacted", "parse_failed"),
             "decimal": nullable(DEC), "relation": enum("eq", "gt", "gte", "lt", "lte", "range", "approx", "unknown"),
             "lower": nullable(DEC), "upper": nullable(DEC), "unit": enum("count", "second", "yinlang", "doucoin", "contribution", "CNY", "percent", "unknown"),
             "display_raw": text(128), "missing_reason": nullable(text(128)), "precision": enum("exact", "minute", "rounded", "unknown")})

SCHEMAS = {
    "sources": obj({**SCOPE, "source_type": enum("synthetic"), "namespace": KEY,
                    "adapter_version": enum("fixture/1"), "role": enum("live", "backend", "chat")}),
    "evidence": obj({"evidence_id": KEY, **SCOPE, "locator": text(120, pattern=r"^[A-Za-z0-9_/.:#-]+$"),
                     "observed_at": TIME, "kind": enum("synthetic_observation", "synthetic_direct_pair"),
                     "left_id": nullable(KEY), "right_id": nullable(KEY)}),
    "sessions": obj({**FACT, "platform_room_id": ID, "platform_session_id": nullable(ID),
                     "session_kind": enum("broadcast", "connection", "unknown"), "started_at": nullable(TIME),
                     "ended_at": nullable(TIME), "reported_duration": VALUE, "status": enum("ended", "live", "unknown")}),
    "identifiers": obj({**FACT, "namespace": KEY, "id_type": enum("user_id", "sec_uid", "open_id"),
                        "id_value": ID, "nickname": text(80), "role": enum("viewer", "chat_peer", "owner"),
                        "id_observation_status": enum("observed", "masked", "unknown")}),
    "coverage": obj({**FACT, "domain": enum("events", "comments", "gifts", "likes", "watch", "ranks", "speech", "cross_session"),
                     "session_key": nullable(KEY), "period": PERIOD, "population": text(100),
                     "status": enum("complete", "partial", "unknown"), "expected_count": nullable(integer()),
                     "observed_count": integer(), "pages_expected": nullable(integer()),
                     "pages_seen": array(integer(1), 1000), "gap_count": integer(), "limited": {"type": "boolean"}}),
    "events": obj({**FACT, "session_key": KEY, "source_subject_key": nullable(KEY),
                   "source_event_id": nullable(ID), "event_type": enum("comment", "gift", "like", "enter", "leave"),
                   "text": nullable(text(1000)), "quantity": VALUE, "semantics": enum("delta", "cumulative", "unknown"),
                   "gift_id": nullable(ID), "gift_name": nullable(text(100)), "combo_id": nullable(ID),
                   "combo_final": {"type": "boolean"}, "counter_epoch": nullable(ID),
                   "reply_to_message_id": nullable(ID), "coverage_id": KEY}),
    "watch_segments": obj({**FACT, "session_key": KEY, "source_subject_key": KEY,
                           "enter_at": nullable(TIME), "leave_at": nullable(TIME),
                           "enter_record_id": nullable(KEY), "leave_record_id": nullable(KEY),
                           "reported_watch": VALUE, "derived_watch": VALUE,
                           "derivation_version": nullable(enum("paired-interval/1")),
                           "interval_status": enum("paired", "missing_enter", "missing_leave", "unknown"),
                           "coverage_id": KEY}),
    "metrics": obj({**FACT, "grain": enum("session", "user_session", "user_period", "account_period", "group"),
                    "session_key": nullable(KEY), "source_subject_key": nullable(KEY), "population": nullable(text(100)),
                    "metric_code": enum("watch_reported", "watch_derived", "comment_count", "like_count", "gift_count", "contribution", "observed_session_count", "companion_count", "viewer_count"),
                    "period": PERIOD, "value": VALUE, "value_semantics": enum("snapshot", "derived"),
                    "derivation_version": nullable(text(100)), "input_record_ids": array(KEY, 2000),
                    "coverage_id": KEY}),
    "boards": obj({**FACT, "board_type": enum("contribution", "comments", "watch"), "session_key": nullable(KEY),
                   "period": PERIOD, "sort_metric": enum("contribution", "comment_count", "watch_reported"),
                   "filter_raw": text(128), "coverage_id": KEY}),
    "rank_rows": obj({**FACT, "snapshot_id": KEY, "source_subject_key": nullable(KEY),
                      "rank_original": integer(1), "page_index": integer(1), "row_locator": text(80), "value": VALUE}),
    "speech": obj({**FACT, "session_key": KEY, "platform_segment_id": nullable(ID),
                   "text": text(2000), "segment_end": nullable(TIME), "playback_offset_ms": nullable(integer()),
                   "speaker_role": enum("host", "unknown"), "transcript_origin": enum("synthetic_platform_text")}),
    "links": obj({**FACT, "left_id": KEY, "right_id": KEY,
                  "method": enum("direct_pair", "weak_hint"),
                  "state": enum("candidate", "direct_evidence", "unknown", "conflict", "rejected", "revoked"),
                  "reason": text(160)})
}
DATASET_SCHEMA = {"$schema": "https://json-schema.org/draft/2020-12/schema",
                  "title": "B synthetic offline dataset v0.1", **obj({
                      "contract_version": enum(CONTRACT), "data_class": enum("synthetic"),
                      "artifact_id": KEY, **{k: array(v) for k, v in SCHEMAS.items()}})}


def validate_schema(value, schema, location="$"):
    if "anyOf" in schema:
        for branch in schema["anyOf"]:
            try:
                validate_schema(value, branch, location)
                return
            except ContractError:
                pass
        raise ContractError("TYPE_OR_FORMAT", location)
    if "enum" in schema and not any(type(value) is type(v) and value == v for v in schema["enum"]):
        raise ContractError("ENUM", location)
    kind = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "null": type(None)}
    if kind and type(value) is not types[kind]:
        raise ContractError("TYPE", location)
    if kind == "object":
        if set(value) != set(schema["required"]):
            raise ContractError("FIELDS", location)
        for key, child in schema["properties"].items():
            validate_schema(value[key], child, location + "." + key)
    if kind == "array":
        if len(value) > schema["maxItems"]:
            raise ContractError("TOO_MANY_ITEMS", location)
        for i, item in enumerate(value):
            validate_schema(item, schema["items"], f"{location}[{i}]")
    if kind == "string":
        if len(value) > schema["maxLength"] or ("pattern" in schema and not re.fullmatch(schema["pattern"], value)):
            raise ContractError("STRING_FORMAT", location)
    if kind == "integer" and not schema["minimum"] <= value <= schema["maximum"]:
        raise ContractError("INTEGER_RANGE", location)


def timestamp(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.utcoffset() is None:
            raise ValueError()
        return dt
    except (ValueError, TypeError):
        raise ContractError("TIMESTAMP") from None


def validate_value(v: dict):
    validate_schema(v, VALUE)
    numeric = (v["decimal"], v["lower"], v["upper"])
    if v["status"] != "known":
        if any(n is not None for n in numeric) or v["relation"] != "unknown" or not v["missing_reason"]:
            raise ContractError("UNKNOWN_IS_NOT_ZERO")
        return
    if v["missing_reason"] is not None or v["unit"] == "unknown":
        raise ContractError("KNOWN_VALUE_NEEDS_UNIT")
    rel = v["relation"]
    valid = ((rel in {"eq", "approx"} and numeric[0] is not None and numeric[1:] == (None, None))
             or (rel in {"gt", "gte"} and numeric[0] is None and numeric[1] is not None and numeric[2] is None)
             or (rel in {"lt", "lte"} and numeric[0] is None and numeric[1] is None and numeric[2] is not None)
             or (rel == "range" and numeric[0] is None and None not in numeric[1:] and Decimal(numeric[1]) <= Decimal(numeric[2])))
    if not valid:
        raise ContractError("VALUE_RELATION")
    if rel == "approx" and v["precision"] == "exact":
        raise ContractError("APPROX_IS_NOT_EXACT")


def validate_period(p):
    validate_schema(p, PERIOD)
    if (p["start"] is None) != (p["end"] is None):
        raise ContractError("PERIOD_PAIR")
    if p["kind"] in {"calendar_day", "rolling_window"} and p["start"] is None:
        raise ContractError("PERIOD_BOUNDS_REQUIRED")
    if p["start"] is not None and timestamp(p["start"]) >= timestamp(p["end"]):
        raise ContractError("PERIOD_ORDER")


def validate_record(kind, r):
    validate_schema(r, SCHEMAS[kind])
    for key in ("occurred_at", "observed_at", "started_at", "ended_at", "enter_at", "leave_at", "segment_end"):
        if r.get(key) is not None:
            timestamp(r[key])
    if "time_basis" in r:
        if (r["time_basis"] == "unknown") != (r["occurred_at"] is None):
            raise ContractError("TIME_BASIS")
        if r["time_precision"] in {"unknown", "month"} and r["occurred_at"] is not None:
            raise ContractError("TIME_PRECISION")
        if r["revision"] == 1 and r["supersedes_id"] is not None:
            raise ContractError("REVISION_CHAIN")
        if r["revision"] > 1 and r["supersedes_id"] is None:
            raise ContractError("REVISION_CHAIN")
        if r["revision"] > 1 and kind not in {"events", "metrics", "links"}:
            raise ContractError("REVISION_KIND_NOT_SUPPORTED_IN_S1")
    for key in ("quantity", "value", "reported_watch", "derived_watch", "reported_duration"):
        if key in r:
            validate_value(r[key])
    if "period" in r:
        validate_period(r["period"])
    if kind == "sessions" and r["started_at"] and r["ended_at"] and timestamp(r["started_at"]) > timestamp(r["ended_at"]):
        raise ContractError("SESSION_TIME_ORDER")
    if kind == "metrics":
        unit = "second" if r["metric_code"] in {"watch_reported", "watch_derived"} else "contribution" if r["metric_code"] == "contribution" else "count"
        if r["value"]["unit"] != unit:
            raise ContractError("METRIC_UNIT")
        s, u, g = (r[k] is not None for k in ("session_key", "source_subject_key", "population"))
        bounded = r["period"]["start"] is not None
        valid = {"session": s and not u and not g,
                 "user_session": s and u and not g,
                 "user_period": not s and u and not g and bounded,
                 "account_period": not s and not u and not g and bounded,
                 "group": not u and g and (s or bounded)}[r["grain"]]
        if not valid:
            raise ContractError("GRAIN_TARGET")
        derived = r["value_semantics"] == "derived"
        if derived != bool(r["derivation_version"] and r["input_record_ids"]):
            raise ContractError("DERIVATION_PROVENANCE")
        if not derived and (r["derivation_version"] is not None or r["input_record_ids"]):
            raise ContractError("SNAPSHOT_HAS_DERIVATION")
    if kind == "watch_segments":
        if r["reported_watch"]["unit"] != "second" or r["derived_watch"]["unit"] != "second":
            raise ContractError("WATCH_UNIT")
        if r["interval_status"] == "paired":
            if not r["enter_at"] or not r["leave_at"] or not r["enter_record_id"] or not r["leave_record_id"]:
                raise ContractError("UNPAIRED_WATCH")
            duration = (timestamp(r["leave_at"]) - timestamp(r["enter_at"])).total_seconds()
            v = r["derived_watch"]
            if duration < 0 or v["status"] != "known" or v["relation"] != "eq" or v["unit"] != "second" or Decimal(v["decimal"]) != Decimal(str(duration)) or r["derivation_version"] != "paired-interval/1":
                raise ContractError("WATCH_DERIVATION")
        elif r["derived_watch"]["status"] == "known":
            raise ContractError("UNPAIRED_WATCH")
    if kind == "events":
        q = r["quantity"]
        if q["unit"] != "count" or (q["decimal"] is not None and Decimal(q["decimal"]) % 1):
            raise ContractError("EVENT_COUNT_UNIT")
        if (r["event_type"] == "comment") != (r["text"] is not None):
            raise ContractError("EVENT_PAYLOAD")
        if r["event_type"] != "gift" and any(r[x] is not None for x in ("gift_id", "gift_name", "combo_id")):
            raise ContractError("EVENT_PAYLOAD")
        if r["event_type"] != "gift" and r["combo_final"]:
            raise ContractError("EVENT_PAYLOAD")


def load_json_bytes(raw: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ContractError("DUPLICATE_JSON_KEY")
            result[key] = value
        return result
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ContractError("NONFINITE_JSON")))
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ContractError("JSON_PARSE") from None


def validate_dataset(data):
    validate_schema(data, DATASET_SCHEMA)
    for kind in SCHEMAS:
        for r in data[kind]:
            validate_record(kind, r)


def current_records(rows):
    """Use only after revision-chain validation; retain history in storage."""
    replaced = {r["supersedes_id"] for r in rows if r.get("supersedes_id")}
    return [r for r in rows if r.get("record_id") not in replaced]
