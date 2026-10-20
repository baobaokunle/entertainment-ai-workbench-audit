from __future__ import annotations

import argparse
import hashlib
from datetime import datetime, timezone
import json
import platform
import sqlite3
import sys
import unittest
import uuid

from .guard import ROOT, BoundaryError, safe_path, snapshot
from .models import ContractError, canonical, load_json_bytes
from .storage import Store
from .quality import quality_report
from .export import export_package, export_review_csv, verify_package


def fixture():
    p = safe_path("scenario.json", "fixtures/synthetic", existing=True)
    if p.stat().st_size > 4 * 1024 * 1024:
        raise ContractError("INPUT_LIMIT")
    return load_json_bytes(p.read_bytes())


def new_run_dir(prefix):
    key = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    out = safe_path(prefix + "-" + key, "runtime")
    out.mkdir(parents=True)
    return out


def source_fingerprint():
    files = [ROOT / "run.py", ROOT / "run.ps1", ROOT / "dependencies.lock.json"]
    for area, pattern in (("src", "*.py"), ("tests", "*.py"), ("fixtures/synthetic", "*.json"), ("contracts", "*.json")):
        files.extend((ROOT / area).rglob(pattern))
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(safe_path(p, existing=True).read_bytes()).hexdigest()
            for p in sorted(files)}


def demo():
    out = new_run_dir("demo")
    with Store(out / "synthetic.sqlite3", create=True) as store:
        data = fixture()
        first = store.ingest(data)
        second = store.ingest(data)
        quality = quality_report(store.dataset(), store.current_events())
        store.save_quality(first["run_id"], quality)
        (out / "quality.json").write_text(canonical(quality) + "\n", encoding="utf-8")
        export_review_csv(quality, out / "quality-review.csv")
        package = export_package(store, out / "package")
        db_check = store.conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk_check = list(store.conn.execute("PRAGMA foreign_key_check"))
    summary = {"stage": "S1", "data_class": "synthetic", "real_platform_validated": False,
       "first_import": first, "reimport": second, "database_integrity": db_check,
       "foreign_key_violations": len(fk_check), "counts": quality["counts"],
       "identity_states": quality["identity_states"], "quality_issue_counts": quality["issue_counts"],
       "package_validation": package, "output_directory": out.relative_to(ROOT).as_posix(),
       "python": platform.python_version(), "sqlite": sqlite3.sqlite_version, "source_sha256": source_fingerprint(), "guard": snapshot()}
    (out / "summary.json").write_text(canonical(summary) + "\n", encoding="utf-8")
    (ROOT / "results" / "latest-demo.json").write_text(canonical(summary) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


class EvidenceResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.cases.append({"test": test.id(), "status": "passed"})

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.cases.append({"test": test.id(), "status": "failed"})

    def addError(self, test, err):
        super().addError(test, err)
        self.cases.append({"test": test.id(), "status": "error"})

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.cases.append({"test": test.id(), "status": "skipped", "reason": reason})

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        self.cases.append({"test": subtest.id(), "status": "passed" if err is None else "failed"})


def tests():
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2, resultclass=EvidenceResult).run(suite)
    report = {"stage": "S1", "data_class": "synthetic", "tests_run": result.testsRun,
              "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped),
              "successful": result.wasSuccessful() and result.testsRun > 0, "cases": result.cases, "guard": snapshot(),
              "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
              "real_platform_validated": False, "source_sha256": source_fingerprint(),
              "error_code": "NO_TESTS_DISCOVERED" if result.testsRun == 0 else None}
    directory = new_run_dir("tests")
    (directory / "test-results.json").write_text(canonical(report) + "\n", encoding="utf-8")
    (ROOT / "results" / "latest-tests.json").write_text(canonical(report) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in {"cases", "guard", "source_sha256"}}, ensure_ascii=False))
    return 0 if report["successful"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="B S1: synthetic offline only")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo", help="create a new synthetic DB, quality report and verified export")
    sub.add_parser("test", help="run offline isolation/contract/data tests")
    verify = sub.add_parser("verify", help="verify an existing B/runtime package")
    verify.add_argument("package")
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            return demo()
        if args.command == "test":
            return tests()
        print(json.dumps(verify_package(safe_path(args.package, "runtime")), ensure_ascii=False))
        return 0
    except (ContractError, BoundaryError) as exc:
        print(json.dumps({"error": str(exc), "data_class": "synthetic"}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error):
        print('{"error":"LOCAL_IO_OR_DATABASE_ERROR","data_class":"synthetic"}', file=sys.stderr)
        return 2
