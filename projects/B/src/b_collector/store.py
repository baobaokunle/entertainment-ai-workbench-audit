"""Independent append-only ciphertext store. Structural SQLite columns contain no source IDs."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import sqlite3
import uuid
import zipfile

from .contract import encode, decode, validate_scope, validate_observation, UUID, MAX_OBSERVATIONS
from .security import CollectorError, WindowsProtection, check_path, private_root
from .review import bucket, add as add_review
from .repair import BaseRepairPlan


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def uid(value):
    if not isinstance(value, str) or not UUID.fullmatch(value):
        raise CollectorError("INVALID_LOCAL_KEY")
    return value


class Store:
    def __init__(self, base: Path, protector=None):
        self.base = Path(base).resolve()
        self.protector = protector or WindowsProtection()
        self.root = private_root(self.base, self.protector)
        self.path = check_path(self.root / "observations.sqlite3", self.base)
        self._guard()
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA journal_mode=DELETE")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                  id TEXT PRIMARY KEY, generation TEXT NOT NULL, state TEXT NOT NULL,
                  cipher BLOB NOT NULL, count INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS observations (
                  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                  sequence INTEGER NOT NULL, cipher BLOB NOT NULL,
                  UNIQUE(run_id, sequence));
                CREATE TABLE IF NOT EXISTS batches (
                  run_id TEXT NOT NULL REFERENCES runs(id), id TEXT NOT NULL,
                  cipher BLOB NOT NULL, PRIMARY KEY(run_id,id));
                CREATE TABLE IF NOT EXISTS packages (
                  name TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id));
            """)
            self._guard()
        except BaseException:
            self.db.close()
            raise

    def _guard(self):
        check_path(self.root, self.base)
        self.protector.verify_acl(self.root, protected=True)
        for suffix in ("", "-journal", "-wal", "-shm"):
            path = check_path(str(self.path) + suffix, self.base)
            if path.exists():
                self.protector.verify_acl(path)

    def _encrypt(self, value):
        return self.protector.protect(encode(value))

    def _decrypt(self, value):
        return decode(self.protector.unprotect(value))

    @contextmanager
    def transaction(self):
        self._guard()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def start(self, scope):
        validate_scope(scope)
        run, generation = uuid.uuid4().hex, uuid.uuid4().hex
        meta = {"format": "b-capture-run/1", "run_id": run, "scope": scope,
                "started_at": now(), "finished_at": None, "stop_reason": None,
                "expires_at": (datetime.now(timezone.utc) + timedelta(days=scope["retention_days"])).isoformat(),
                "formal_A_acceptance": False}
        with self.transaction():
            self.db.execute("INSERT INTO runs(id,generation,state,cipher) VALUES(?,?,?,?)", (run, generation, "running", self._encrypt(meta)))
        return {"run_id": run, "generation": generation, "state": "running"}

    def repair_scan_page(self, parent, plan=None):
        """Bounded scan; ended append-only parent and fixed count prevent mixed pages."""
        self._guard()
        self.db.execute('BEGIN')
        try:
            row, meta = self._run(parent)
            self._check_repair_ancestors(meta)
            if row[1] == 'running':
                raise CollectorError('STOP_BEFORE_REPAIR')
            if datetime.fromisoformat(meta['expires_at']) <= datetime.now(timezone.utc):
                raise CollectorError('RETENTION_EXPIRED')
            if plan is None:
                plan = BaseRepairPlan(parent, meta, row[3])
            if plan.parent != parent or plan.total != row[3] or plan.meta != meta:
                raise CollectorError('REPAIR_CHANGED')
            initial = plan.offset
            used = 0
            for sequence, cipher in self.db.execute('SELECT sequence,cipher FROM observations WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT 250', (parent, initial)):
                if used >= 8 * 1024 * 1024:
                    break
                plan.feed(self._decrypt(cipher), sequence)
                used += len(cipher)
            if plan.offset == initial and initial < plan.total:
                raise CollectorError('REPAIR_RECORD_MISMATCH')
            if datetime.fromisoformat(meta['expires_at']) <= datetime.now(timezone.utc):
                raise CollectorError('RETENTION_EXPIRED')
            return plan
        finally:
            self.db.rollback()

    def start_base_repair(self, plan):
        repair = plan.finish()
        if not repair['rooms']:
            raise CollectorError('REPAIR_NO_TARGETS')
        with self.transaction():
            row, parent = self._run(plan.parent)
            self._check_repair_ancestors(parent)
            if row[1] == 'running' or row[3] != plan.total or parent != plan.meta:
                raise CollectorError('REPAIR_CHANGED')
            if datetime.fromisoformat(parent['expires_at']) <= datetime.now(timezone.utc):
                raise CollectorError('RETENTION_EXPIRED')
            scope = {**parent['scope'], 'domains': ['account', 'capability', 'session'], 'include_body': False}
            validate_scope(scope)
            run, generation = uuid.uuid4().hex, uuid.uuid4().hex
            meta = {'format': 'b-capture-run/1', 'run_id': run, 'scope': scope,
                    'started_at': now(), 'finished_at': None, 'stop_reason': None,
                    'expires_at': parent['expires_at'], 'formal_A_acceptance': False, 'base_repair': repair}
            self.db.execute('INSERT INTO runs(id,generation,state,cipher,count) VALUES(?,?,?,?,0)', (run, generation, 'running', self._encrypt(meta)))
        return {'run_id': run, 'generation': generation, 'state': 'running', 'scope': scope,
                'owner': repair['owner'], 'app': repair['app'], 'target_count': len(repair['rooms']),
                'unlocated_failures': repair['unlocated_failures'], 'other_diagnostics': repair['other_diagnostics']}

    def repair_targets(self, run, generation, offset):
        if type(offset) is not int or offset < 0:
            raise CollectorError('REPAIR_CURSOR')
        self._guard()
        _, meta = self._run(run, generation, active=True)
        repair = meta.get('base_repair')
        if not repair or offset > len(repair['rooms']):
            raise CollectorError('REPAIR_CURSOR')
        rooms = repair['rooms'][offset:offset + 50]
        return {'rooms': rooms, 'next_offset': offset + len(rooms), 'total': len(repair['rooms'])}

    def _run(self, run, generation=None, active=False):
        row = self.db.execute("SELECT generation,state,cipher,count FROM runs WHERE id=?", (uid(run),)).fetchone()
        if row is None:
            raise CollectorError("RUN_NOT_FOUND")
        if generation is not None and row[0] != uid(generation):
            raise CollectorError("STALE_GENERATION")
        if active and row[1] != "running":
            raise CollectorError("RUN_NOT_ACTIVE")
        meta = self._decrypt(row[2])
        if active and datetime.fromisoformat(meta["expires_at"]) <= datetime.now(timezone.utc):
            raise CollectorError("RETENTION_EXPIRED")
        if active:
            self._check_repair_ancestors(meta)
        return row, meta

    def _check_repair_ancestors(self, meta):
        seen = {meta['run_id']}
        while meta.get('base_repair'):
            parent = meta['base_repair']['parent_run']
            if parent in seen or len(seen) > 1000:
                raise CollectorError('REPAIR_PARENT_UNAVAILABLE')
            seen.add(parent)
            row = self.db.execute('SELECT state,cipher FROM runs WHERE id=?', (parent,)).fetchone()
            if not row or row[0] == 'running':
                raise CollectorError('REPAIR_PARENT_UNAVAILABLE')
            meta = self._decrypt(row[1])
            if datetime.fromisoformat(meta['expires_at']) <= datetime.now(timezone.utc):
                raise CollectorError('RETENTION_EXPIRED')

    def append(self, run, generation, batch_id, items):
        uid(batch_id)
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_OBSERVATIONS:
            raise CollectorError("BATCH_SIZE")
        # Digest remains inside DPAPI ciphertext; never indexes a real identifier in clear.
        digest = hashlib.sha256(encode(items)).hexdigest()
        with self.transaction():
            row, meta = self._run(run, generation, active=True)
            duplicate = self.db.execute("SELECT cipher FROM batches WHERE run_id=? AND id=?", (run, batch_id)).fetchone()
            if duplicate:
                receipt = self._decrypt(duplicate[0])
                if receipt["digest"] != digest:
                    raise CollectorError("BATCH_CONFLICT")
                return {"inserted": 0, "replayed": True, "total": row[3]}
            if row[3] + len(items) > 1000000:
                raise CollectorError("RUN_RECORD_LIMIT")
            for i, item in enumerate(items):
                validate_observation(item, meta["scope"])
                repair = meta.get('base_repair')
                if repair:
                    c, f = item['context'], item['fields']
                    if item['source']['origin'] != 'https://anchor.douyin.com':
                        raise CollectorError('REPAIR_SCOPE')
                    if item['domain'] == 'account':
                        if c['owner'] != repair['owner'] or c['app_scope'] != repair['app'] or f.get('user_id_str') != repair['owner'] or f.get('app_id') != repair['app']:
                            raise CollectorError('REPAIR_ACCOUNT_CHANGED')
                    elif not c['session'] or c['session']['type'] != 'string' or c['session']['lossless'] is not True or c['session']['value'] not in repair['rooms']:
                        raise CollectorError('REPAIR_SCOPE')
                    elif item['domain'] == 'session' and (c['owner'] != repair['owner'] or c['app_scope'] != repair['app'] or f.get('room_id') != c['session'] or f.get('anchor_id') != repair['owner']):
                        raise CollectorError('REPAIR_SCOPE')
                    elif item['domain'] == 'capability' and f.get('domain', {}).get('value') != 'session':
                        raise CollectorError('REPAIR_SCOPE')
                observation_id = uuid.uuid4().hex
                payload = {"observation_id": observation_id, "run_id": run,
                           "sequence": row[3] + i + 1, "ingested_at": now(), "observation": item}
                self.db.execute("INSERT INTO observations VALUES(?,?,?,?)", (observation_id, run, payload["sequence"], self._encrypt(payload)))
            self.db.execute("INSERT INTO batches VALUES(?,?,?)", (run, batch_id, self._encrypt({"digest": digest, "count": len(items)})))
            self.db.execute("UPDATE runs SET count=count+? WHERE id=?", (len(items), run))
            return {"inserted": len(items), "replayed": False, "total": row[3] + len(items)}

    def finish(self, run, generation, reason):
        if reason not in {"completed_requested_scope", "cancelled", "source_changed", "disconnected", "failed", "budget"}:
            raise CollectorError("STOP_REASON")
        with self.transaction():
            row, meta = self._run(run, generation)
            if row[1] != "running":
                return {"state": row[1], "total": row[3]}
            meta.update(finished_at=now(), stop_reason=reason)
            state = "finished" if reason == "completed_requested_scope" else reason
            # Rotating the generation permanently rejects every old append capability.
            self.db.execute("UPDATE runs SET generation=?,state=?,cipher=? WHERE id=?", (uuid.uuid4().hex, state, self._encrypt(meta), run))
            return {"state": state, "total": row[3]}

    def list_runs(self):
        self._guard()
        rows = self.db.execute("SELECT id,state,cipher,count FROM runs ORDER BY rowid DESC LIMIT 100").fetchall()
        result = []
        for key, state, cipher, count in rows:
            meta = self._decrypt(cipher)
            result.append({"run_id": key, "state": state, "count": count, "started_at": meta["started_at"],
                           "expires_at": meta["expires_at"], "domains": meta["scope"]["domains"],
                           "base_repair": bool(meta.get('base_repair')), "gap_repair": bool(meta.get('gap_repair'))})
        return result

    def recover_interrupted(self):
        """Host holds the exclusive process mutex before calling this."""
        for run, generation in self.db.execute("SELECT id,generation FROM runs WHERE state='running'").fetchall():
            self.finish(run, generation, "disconnected")

    def delete_run(self, run):
        """Explicit user action, only this tool's own captured records/packages."""
        with self.transaction():
            row, _ = self._run(run)
            if row[1] == "running":
                raise CollectorError("STOP_BEFORE_DELETE")
            for (name,) in self.db.execute("SELECT name FROM packages WHERE run_id=?", (run,)):
                for filename in (name, name.removesuffix(".bcap.zip") + ".partial"):
                    target = check_path(self.root / filename, self.base)
                    if target.exists():
                        self.protector.verify_acl(target)
                        target.unlink()
            self.db.execute("DELETE FROM packages WHERE run_id=?", (run,))
            self.db.execute("DELETE FROM batches WHERE run_id=?", (run,))
            self.db.execute("DELETE FROM observations WHERE run_id=?", (run,))
            self.db.execute("DELETE FROM runs WHERE id=?", (run,))
        return {"deleted": True, "secure_erasure_guaranteed": False}

    def preview(self, run, offset):
        if type(offset) is not int or not 0 <= offset <= 1000000:
            raise CollectorError("PAGE_OFFSET")
        self._guard()
        row, meta = self._run(run)
        if datetime.fromisoformat(meta["expires_at"]) <= datetime.now(timezone.utc):
            raise CollectorError("RETENTION_EXPIRED")
        result = []
        used = 0
        for (cipher,) in self.db.execute("SELECT cipher FROM observations WHERE run_id=? ORDER BY sequence LIMIT 25 OFFSET ?", (run, offset)):
            value = self._decrypt(cipher)
            size = len(encode(value))
            if used + size > 700000:
                break
            result.append(value)
            used += size
        return {"records": result, "next_offset": offset + len(result), "total": row[3]}

    def review_page(self, run, offset, total):
        if type(offset) is not int or offset < 0 or (total is not None and (type(total) is not int or total < offset)):
            raise CollectorError("REVIEW_CURSOR")
        self._guard()
        row, meta = self._run(run)
        if row[1] == "running":
            raise CollectorError("STOP_BEFORE_REVIEW")
        if datetime.fromisoformat(meta["expires_at"]) <= datetime.now(timezone.utc):
            raise CollectorError("RETENTION_EXPIRED")
        if total is None:
            if offset != 0:
                raise CollectorError("REVIEW_CURSOR")
            total = row[3]
        if total != row[3]:
            raise CollectorError("REVIEW_CHANGED")
        summary = {"counts": bucket(), "domains": {}, "offset": offset, "next_offset": offset,
                   "total": total, "selected_domains": sorted(meta["scope"]["domains"]),
                   "state": row[1], "source_complete": False, "formal_A_acceptance": False}
        used = 0
        for sequence, cipher in self.db.execute(
            "SELECT sequence,cipher FROM observations WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT 250", (run, offset)
        ):
            if used >= 8 * 1024 * 1024:
                break
            value = self._decrypt(cipher)
            if sequence != summary["next_offset"] + 1 or value.get("sequence") != sequence or value.get("run_id") != run:
                raise CollectorError("REVIEW_RECORD_MISMATCH")
            add_review(summary, value["observation"])
            summary["next_offset"] = sequence
            used += len(cipher)
        if summary["next_offset"] > total or (summary["next_offset"] == offset and offset < total):
            raise CollectorError("REVIEW_RECORD_MISMATCH")
        summary["done"] = summary["next_offset"] == total
        if datetime.fromisoformat(meta["expires_at"]) <= datetime.now(timezone.utc):
            raise CollectorError("RETENTION_EXPIRED")
        return summary

    def export(self, run):
        self._guard()
        row, meta = self._run(run)
        if row[1] == "running":
            raise CollectorError("STOP_BEFORE_EXPORT")
        if datetime.fromisoformat(meta["expires_at"]) <= datetime.now(timezone.utc):
            raise CollectorError("RETENTION_EXPIRED")
        key = uuid.uuid4().hex
        target = check_path(self.root / f"{key}.bcap.zip", self.base)
        temp = check_path(self.root / f"{key}.partial", self.base)
        # Persist ownership BEFORE any output exists. A crash at every subsequent
        # stage still leaves a discoverable final/partial file for explicit deletion.
        with self.transaction():
            self.db.execute("INSERT INTO packages VALUES(?,?)", (target.name, run))
        # A read transaction binds counts, run state and payloads to one snapshot.
        self.db.execute("BEGIN")
        try:
            row, meta = self._run(run)
            manifest = {"format": "b-encrypted-candidate-package/1", "package_id": key,
                        "protection": "Windows-DPAPI-CurrentUser-null-entropy-BLC1",
                        "transfer_scope": "same_windows_user_on_same_machine",
                        "state": row[1], "record_count": row[3], "source_complete": False, "files": []}
            with temp.open("xb") as handle:
                self.protector.verify_acl(temp)
                with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_STORED) as archive:
                    def write(name, cipher):
                        archive.writestr(name, cipher)
                        manifest["files"].append({"name": name, "bytes": len(cipher), "ciphertext_sha256": hashlib.sha256(cipher).hexdigest()})
                    write("run.dpapi", row[2])
                    actual = 0
                    for oid, cipher in self.db.execute("SELECT id,cipher FROM observations WHERE run_id=? ORDER BY sequence", (run,)):
                        write(f"observations/{oid}.dpapi", cipher)
                        actual += 1
                    if actual != row[3]:
                        raise CollectorError("STORE_COUNT_MISMATCH")
                    archive.writestr("manifest.json", encode(manifest))
                handle.flush()
                import os
                os.fsync(handle.fileno())
            self._guard()
            check_path(temp, self.base)
            if target.exists():
                raise CollectorError("EXPORT_COLLISION")
            temp.rename(target)
            self.protector.verify_acl(target)
            return {"package_id": key, "file_name": target.name, "record_count": actual,
                    "directory": "B/runtime/collector-private", "source_complete": False}
        finally:
            self.db.rollback()
            if temp.exists():
                check_path(temp, self.base).unlink()

    def close(self):
        self.db.close()
