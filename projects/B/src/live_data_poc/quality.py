"""Quality findings and limited, explicitly observed synthetic aggregates."""
from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal

from .identity import evaluate_links
from .models import current_records, digest, timestamp


def exact(v, unit="count"):
    if v["status"] == "known" and v["relation"] == "eq" and v["unit"] == unit and v["precision"] == "exact":
        return Decimal(v["decimal"])
    return None


def summarize_events(events):
    groups = defaultdict(list)
    for e in events:
        groups[(e["source_id"], e["account_scope_id"], e["app_scope_id"], e["session_key"], e["source_subject_key"], e["event_type"], e["coverage_id"])].append(e)
    result = []
    for key, rows in sorted(groups.items(), key=lambda kv: repr(kv[0])):
        kind = key[5]
        value, flags = None, []
        if kind in {"comment", "enter", "leave"}:
            value = Decimal(len(rows))
        elif kind in {"gift", "like"}:
            if all(r["semantics"] == "delta" and exact(r["quantity"]) is not None for r in rows):
                value = sum((exact(r["quantity"]) for r in rows), Decimal(0))
            elif kind == "gift" and all(r["semantics"] == "cumulative" and r["combo_id"] and exact(r["quantity"]) is not None for r in rows):
                combos = defaultdict(list)
                for r in rows:
                    combos[(r["combo_id"], r["gift_id"])].append(r)
                finals = []
                for members in combos.values():
                    final_rows = [r for r in members if r["combo_final"]]
                    if len(final_rows) != 1:
                        flags.append("GIFT_FINAL_MISSING_OR_AMBIGUOUS")
                        continue
                    final = exact(final_rows[0]["quantity"])
                    if any(exact(r["quantity"]) > final for r in members):
                        flags.append("GIFT_COUNTER_CONFLICT")
                    finals.append(final)
                if not flags:
                    value = sum(finals, Decimal(0))
            elif kind == "like" and all(r["semantics"] == "cumulative" and r["occurred_at"] and r["counter_epoch"] and exact(r["quantity"]) is not None for r in rows):
                epochs = {r["counter_epoch"] for r in rows}
                ordered = sorted(rows, key=lambda r: timestamp(r["occurred_at"]))
                amounts = [exact(r["quantity"]) for r in ordered]
                if len(epochs) != 1 or any(b < a for a, b in zip(amounts, amounts[1:])):
                    flags.append("LIKE_COUNTER_RESET")
                elif len(set(timestamp(r["occurred_at"]) for r in rows)) != len(rows):
                    flags.append("LIKE_COUNTER_ORDER_UNKNOWN")
                else:
                    # This is a reported cumulative snapshot, NOT clicks during capture.
                    value = amounts[-1]
                    flags.append("CUMULATIVE_SNAPSHOT_NOT_INTERVAL_DELTA")
            else:
                flags.append("QUANTITY_SEMANTICS_UNRESOLVED")
        if key[4] is None:
            flags.append("IDENTITY_UNRESOLVED")
        result.append({"source_id": key[0], "account_scope_id": key[1], "app_scope_id": key[2],
                       "session_key": key[3], "source_subject_key": key[4], "event_type": kind,
                       "coverage_id": key[6], "observed_records": len(rows),
                       "observed_value": str(value) if value is not None else None,
                       "algorithm": "observed-only/1", "input_record_ids": sorted(r["record_id"] for r in rows),
                       "flags": flags})
    return result


