"""Closed candidate-observation contract; all real fields remain in ciphertext."""
from __future__ import annotations
from datetime import datetime
import json
import math
import re
from . import CONTRACT
from .security import CollectorError

DOMAINS = {"account", "session_list", "session", "summary", "rank", "profile", "comment", "gift", "speech", "trend", "chat", "legacy_chat", "capability"}
SOURCE_ORIGINS = {"https://anchor.douyin.com", "https://www.douyin.com", "local:user-selected-jsonl"}
MAX_MESSAGE = 1024 * 1024
MAX_OBSERVATIONS = 100
FORBIDDEN = re.compile(r"cookie|authorization|password|access.?token|refresh.?token|csrf|signature|a_bogus|msToken|headers|credentials", re.I)
KEY = re.compile(r"^[A-Za-z0-9_./\[\]-]{1,180}$")
UUID = re.compile(r"^[0-9a-f]{32}$")


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def decode(data):
    def pairs(items):
        out = {}
        for k, v in items:
            if k in out:
                raise CollectorError("DUPLICATE_JSON_KEY")
            out[k] = v
        return out
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(CollectorError("JSON_NUMBER")))
    except (UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, CollectorError):
            raise
        raise CollectorError("INVALID_JSON") from None


def keys(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise CollectorError("CONTRACT_KEYS")


def stamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise CollectorError("TIME_FORMAT")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError()
    except ValueError:
        raise CollectorError("TIME_FORMAT") from None


def scalar(value):
    keys(value, {"type", "value", "lossless"})
    if value["type"] not in {"string", "number_token", "number", "boolean", "null"} or type(value["lossless"]) is not bool:
        raise CollectorError("SCALAR_TYPE")
    v, t = value["value"], value["type"]
    if t in {"string", "number_token"}:
        if not isinstance(v, str) or len(v) > 32768:
            raise CollectorError("SCALAR_SIZE")
        if t == "number_token" and not re.fullmatch(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?", v):
            raise CollectorError("NUMBER_TOKEN")
    elif t == "number":
        if type(v) not in {int, float} or not math.isfinite(v) or abs(v) > 9007199254740991:
            raise CollectorError("UNSAFE_NUMBER")
    elif t == "boolean" and type(v) is not bool:
        raise CollectorError("SCALAR_TYPE")
    elif t == "null" and v is not None:
        raise CollectorError("SCALAR_TYPE")


def validate_scope(scope):
    keys(scope, {"domains", "start_date", "end_date", "include_body", "retention_days"})
    if not isinstance(scope["domains"], list) or not scope["domains"] or len(set(scope["domains"])) != len(scope["domains"]) or not set(scope["domains"]) <= DOMAINS:
        raise CollectorError("SCOPE_DOMAINS")
    if type(scope["include_body"]) is not bool or type(scope["retention_days"]) is not int or not 1 <= scope["retention_days"] <= 30:
        raise CollectorError("SCOPE_POLICY")
    for key in ("start_date", "end_date"):
        if not isinstance(scope[key], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", scope[key]):
            raise CollectorError("SCOPE_DATE")
        try:
            datetime.strptime(scope[key], "%Y-%m-%d")
        except ValueError:
            raise CollectorError("SCOPE_DATE") from None
    if scope["start_date"] > scope["end_date"]:
        raise CollectorError("SCOPE_DATE")


def validate_observation(item, scope):
    keys(item, {"contract", "domain", "source", "context", "observed_at", "fields", "coverage", "issues"})
    if item["contract"] != CONTRACT or item["domain"] not in scope["domains"]:
        raise CollectorError("OBSERVATION_SCOPE")
    stamp(item["observed_at"])
    source = item["source"]
    keys(source, {"origin", "path", "adapter", "transport", "record_ref", "raw_response_saved"})
    if source["origin"] not in SOURCE_ORIGINS or source["transport"] not in {"site_get", "native_sdk", "selected_jsonl", "capability"} or source["raw_response_saved"] is not False:
        raise CollectorError("SOURCE_SCOPE")
    for k, maxlen in (("path", 240), ("adapter", 80), ("record_ref", 180)):
        if not isinstance(source[k], str) or len(source[k]) > maxlen or "?" in source[k] or "#" in source[k] or FORBIDDEN.search(source[k]):
            raise CollectorError("SOURCE_LOCATOR")
    if not source["path"].startswith("/") or not source["record_ref"].startswith("/"):
        raise CollectorError("SOURCE_LOCATOR")
    context = item["context"]
    keys(context, {"owner", "app_scope", "session", "conversation", "period_label", "period_start", "period_end", "source_timezone", "source_cutoff"})
    for k in ("owner", "app_scope", "session", "conversation"):
        if context[k] is not None:
            scalar(context[k])
    for k in ("period_label", "period_start", "period_end", "source_timezone", "source_cutoff"):
        if context[k] is not None and (not isinstance(context[k], str) or len(context[k]) > 80):
            raise CollectorError("CONTEXT_FORMAT")
    fields = item["fields"]
    if not isinstance(fields, dict) or len(fields) > 160:
        raise CollectorError("FIELDS_SIZE")
    for path, value in fields.items():
        if not isinstance(path, str) or not KEY.fullmatch(path) or FORBIDDEN.search(path):
            raise CollectorError("FIELD_REJECTED")
        if not scope["include_body"] and re.search(r"(?:^|[./_])(text|content|body)(?:$|[./_])", path, re.I):
            raise CollectorError("BODY_NOT_ALLOWED")
        scalar(value)
    coverage = item["coverage"]
    keys(coverage, {"status", "basis", "received", "limit", "terminal"})
    if coverage["status"] not in {"partial", "unknown", "not_available"} or coverage["terminal"] not in {"unknown", "source_no_more", "budget", "cancelled", "selected_file_end", "cache_only"}:
        raise CollectorError("COVERAGE_CLAIM")
    if not isinstance(coverage["basis"], str) or not re.fullmatch(r"[a-z_]{1,60}", coverage["basis"]):
        raise CollectorError("COVERAGE_BASIS")
    for k in ("received", "limit"):
        if coverage[k] is not None and (type(coverage[k]) is not int or not 0 <= coverage[k] <= 1000000):
            raise CollectorError("COVERAGE_COUNT")
    if not isinstance(item["issues"], list) or len(item["issues"]) > 30 or any(not isinstance(x, str) or not re.fullmatch(r"[A-Z0-9_]{1,80}", x) for x in item["issues"]):
        raise CollectorError("ISSUE_CODE")
    if len(encode(item)) > 256 * 1024:
        raise CollectorError("OBSERVATION_SIZE")
