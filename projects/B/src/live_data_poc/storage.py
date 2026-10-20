"""B-owned SQLite with atomic imports, scoped references and explicit revisions."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import uuid

from .guard import safe_path, check_database_paths
from .models import CONTRACT, ContractError, SCHEMAS, canonical, digest, validate_dataset
from .identity import evaluate_links
from .semantics import coverage_domain, validate_semantics

APPLICATION_ID = 0x42504F43
TABLES = {"sources": "data_source", "evidence": "evidence_record", "sessions": "live_session",
          "identifiers": "subject_identifier", "coverage": "coverage_record", "events": "live_event",
          "watch_segments": "watch_segment", "metrics": "metric_observation", "boards": "leaderboard_snapshot",
          "rank_rows": "leaderboard_row", "speech": "host_speech", "links": "identity_link_candidate"}
ORDER = tuple(TABLES)


def record_key(kind, r):
    return r[{"sources": "source_id", "evidence": "evidence_id"}.get(kind, "record_id")]


def validate_relations(data):
    maps = {k: {} for k in ORDER}
    global_records = {}
    for kind in ORDER:
        for row in data[kind]:
            key = record_key(kind, row)
            if key in maps[kind] or key in global_records:
                raise ContractError("DUPLICATE_RECORD_ID")
            maps[kind][key] = row
            global_records[key] = row

    def target(r, kind, field, *, same_source=False):
        key = r.get(field)
        if key is None:
            return
        t = maps[kind].get(key)
        if t is None:
            raise ContractError("MISSING_REFERENCE", field)
        if any(t[k] != r[k] for k in ("account_scope_id", "app_scope_id")):
            raise ContractError("CROSS_SCOPE_REFERENCE", field)
        if same_source and t["source_id"] != r["source_id"]:
            raise ContractError("CROSS_SOURCE_SUBJECT", field)

    for kind in ORDER:
        for r in data[kind]:
            target(r, "sources", "source_id")
            if kind not in {"sources", "evidence"}:
                target(r, "evidence", "evidence_id", same_source=True)
            if "session_key" in r:
                target(r, "sessions", "session_key")
            if "source_subject_key" in r:
                target(r, "identifiers", "source_subject_key", same_source=True)
                u = maps["identifiers"].get(r["source_subject_key"])
                if u and u["role"] != "viewer":
                    raise ContractError("FACT_SUBJECT_NOT_VIEWER")
            if "coverage_id" in r:
                target(r, "coverage", "coverage_id", same_source=True)
                c = maps["coverage"][r["coverage_id"]]
                if c["domain"] not in coverage_domain(kind, r):
                    raise ContractError("COVERAGE_DOMAIN_MISMATCH")
                if c["session_key"] != r.get("session_key"):
                    raise ContractError("COVERAGE_SESSION_MISMATCH")
                if "period" in r and r["period"] != c["period"]:
                    raise ContractError("COVERAGE_PERIOD_MISMATCH")
            if kind == "rank_rows":
                target(r, "boards", "snapshot_id", same_source=True)
            if kind == "identifiers" and r["namespace"] != maps["sources"][r["source_id"]]["namespace"]:
                raise ContractError("IDENTIFIER_NAMESPACE")
            if kind in {"evidence", "links"}:
                for f in ("left_id", "right_id"):
                    if r[f] is not None and r[f] not in maps["identifiers"]:
                        raise ContractError("MISSING_IDENTITY_ENDPOINT")
                if kind == "evidence" and ((r["kind"] == "synthetic_direct_pair") != (r["left_id"] is not None and r["right_id"] is not None)):
                    raise ContractError("PAIR_EVIDENCE_SHAPE")
                if kind == "links":
                    proof = maps["evidence"][r["evidence_id"]]
                    if r["state"] == "direct_evidence" and (r["method"] != "direct_pair" or proof["kind"] != "synthetic_direct_pair" or (proof["left_id"], proof["right_id"]) != (r["left_id"], r["right_id"])):
                        raise ContractError("UNSUPPORTED_DIRECT_EVIDENCE")
            if kind == "metrics":
                for key in r["input_record_ids"]:
                    dep = global_records.get(key)
                    if not dep or key == r["record_id"] or any(dep[k] != r[k] for k in ("account_scope_id", "app_scope_id")):
                        raise ContractError("DERIVATION_REFERENCE")
                    if dep.get("source_subject_key") != r["source_subject_key"]:
                        raise ContractError("DERIVATION_SUBJECT")
    # Derived records cannot form cycles.
    def visit(key, stack):
        if key in stack:
            raise ContractError("DERIVATION_CYCLE")
        for dep in global_records[key].get("input_record_ids", []):
            visit(dep, stack | {key})
    for r in data["metrics"]:
        visit(r["record_id"], set())
    validate_semantics(data, maps)
    return maps


def _schema_sql():
    statements = ["""
        CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE input_artifact(artifact_id TEXT PRIMARY KEY, content_hash TEXT NOT NULL);
        CREATE TABLE capture_run(run_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL,
          started_at TEXT NOT NULL, status TEXT NOT NULL, records_read INTEGER NOT NULL,
          records_inserted INTEGER NOT NULL, error_code TEXT,
          FOREIGN KEY(artifact_id) REFERENCES input_artifact(artifact_id));
        CREATE TABLE ingest_receipt(artifact_id TEXT NOT NULL, kind TEXT NOT NULL, locator INTEGER NOT NULL,
          record_id TEXT NOT NULL, content_hash TEXT NOT NULL,
          PRIMARY KEY(artifact_id,kind,locator), FOREIGN KEY(artifact_id) REFERENCES input_artifact(artifact_id));
        CREATE TABLE quality_issue(issue_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, payload_json TEXT NOT NULL,
          FOREIGN KEY(run_id) REFERENCES capture_run(run_id));
        CREATE TABLE import_rejection(rejection_id TEXT PRIMARY KEY, observed_at TEXT NOT NULL, error_code TEXT NOT NULL);
        CREATE TABLE data_source(source_id TEXT PRIMARY KEY, account_scope_id TEXT NOT NULL,
          app_scope_id TEXT NOT NULL, payload_json TEXT NOT NULL,
          UNIQUE(source_id,account_scope_id,app_scope_id));
        CREATE TABLE evidence_record(evidence_id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
          account_scope_id TEXT NOT NULL, app_scope_id TEXT NOT NULL, payload_json TEXT NOT NULL,
          UNIQUE(evidence_id,source_id,account_scope_id,app_scope_id),
          FOREIGN KEY(source_id,account_scope_id,app_scope_id)
            REFERENCES data_source(source_id,account_scope_id,app_scope_id));
    """]
    for kind, table in TABLES.items():
        if kind in {"sources", "evidence"}:
            continue
        columns = ["record_id TEXT PRIMARY KEY", "source_id TEXT NOT NULL", "account_scope_id TEXT NOT NULL",
                   "app_scope_id TEXT NOT NULL", "evidence_id TEXT NOT NULL", "revision INTEGER NOT NULL CHECK(revision>=1)",
                   "supersedes_id TEXT", "payload_json TEXT NOT NULL"]
        constraints = ["UNIQUE(record_id,account_scope_id,app_scope_id)",
                       "UNIQUE(record_id,source_id,account_scope_id,app_scope_id)",
                       "FOREIGN KEY(source_id,account_scope_id,app_scope_id) REFERENCES data_source(source_id,account_scope_id,app_scope_id)",
                       "FOREIGN KEY(evidence_id,source_id,account_scope_id,app_scope_id) REFERENCES evidence_record(evidence_id,source_id,account_scope_id,app_scope_id)"]
        for field, target_table in (("session_key", "live_session"), ("source_subject_key", "subject_identifier"),
                                    ("coverage_id", "coverage_record"), ("snapshot_id", "leaderboard_snapshot")):
            if field in SCHEMAS[kind]["properties"]:
                columns.append(f"{field} TEXT")
                if field == "session_key":
                    constraints.append(f"FOREIGN KEY({field},account_scope_id,app_scope_id) REFERENCES {target_table}(record_id,account_scope_id,app_scope_id)")
                else:
                    constraints.append(f"FOREIGN KEY({field},source_id,account_scope_id,app_scope_id) REFERENCES {target_table}(record_id,source_id,account_scope_id,app_scope_id)")
        if kind == "identifiers":
            columns += ["namespace TEXT NOT NULL", "id_type TEXT NOT NULL", "id_value TEXT NOT NULL"]
            constraints.append("UNIQUE(namespace,id_type,account_scope_id,app_scope_id,id_value)")
        if kind == "events":
            columns.append("logical_key TEXT NOT NULL")
            constraints.append("UNIQUE(logical_key,revision)")
        statements.append(f"CREATE TABLE {table}({','.join(columns + constraints)});")
    return "\n".join(statements)


class Store:
    def __init__(self, path, *, create=False):
        self.path = check_database_paths(path)
        if self.path.suffix != ".sqlite3":
            raise ContractError("DATABASE_SUFFIX")
        if create:
            if self.path.exists():
                raise ContractError("DATABASE_ALREADY_EXISTS")
            self.path.parent.mkdir(parents=True, exist_ok=True)
        elif not self.path.is_file():
            raise ContractError("DATABASE_NOT_FOUND")
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self.conn.execute("PRAGMA foreign_keys=ON")
        if self.conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            self.conn.close()
            raise ContractError("FOREIGN_KEYS_DISABLED")
        if create:
            self.conn.executescript(_schema_sql())
            self.conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
            self.conn.execute("INSERT INTO metadata VALUES('contract',?)", (CONTRACT,))
            self.conn.execute("INSERT INTO metadata VALUES('data_class','synthetic')")
            self.conn.commit()
        else:
            if self.conn.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
                self.conn.close()
                raise ContractError("NOT_A_B_SYNTHETIC_DATABASE")
            meta = dict(self.conn.execute("SELECT key,value FROM metadata"))
            if meta != {"contract": CONTRACT, "data_class": "synthetic"}:
                self.conn.close()
                raise ContractError("DATABASE_CONTRACT")
        def authorize(action, arg1, arg2, database, trigger):
            if action in {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH}:
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() == "load_extension":
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_PRAGMA and arg2 is not None:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        self.conn.set_authorizer(authorize)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.conn.close()

    def dataset(self):
        data = {"contract_version": CONTRACT, "data_class": "synthetic", "artifact_id": "syn:database-snapshot"}
        for kind, table in TABLES.items():
            key = {"sources": "source_id", "evidence": "evidence_id"}.get(kind, "record_id")
            data[kind] = [json.loads(r[0]) for r in self.conn.execute(f"SELECT payload_json FROM {table} ORDER BY {key}")]
        return data

    def _logical_key(self, row, artifact_id, locator):
        if row["source_event_id"] is not None:
            return digest([row[k] for k in ("source_id", "account_scope_id", "app_scope_id", "session_key", "event_type", "source_event_id")])
        return digest(["no-source-event-id", artifact_id, "events", locator])

    def ingest(self, data):
        try:
            return self._ingest(data)
        except ContractError as exc:
            # Failed imports retain only a fixed error code, never rejected content.
            self.conn.rollback()
            with self.conn:
                self.conn.execute("INSERT INTO import_rejection VALUES(?,?,?)", ("syn:rejected-" + uuid.uuid4().hex, datetime.now(timezone.utc).isoformat(), exc.code))
            raise

    def _ingest(self, data):
        check_database_paths(self.path)
        validate_dataset(data)
        validate_relations(data)
        run_id = "syn:run-" + uuid.uuid4().hex
        artifact_id, content_hash = data["artifact_id"], digest(data)
        existing = self.conn.execute("SELECT content_hash FROM input_artifact WHERE artifact_id=?", (artifact_id,)).fetchone()
        if existing and existing[0] != content_hash:
            raise ContractError("ARTIFACT_ID_CONTENT_MISMATCH")
        original_data = data
        data = json.loads(canonical(data))
        aliases = {}
        # An independently numbered observation of the same stable message is a
        # receipt, not a new fact. Never deduplicate by nickname/text/time.
        for i, row in enumerate(data["events"]):
            if row["source_event_id"] is None:
                continue
            logical = self._logical_key(row, artifact_id, i)
            found = self.conn.execute("SELECT payload_json FROM live_event WHERE logical_key=? AND revision=?", (logical, row["revision"])).fetchone()
            if found:
                old_row = json.loads(found[0])
                exclude = {"record_id", "evidence_id", "observed_at"}
                business = lambda x: {k: v for k, v in x.items() if k not in exclude}
                if business(row) == business(old_row):
                    aliases[row["record_id"]] = old_row["record_id"]
                    data["events"][i] = old_row
        for kind in ORDER:
            for row in data[kind]:
                for field in ("supersedes_id", "enter_record_id", "leave_record_id"):
                    if row.get(field) in aliases:
                        row[field] = aliases[row[field]]
                if "input_record_ids" in row:
                    row["input_record_ids"] = [aliases.get(k, k) for k in row["input_record_ids"]]
        count = sum(len(data[k]) for k in ORDER)
        inserted = 0
        try:
            with self.conn:
                self.conn.execute("INSERT OR IGNORE INTO input_artifact VALUES(?,?)", (artifact_id, content_hash))
                for kind in ORDER:
                    table = TABLES[kind]
                    for locator, row in sorted(enumerate(data[kind]), key=lambda item: item[1].get("revision", 1)):
                        key = record_key(kind, row)
                        payload = canonical(row)
                        pk = {"sources": "source_id", "evidence": "evidence_id"}.get(kind, "record_id")
                        old = self.conn.execute(f"SELECT payload_json FROM {table} WHERE {pk}=?", (key,)).fetchone()
                        if old:
                            if old[0] != payload:
                                raise ContractError("IMMUTABLE_RECORD_CHANGED")
                        else:
                            values = {k: row[k] for k in (pk, "source_id", "account_scope_id", "app_scope_id")}
                            if kind not in {"sources", "evidence"}:
                                values.update({k: row[k] for k in ("evidence_id", "revision", "supersedes_id")})
                            for field in ("session_key", "source_subject_key", "coverage_id", "snapshot_id"):
                                if field in row:
                                    values[field] = row[field]
                            if kind == "identifiers":
                                values.update({k: row[k] for k in ("namespace", "id_type", "id_value")})
                            if kind == "events":
                                logical = self._logical_key(row, artifact_id, locator)
                                values["logical_key"] = logical
                                previous = self.conn.execute("SELECT record_id,revision FROM live_event WHERE logical_key=? ORDER BY revision DESC LIMIT 1", (logical,)).fetchone()
                                if previous and (row["revision"] != previous[1] + 1 or row["supersedes_id"] != previous[0]):
                                    raise ContractError("EVENT_REVISION_CONFLICT")
                                if not previous and row["revision"] != 1:
                                    raise ContractError("REVISION_PREDECESSOR_MISSING")
                            values["payload_json"] = payload
                            self.conn.execute(f"INSERT INTO {table}({','.join(values)}) VALUES({','.join('?' for _ in values)})", tuple(values.values()))
                            inserted += 1
                        self.conn.execute("INSERT OR IGNORE INTO ingest_receipt VALUES(?,?,?,?,?)", (artifact_id, kind, locator, key, digest(original_data[kind][locator])))
                # Identity conflicts are evaluated from all persisted evidence, not just this input.
                combined = self.dataset()
                validate_dataset(combined)
                validate_relations(combined)
                self.conn.execute("INSERT INTO capture_run VALUES(?,?,?,?,?,?,?)",
                                  (run_id, artifact_id, datetime.now(timezone.utc).isoformat(), "success", count, inserted, None))
        except sqlite3.IntegrityError:
            raise ContractError("DATABASE_CONSTRAINT") from None
        return {"run_id": run_id, "artifact_id": artifact_id, "read": count, "inserted": inserted,
                "replayed": count - inserted, "data_class": "synthetic"}

    def current_events(self):
        # Only revision chains explicitly accepted by ingest; arrival time is not used.
        return [json.loads(r[0]) for r in self.conn.execute("""
          SELECT e.payload_json FROM live_event e
          WHERE NOT EXISTS(SELECT 1 FROM live_event n WHERE n.logical_key=e.logical_key AND n.revision>e.revision)
          ORDER BY e.record_id""")]

    def candidates(self):
        data = self.dataset()
        return evaluate_links(data["links"], data["identifiers"], data["evidence"])

    def save_quality(self, run_id, report):
        with self.conn:
            for i, issue in enumerate(report["issues"]):
                issue_id = "syn:quality-" + digest([run_id, i, issue])[:24]
                self.conn.execute("INSERT OR IGNORE INTO quality_issue VALUES(?,?,?)", (issue_id, run_id, canonical(issue)))