def quality_report(data, current_events=None):
    events = current_records(data["events"]) if current_events is None else current_events
    board_coverage = {b["record_id"]: b["coverage_id"] for b in data["boards"]}
    observed = Counter(r["coverage_id"] for r in events + data["watch_segments"])
    observed.update(board_coverage[r["snapshot_id"]] for r in data["rank_rows"])
    issues = []
    def issue(code, key, severity="warning"):
        issues.append({"code": code, "record_id": key, "severity": severity})
    coverage = []
    for c in data["coverage"]:
        reasons = []
        actual = observed[c["record_id"]] if c["domain"] in {"events", "comments", "gifts", "likes", "watch", "ranks"} else None
        if actual is None:
            reasons.append("COUNT_BASIS_UNVERIFIED")
        elif actual != c["observed_count"]:
            reasons.append("OBSERVED_COUNT_MISMATCH")
        pages = c["pages_seen"]
        if len(pages) != len(set(pages)):
            reasons.append("DUPLICATE_PAGE")
        if c["pages_expected"] is not None and set(pages) != set(range(1, c["pages_expected"] + 1)):
            reasons.append("PAGE_GAP")
        if c["gap_count"]:
            reasons.append("CAPTURE_GAP")
        if c["limited"]:
            reasons.append("LIMITED_POPULATION")
        if c["expected_count"] is None:
            reasons.append("DENOMINATOR_UNKNOWN")
        elif c["observed_count"] != c["expected_count"]:
            reasons.append("COUNT_MISMATCH")
        effective = c["status"]
        if effective == "complete" and reasons:
            issue("UNSUPPORTED_COMPLETE_CLAIM", c["record_id"], "error")
            effective = "partial" if any(x != "DENOMINATOR_UNKNOWN" for x in reasons) else "unknown"
        elif reasons:
            for code in reasons:
                issue(code, c["record_id"])
        denominator = c["expected_count"]
        ratio = None
        if denominator and not c["limited"] and actual is not None and actual <= denominator:
            ratio = str(Decimal(actual) / Decimal(denominator))
        coverage.append({"coverage_id": c["record_id"], "claimed": c["status"], "effective": effective,
                         "ratio": ratio, "actual_linked_count": actual, "reasons": reasons, "population": c["population"]})
    aggregates = summarize_events(events)
    for a in aggregates:
        for code in a["flags"]:
            issue(code, a["input_record_ids"][0])
    for kind in ("events", "metrics", "speech"):
        for r in data[kind]:
            if r["occurred_at"] is None:
                issue("OCCURRED_TIME_UNKNOWN", r["record_id"])
    for r in data["watch_segments"]:
        if r["interval_status"] != "paired":
            issue("WATCH_INTERVAL_INCOMPLETE", r["record_id"])
        p, d = exact(r["reported_watch"], "second"), exact(r["derived_watch"], "second")
        if p is not None and d is not None and p != d:
            issue("WATCH_REPORTED_DERIVED_DIFFER", r["record_id"])
    paired = [r for r in data["watch_segments"] if r["interval_status"] == "paired"]
    for i, left in enumerate(paired):
        for right in paired[i + 1:]:
            if all(left[k] == right[k] for k in ("source_id", "account_scope_id", "app_scope_id", "session_key", "source_subject_key")) and max(timestamp(left["enter_at"]), timestamp(right["enter_at"])) < min(timestamp(left["leave_at"]), timestamp(right["leave_at"])):
                issue("WATCH_INTERVAL_OVERLAP_NOT_SUMMED", right["record_id"])
    no_id = defaultdict(list)
    for e in events:
        if e["source_event_id"] is None:
            no_id[digest([e[k] for k in ("source_id", "session_key", "source_subject_key", "event_type", "occurred_at", "text")])].append(e)
    for rows in no_id.values():
        if len(rows) > 1:
            for r in rows:
                issue("POSSIBLE_DUPLICATE_RETAINED", r["record_id"])
    candidates = evaluate_links(data["links"], data["identifiers"], data["evidence"])
    for c in candidates:
        if c["state"] in {"candidate", "unknown", "conflict"}:
            issue("IDENTITY_" + c["state"].upper(), c["record_id"])
    boards = data["boards"]
    if len({digest(b["period"]) for b in boards}) > 1:
        issue("BOARD_WINDOWS_DIFFER_NOT_COMBINED", "syn:board-window-check")
    summary = dict(sorted(Counter(i["code"] for i in issues).items()))
    return {"report_version": "b-quality/0.1", "data_class": "synthetic", "real_platform_validated": False,
            "counts": {k: len(data[k]) for k in data if isinstance(data[k], list)},
            "coverage": coverage, "issues": issues, "issue_counts": summary,
            "identity_states": dict(sorted(Counter(c["state"] for c in candidates).items())),
            "observed_aggregates": aggregates,
            "limitations": ["synthetic_only", "no_platform_accuracy_claim", "no_A_identity_grant",
                            "observed_counts_not_complete_totals", "no_30_day_rank_reconstruction"]}
