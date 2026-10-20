"""Continue registered backend queues. No planner, body scan or implicit retry."""
import hashlib
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from .security import CollectorError, check_path
from .store import uid, now

CLIENT = 'b-gap-client/1'
CODE_HASH = '819e34a6251e0ad37a7af3613f3818efb83ece61f784eee02bc4026863c8dd7b'
QUEUE_HASH = 'dab1397728e1605a31fff6ee136cb0d69f68e24feb5e6a6afcee5a4956153767'
FROZEN = ('collector-extension/page-read.js', 'collector-extension/engine.js',
          'collector-extension/core.js', 'tools/gap_repair_queue.py',
          'tools/gap_repair_pipe.py', 'tools/run_gap_repair.mjs')


def verify_code(base):
    digest = hashlib.sha256()
    for name in FROZEN:
        digest.update(check_path(Path(base) / name, base).read_bytes())
    if digest.hexdigest() != CODE_HASH:
        raise CollectorError('VERSION_MISMATCH')


def queue_class():
    path = Path(__file__).with_name('gap_queue.py')
    if not path.exists():
        path = Path(__file__).resolve().parents[2] / 'tools' / 'gap_repair_queue.py'
    if hashlib.sha256(path.read_bytes()).hexdigest() != QUEUE_HASH:
        raise CollectorError('VERSION_MISMATCH')
    spec = importlib.util.spec_from_file_location('b_collector_gap_queue', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.GapQueue


class GapBridge:
    def __init__(self, store, *, factory=None, verify=None, code_hash=CODE_HASH):
        self.s = store
        self.factory = factory or queue_class()
        self.verify = verify or (lambda: verify_code(store.base))
        self.code_hash = code_hash
        self.queue = None
        self.owned = None
        self.current = None
        self.account_ok = False
        self.blocked = False

    def metadata(self, run):
        self.s._guard()
        row, meta = self.s._run(uid(run))
        g = meta.get('gap_repair', {})
        if g.get('format') != 'b-backend-gap/1' or 'chat' in meta['scope']['domains'] or 'legacy_chat' in meta['scope']['domains']:
            raise CollectorError('GAP_NOT_REGISTERED')
        if g['code_hash'] != self.code_hash:
            raise CollectorError('VERSION_MISMATCH')
        self.verify()
        # Same frozen queue parent checks, without constructing its DDL-writing instance.
        readonly = self.factory.__new__(self.factory)
        readonly.s = self.s
        readonly.parents(meta)
        for table in ('gap_targets', 'gap_operations'):
            if not self.s.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                raise CollectorError('GAP_NOT_REGISTERED')
        return row, meta

    def inspect(self, run):
        row, meta = self.metadata(run)
        counts = {i: {} for i in (0, 1, 2, 3)}
        for phase, state, n in self.s.db.execute('SELECT phase,state,COUNT(*) FROM gap_operations WHERE run_id=? GROUP BY phase,state', (run,)):
            if phase in counts:
                counts[phase][state] = n
        totals = {0: sum(counts[0].values()), 1: 0, 2: 0, 3: 0}
        domains = meta['scope']['domains']
        # Metadata only: account IDs / room IDs remain local; observation bodies are never read.
        for (cipher,) in self.s.db.execute('SELECT cipher FROM gap_targets WHERE run_id=?', (run,)):
            target = self.s._decrypt(cipher)
            if 'profile' in domains:
                totals[2] += len(target['users'])
            if not target['start'] or not target['end']:
                continue
            start, end = (datetime.strptime(target[k], '%Y-%m-%d %H:%M:%S') for k in ('start', 'end'))
            for domain, step, phase in (('speech', 1800, 1), ('comment', 60, 3), ('gift', 60, 3)):
                if domain not in domains:
                    continue
                n = max(0, int((end - start).total_seconds()) // step + 1)
                excluded = set()
                for d, a, b in target['content_success']:
                    if d != domain:
                        continue
                    a, b = (datetime.strptime(v, '%Y-%m-%d %H:%M:%S') for v in (a, b))
                    delta = int((a - start).total_seconds())
                    if delta >= 0 and delta % step == 0 and a <= end and int((b-a).total_seconds()) == min(step-1, int((end-a).total_seconds())):
                        excluded.add(delta // step)
                totals[phase] += n - len(excluded)
        phases = []
        for phase, c in counts.items():
            done = sum(c.get(k, 0) for k in ('success', 'empty', 'failed'))
            if totals[phase] < done:
                raise CollectorError('REPAIR_RECORD_MISMATCH')
            phases.append(dict(phase=phase, positive=c.get('success', 0), empty=c.get('empty', 0),
                               failed=c.get('failed', 0), unattempted=totals[phase]-done,
                               scheduled=c.get('pending', 0)+c.get('reading', 0)))
        pending = self.s.db.execute("SELECT COUNT(*) FROM gap_operations WHERE run_id=? AND state IN ('pending','reading')", (run,)).fetchone()[0]
        self.metadata(run)  # Discard the whole report if it expired during the scan.
        return {'run_id': run, 'state': row[1], 'records': row[3], 'expires_at': meta['expires_at'],
                'phases': phases, 'can_continue': row[1] != 'running' and pending > 0,
                'requests_finished': pending == 0, 'platform_completeness_verified': False}

    def open(self, run, client):
        if client != CLIENT:
            raise CollectorError('VERSION_MISMATCH')
        if not self.inspect(run)['can_continue']:
            raise CollectorError('GAP_NO_UNATTEMPTED')
        self.queue = self.factory(self.s)
        result = self.queue.resume(run, self.code_hash)
        self.owned = {k: result[k] for k in ('run_id', 'generation')}
        self.current = None
        self.account_ok = self.blocked = False
        # Separate client provenance; the frozen six-file hash is not rewritten.
        try:
            with self.s.transaction():
                _, m = self.s._run(run, result['generation'], active=True)
                m['gap_repair'].setdefault('client_resumes', []).append({'client': CLIENT, 'at': now(), 'queue_sha256': QUEUE_HASH})
                self.s.db.execute('UPDATE runs SET cipher=? WHERE id=?', (self.s._encrypt(m), run))
        except BaseException:
            try:
                self.s.finish(run, result['generation'], 'failed')
                self.clear()
            except Exception:
                pass  # Dispatcher retains ownership if cleanup cannot write.
            raise
        return result

    def claim(self, run, generation):
        if self.blocked:
            raise CollectorError('BACKEND_ACCESS_BLOCKED')
        if self.current:
            raise CollectorError('REPAIR_CURSOR')
        op = self.queue.claim(run, generation)
        if op and op['domain'] != 'account' and not self.account_ok:
            raise CollectorError('ACCOUNT_CHECK_REQUIRED')
        self.current = op
        return op

    def part(self, run, generation, **args):
        if not self.current or args['op_id'] != self.current['id'] or args['attempt'] != self.current['attempt']:
            raise CollectorError('REPAIR_CURSOR')
        return self.queue.part(run, generation, **args)

    def commit(self, run, generation, **args):
        if not self.current or args['op_id'] != self.current['id'] or args['attempt'] != self.current['attempt']:
            raise CollectorError('REPAIR_CURSOR')
        staged = self.queue.stage.get(args['op_id'], {}).get('items', [])
        blocked = any(x['domain'] == 'capability' and (
            x['fields'].get('status', {}).get('value') in ('SOURCE_PERMISSION', 'HTTP_401', 'HTTP_403', 'BACKEND_ACCESS_BLOCKED') or
            str(x['fields'].get('b_diagnostic.http_status', {}).get('value')) in ('401', '403', '429')) for x in staged)
        result = self.queue.commit(run, generation, **args)
        if self.current['domain'] == 'account':
            self.account_ok = result['state'] == 'success'
            blocked |= not self.account_ok
        self.blocked |= blocked
        self.current = None
        return {**result, 'must_stop': self.blocked}

    def finish(self, run, generation, reason):
        if reason == 'completed_requested_scope':
            if self.blocked or self.current or self.s.db.execute("SELECT 1 FROM gap_operations WHERE run_id=? AND state IN ('pending','reading')", (run,)).fetchone():
                raise CollectorError('GAP_INCOMPLETE')
        result = self.s.finish(run, generation, reason)
        self.clear()
        return result

    def clear(self):
        if self.queue:
            self.queue.stage.clear()
        self.current = None
        self.owned = None
        self.account_ok = False
        self.blocked = False
