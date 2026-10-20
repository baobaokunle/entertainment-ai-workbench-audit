"""Versioned offline package; no server and no writes to A."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
from pathlib import Path

from . import CONTRACT
from .guard import safe_path
from .models import (ContractError, SCHEMAS, KEY, TIME, array, canonical, digest, enum, integer, load_json_bytes,
                     obj, text, timestamp, validate_dataset, validate_schema)
from .storage import validate_relations
from .identity import evaluate_links
from .quality import quality_report

MAX_FILE = 4 * 1024 * 1024
MAX_TOTAL = 16 * 1024 * 1024
EXPORT_KINDS = tuple(k for k in SCHEMAS if k != "speech")
MANIFEST_SCHEMA = obj({"contract_version": enum(CONTRACT), "package_id": text(68, pattern=r"^syn:[0-9a-f]{64}$"),
    "producer": enum("B-S1-offline"), "data_class": enum("synthetic"), "generated_at": TIME,
    "source_artifact": KEY, "speech_included": enum(False),
    "input_artifacts": array(obj({"artifact_id": KEY, "sha256": text(64, pattern=r"^[0-9a-f]{64}$")})),
    "files": array(obj({"name": text(100), "rows": integer(0, 10000), "bytes": integer(0, MAX_FILE),
                         "sha256": text(64, pattern=r"^[0-9a-f]{64}$")}), 20)})


def package_id(manifest):
    return "syn:" + digest({k: v for k, v in manifest.items() if k != "package_id"})


def assessment(data):
    return {"assessment_version": "b-assessment/0.1", "identity_algorithm": "scoped-current-links/1",
            "scope": "all_records_in_this_synthetic_package_only", "input_sha256": digest(data),
            "current_identity_candidates": evaluate_links(data["links"], data["identifiers"], data["evidence"]),
            "quality": quality_report(data), "grants_A_identity": False,
            "proves_source_authenticity_or_external_completeness": False}


def export_package(store, destination):
    out = safe_path(destination, "runtime")
    if out.exists():
        raise ContractError("EXPORT_DESTINATION_EXISTS")
    data = store.dataset()
    data["speech"] = []
    validate_dataset(data)
    validate_relations(data)
    # All revisions remain available. Consumers must not sum physical versions.
    out.mkdir(parents=True)
    entries = []
    documents = [(k + ".jsonl", data[k]) for k in EXPORT_KINDS] + [("assessment.json", [assessment(data)])]
    for name, rows in documents:
        raw = ("".join(canonical(r) + "\n" for r in rows)).encode("utf-8")
        if len(raw) > MAX_FILE:
            raise ContractError("EXPORT_FILE_LIMIT")
        safe_path(out / name, "runtime").write_bytes(raw)
        entries.append({"name": name, "rows": len(rows), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    manifest = {"contract_version": CONTRACT,
                "producer": "B-S1-offline", "data_class": "synthetic",
                "generated_at": datetime.now(timezone.utc).isoformat(), "source_artifact": data["artifact_id"],
                "speech_included": False, "files": entries,
                "input_artifacts": [{"artifact_id": r[0], "sha256": r[1]} for r in store.conn.execute("SELECT artifact_id,content_hash FROM input_artifact ORDER BY artifact_id")]}
    manifest["package_id"] = package_id(manifest)
    validate_schema(manifest, MANIFEST_SCHEMA)
    safe_path(out / "manifest.json", "runtime").write_text(canonical(manifest) + "\n", encoding="utf-8")
    return verify_package(out)


def verify_package(directory):
    root = safe_path(directory, "runtime")
    manifest_path = safe_path(root / "manifest.json", "runtime", existing=True)
    if manifest_path.stat().st_size > 32768:
        raise ContractError("MANIFEST_LIMIT")
    manifest = load_json_bytes(manifest_path.read_bytes())
    validate_schema(manifest, MANIFEST_SCHEMA)
    timestamp(manifest["generated_at"])
    expected = {k + ".jsonl" for k in EXPORT_KINDS} | {"assessment.json"}
    names = [f["name"] for f in manifest["files"]]
    if len(names) != len(set(names)) or set(names) != expected:
        raise ContractError("PACKAGE_FILE_ALLOWLIST")
    if {p.name for p in root.iterdir()} != expected | {"manifest.json"}:
        raise ContractError("UNDECLARED_PACKAGE_FILE")
    if manifest["package_id"] != package_id(manifest):
        raise ContractError("PACKAGE_ID_CONTENT_MISMATCH")
    if sum(f["bytes"] for f in manifest["files"]) > MAX_TOTAL:
        raise ContractError("PACKAGE_SIZE_LIMIT")
    data = {"contract_version": CONTRACT, "data_class": "synthetic", "artifact_id": manifest["source_artifact"], "speech": []}
    claimed = None
    for f in manifest["files"]:
        p = safe_path(root / f["name"], "runtime", existing=True)
        if p.stat().st_size != f["bytes"] or p.stat().st_size > MAX_FILE:
            raise ContractError("PACKAGE_FILE_SIZE")
        raw = p.read_bytes()
        if hashlib.sha256(raw).hexdigest() != f["sha256"]:
            raise ContractError("PACKAGE_FILE_HASH")
        rows = [load_json_bytes(line) for line in raw.splitlines()]
        if len(rows) != f["rows"]:
            raise ContractError("PACKAGE_ROW_COUNT")
        if f["name"] == "assessment.json":
            if len(rows) != 1:
                raise ContractError("ASSESSMENT_ROWS")
            claimed = rows[0]
        else:
            data[f["name"][:-6]] = rows
    validate_dataset(data)
    validate_relations(data)
    if canonical(claimed) != canonical(assessment(data)):
        raise ContractError("ASSESSMENT_RECOMPUTATION_MISMATCH")
    return {"valid": True, "data_class": "synthetic", "package_id": manifest["package_id"],
            "files": len(names), "rows": sum(f["rows"] for f in manifest["files"]), "speech_included": False}


def export_review_csv(report, path):
    """Only fixed codes and synthetic keys; no text/IDs/credentials or formulas."""
    p = safe_path(path, "runtime")
    if p.exists():
        raise ContractError("CSV_DESTINATION_EXISTS")
    with p.open("x", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["code", "record_id", "severity"])
        writer.writeheader()
        writer.writerows(report["issues"])
