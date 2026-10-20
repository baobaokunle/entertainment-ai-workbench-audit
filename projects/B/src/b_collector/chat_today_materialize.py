"""Offline candidate: verify saved page blocks and materialize without a source.

No browser, download, new run, approval loader or legacy-data access. Only the
explicit parent and current run are indexed. A production loader must review
the complete inherited run set before using this candidate for real records.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import uuid

from . import CONTRACT
from .chat_today_journal import FORMAT, digest
from .contract import encode, scalar, validate_observation, stamp, KEY, FORBIDDEN
from .security import CollectorError
from .store import uid, now


def typed(value):
    scalar(value)
    return (value['type'], value['value'], value['lossless'])


def identity(owner, conversation, fields):
    """Private semantic key; no coercion, global ID assumption or old key rewrite."""
    sender = fields.get('sender') or fields.get('senderUserId')
    parts = [owner, conversation, sender]
    if any(not isinstance(v, dict) or v.get('type') != 'string'
           or v.get('lossless') is not True or not isinstance(v.get('value'), str) or not v['value'] for v in parts):
        return None
    for name in ('serverId', 'clientId', 'id'):
        value = fields.get(name)
        if isinstance(value, dict) and value.get('type') == 'string' and value.get('lossless') is True and value.get('value'):
            return encode([typed(owner), typed(conversation), name, typed(value), typed(sender)])
    return None


def native_digest(fields):
    # A native Date may only survive in these two projected fields. Do not
    # discard a changed message timestamp merely because it has a b_ prefix.
    return digest([[name, typed(value)] for name, value in sorted(fields.items())
                   if not name.startswith(('b_', 'conversation.'))
                   or name in {'b_chat.created_at_iso', 'b_chat.created_at_basis'}])


def validate_fields(fields):
    if not isinstance(fields, dict) or len(fields) > 160:
        raise CollectorError('FIELDS_SIZE')
    for name, value in fields.items():
        if not isinstance(name, str) or not KEY.fullmatch(name) or FORBIDDEN.search(name):
            raise CollectorError('FIELD_REJECTED')
        scalar(value)
    if len(encode(fields)) > 256 * 1024: raise CollectorError('OBSERVATION_SIZE')


def read_verified_page(journal, page_id):
    """Read within the caller's transaction; returned fields remain private."""
    store = journal.store
    if not store.db.in_transaction: raise CollectorError('CHAT_TODAY_TRANSACTION_REQUIRED')
    journal._check()
    row = store.db.execute('SELECT target_id,ordinal,state,cipher FROM chat_today_pages WHERE id=? AND run_id=?',
                           (uid(page_id), journal.run)).fetchone()
    if not row or row[2] != 'committed': raise CollectorError('CHAT_TODAY_PAGE_NOT_COMMITTED')
    manifest = store._decrypt(row[3]); _, target = journal._target(row[0])
    if (manifest.get('format') != FORMAT or manifest.get('run_id') != journal.run
            or manifest.get('target_id') != row[0] or manifest.get('page_id') != page_id
            or manifest.get('ordinal') != row[1] or manifest.get('owner') != journal.policy['owner']
            or manifest.get('entry_kind') != journal.policy.get('entry_kind','history_only')
            or manifest.get('conversation') != target['conversation']
            or type(manifest.get('projection_blocks')) is not int or not 0 <= manifest['projection_blocks'] <= 100
            or not 1 <= row[1] <= target['pages']):
        raise CollectorError('CHAT_TODAY_BLOCK_INTEGRITY')
    stamp(manifest.get('claimed_at')); stamp(manifest.get('committed_at'))
    if not datetime.fromisoformat(manifest['claimed_at']) <= datetime.fromisoformat(manifest['committed_at']) < journal.window.expires:
        raise CollectorError('CHAT_TODAY_BLOCK_INTEGRITY')
    projected = []; counts = {'selected': 0, 'unresolved': 0}
    for position, cipher in store.db.execute('SELECT position,cipher FROM chat_today_page_blocks WHERE page_id=? ORDER BY position', (page_id,)):
        block = store._decrypt(cipher)
        if (len(projected) >= 100 or position != len(projected) or set(block) != {'format', 'run_id', 'page_id', 'position', 'kind', 'fields'}
                or block['format'] != FORMAT or block['run_id'] != journal.run or block['page_id'] != page_id
                or type(block['position']) is not int or block['position'] != position or block['kind'] not in counts):
            raise CollectorError('CHAT_TODAY_BLOCK_INTEGRITY')
        validate_fields(block['fields'])
        routed = journal.window.route([block['fields']], target['conversation'])
        slot = 'selected_rows' if block['kind'] == 'selected' else 'unresolved_rows'
        if len(routed[slot]) != 1: raise CollectorError('CHAT_TODAY_BLOCK_INTEGRITY')
        counts[block['kind']] += 1
        projected.append({'kind': block['kind'], 'fields': block['fields']})
    if (len(projected) != manifest['projection_blocks'] or digest(projected) != manifest.get('projection_sha256')
            or counts['selected'] != manifest.get('counts', {}).get('selected')
            or counts['unresolved'] != manifest.get('counts', {}).get('unknown_time_or_key')
            or manifest.get('media_receipt') != {'state': 'classification_pending', 'selected_rows': counts['selected'],
                                                'saved_assets': 0, 'unresolved_rows_download_allowed': False}
            or manifest.get('materialization_complete') is not False):
        raise CollectorError('CHAT_TODAY_BLOCK_INTEGRITY')
    journal._check()
    return manifest, projected, hashlib.sha256(row[3]).hexdigest()


class InheritanceIndex:
    """One bounded scan of explicitly pinned parent/current observations.

    Stores native-message digests and media-attempt keys in memory, not bodies.
    This is functional deduplication preparation, not another terminal audit.
    """
    def __init__(self, journal):
        self.journal = journal
        self.versions, self.media_attempted = set(), set()
        self.evidence = {}
        self.parent = deepcopy(journal.policy['parent'])
        self.current_count = None
        store = journal.store
        with store.transaction():
            journal._check()
            for run in (self.parent['run_id'], journal.run):
                row, meta = store._run(run)
                if datetime.fromisoformat(meta['expires_at']) <= datetime.now(timezone.utc):
                    raise CollectorError('RETENTION_EXPIRED')
                seen = 0
                for identifier, sequence, cipher in store.db.execute('SELECT id,sequence,cipher FROM observations WHERE run_id=? ORDER BY sequence', (run,)):
                    seen += 1; payload = store._decrypt(cipher)
                    if (sequence != seen or payload.get('sequence') != sequence or payload.get('run_id') != run
                            or payload.get('observation_id') != identifier):
                        raise CollectorError('CHAT_TODAY_INDEX_CHANGED')
                    item = payload['observation']; validate_observation(item, meta['scope'])
                    if item['domain'] != 'chat': continue
                    if item['context']['owner'] != journal.policy['owner']:
                        raise CollectorError('SOURCE_CHANGED')
                    fields = item['fields']; key = identity(item['context']['owner'], item['context']['conversation'], fields)
                    if key is None: continue
                    if fields.get('b_chat.record_role', {}).get('value') != 'media_attachment':
                        version = (key, native_digest(fields))
                        self.versions.add(version)
                        self.evidence.setdefault(version, {'run_id': run, 'observation_id': identifier,
                                                          'sequence': sequence, 'cipher_sha256': hashlib.sha256(cipher).hexdigest()})
                    status = fields.get('b_chat.media_status', {}).get('value')
                    if (status is not None and status != 'classification_pending'
                            or fields.get('b_chat.media_file_saved', {}).get('value') is True
                            or 'b_chat.media_asset_id' in fields):
                        self.media_attempted.add(key)
                if seen != row[3]: raise CollectorError('CHAT_TODAY_INDEX_CHANGED')
                if run == journal.run: self.current_count = row[3]
            journal._check()

    def check(self):
        journal = self.journal; journal._check()
        if journal.policy['parent'] != self.parent or journal.store._run(journal.run)[0][3] != self.current_count:
            raise CollectorError('CHAT_TODAY_INDEX_CHANGED')


def observation(journal, manifest, position, fields, old_media_attempt):
    f = deepcopy(fields)
    reserved = {'b_chat.record_role', 'b_chat.media_status', 'b_chat.media_asset_id', 'b_chat.media_file_saved',
                'b_chat.observed_at_basis', 'b_request.conversation_id'}
    if reserved & set(f): raise CollectorError('CHAT_TODAY_DERIVED_FIELD_CONFLICT')
    def label(v): return {'type': 'boolean' if type(v) is bool else 'string', 'value': v, 'lossless': True}
    f.update({'b_chat.record_role': label('message'), 'b_chat.media_file_saved': label(False),
              'b_chat.media_status': label('inherited_attempt_not_retried' if old_media_attempt else 'classification_pending'),
              'b_chat.observed_at_basis': label('local_page_commit_time'),
              'b_request.conversation_id': deepcopy(manifest['conversation']['id'])})
    item = {'contract': CONTRACT, 'domain': 'chat',
            'source': {'origin': 'https://www.douyin.com', 'path': '/native-sdk/chat_cache' if manifest['entry_kind']=='cache_only' else '/native-sdk/chat_history',
                       'adapter': 'b-chat-today-materialize/1', 'transport': 'native_sdk',
                       'record_ref': f"/today-pages/{manifest['page_id']}/blocks/{position}", 'raw_response_saved': False},
            'context': {'owner': journal.policy['owner'], 'app_scope': None, 'session': None,
                        'conversation': manifest['conversation']['id'], 'period_label': None,
                        'period_start': None, 'period_end': None, 'source_timezone': None, 'source_cutoff': None},
            'observed_at': manifest['committed_at'], 'fields': f,
            'coverage': {'status': 'partial', 'basis': 'fixed_window_saved_cache_page' if manifest['entry_kind']=='cache_only' else 'fixed_window_saved_history_page',
                         'received': 1, 'limit': None, 'terminal': 'unknown'},
            'issues': ['FIXED_WINDOW_PARTIAL', 'CACHE_ENTRY_COVERAGE_UNKNOWN', 'MEDIA_NOT_MATERIALIZED',
                       'LOCAL_PAGE_COMMIT_TIME', 'SENDER_DIRECTION_NOT_SEND_RECEIPT']}
    validate_observation(item, journal.meta['scope'])
    return item


def verify_receipt(journal, receipt, manifest, projections, source_sha):
    """A lost local return may reuse durable output, never blindly trust counts."""
    counts = receipt.get('counts', {})
    expected = {'messages_inserted', 'exact_duplicates', 'quarantined_rows', 'inherited_media_attempts'}
    if (receipt.get('format') != 'b-chat-today-materialization/1' or receipt.get('run_id') != journal.run
            or receipt.get('page_id') != manifest['page_id'] or receipt.get('source_manifest_sha256') != source_sha
            or receipt.get('projection_sha256') != manifest['projection_sha256']
            or receipt.get('media_saved') != 0 or receipt.get('media_complete') is not False
            or receipt.get('window_complete') is not False
            or not isinstance(counts, dict) or set(counts) != expected
            or any(type(v) is not int or not 0 <= v <= 100 for v in counts.values())
            or sum(counts[k] for k in expected - {'inherited_media_attempts'}) != len(projections)
            or counts['inherited_media_attempts'] > counts['messages_inserted']):
        raise CollectorError('CHAT_TODAY_MATERIALIZATION_CHANGED')
    dispositions = receipt.get('dispositions'); base = receipt.get('observations_before')
    if (not isinstance(dispositions, list) or len(dispositions) != len(projections)
            or type(base) is not int or base < 0
            or base + counts['messages_inserted'] > journal.store._run(journal.run)[0][3]):
        raise CollectorError('CHAT_TODAY_MATERIALIZATION_CHANGED')
    computed = dict.fromkeys(expected, 0); inserted = set()
    for position, disposition in enumerate(dispositions):
        if (not isinstance(disposition, dict) or set(disposition) != {'position', 'kind', 'evidence'}
                or type(disposition['position']) is not int or disposition['position'] != position
                or disposition['kind'] not in {'quarantined', 'inserted', 'duplicate'}):
            raise CollectorError('CHAT_TODAY_MATERIALIZATION_CHANGED')
        projection = projections[position]; fields = projection['fields']
        key = identity(journal.policy['owner'], manifest['conversation']['id'], fields)
        quarantine = projection['kind'] == 'unresolved' or key is None
        if disposition['kind'] == 'quarantined':
            if not quarantine or disposition['evidence'] is not None:
                raise CollectorError('CHAT_TODAY_MATERIALIZATION_CHANGED')
            computed['quarantined_rows'] += 1; continue
        if quarantine: raise CollectorError('CHAT_TODAY_MATERIALIZATION_CHANGED')
        link = disposition['evidence']
        if (not isinstance(link, dict) or set(link) != {'run_id', 'observation_id', 'sequence', 'cipher_sha256'}
                or link['run_id'] not in {journal.run, journal.policy['parent']['run_id']}
                or type(link['sequence']) is not int or link['sequence'] < 1):
            raise CollectorError('CHAT_TODAY_MATERIALIZATION_CHANGED')
        row = journal.store.db.execute('SELECT sequence,cipher FROM observations WHERE id=? AND run_id=?',
                                      (uid(link['observation_id']), link['run_id'])).fetchone()
        if not row or row[0] != link['sequence'] or hashlib.sha256(row[1]).hexdigest() != link['cipher_sha256']:
            raise CollectorError('CHAT_TODAY_MATERIALIZATION_CHANGED')
        payload = journal.store._decrypt(row[1]); item = payload.get('observation', {})
        if (payload.get('run_id') != link['run_id'] or payload.get('observation_id') != link['observation_id']
                or payload.get('sequence') != link['sequence']):
            raise CollectorError('CHAT_TODAY_MATERIALIZATION_CHANGED')
        _, source_meta = journal.store._run(link['run_id'])
        validate_observation(item, source_meta['scope'])
        if disposition['kind'] == 'duplicate':
            # Must refer to an older record or an earlier insertion in this
            # page, never borrow a later record to rewrite historical counts.
            own_page = (item['source']['adapter'] == 'b-chat-today-materialize/1'
                        and item['source']['record_ref'].startswith(f"/today-pages/{manifest['page_id']}/blocks/"))
            if (link['run_id'] == journal.run and link['sequence'] > base and link['observation_id'] not in inserted
                    or own_page and link['observation_id'] not in inserted
                    or item['domain'] != 'chat'
                    or item['fields'].get('b_chat.record_role', {}).get('value') == 'media_attachment'
                    or identity(item['context']['owner'], item['context']['conversation'], item['fields']) != key
                    or native_digest(item['fields']) != native_digest(fields)):
                raise CollectorError('CHAT_TODAY_MATERIALIZATION_CHANGED')
            computed['exact_duplicates'] += 1; continue
        if (link['run_id'] != journal.run or link['observation_id'] in inserted
                or link['sequence'] != base + computed['messages_inserted'] + 1):
            raise CollectorError('CHAT_TODAY_MATERIALIZATION_CHANGED')
        inherited = item.get('fields', {}).get('b_chat.media_status', {}).get('value') == 'inherited_attempt_not_retried'
        if item != observation(journal, manifest, position, fields, inherited):
            raise CollectorError('CHAT_TODAY_MATERIALIZATION_CHANGED')
        inserted.add(link['observation_id']); computed['messages_inserted'] += 1
        computed['inherited_media_attempts'] += int(inherited)
    if computed != counts: raise CollectorError('CHAT_TODAY_MATERIALIZATION_CHANGED')
    return counts


def materialize_page(journal, index, page_id):
    """Atomic local conversion only. No source reread or media attempt occurs."""
    if not isinstance(index, InheritanceIndex) or index.journal is not journal:
        raise CollectorError('CHAT_TODAY_INDEX_CHANGED')
    store = journal.store; pending_versions = set(); pending_media = set(); pending_evidence = {}
    with store.transaction():
        index.check()
        manifest, projections, source_sha = read_verified_page(journal, page_id)
        store.db.execute('''CREATE TABLE IF NOT EXISTS chat_today_materialized(
            page_id TEXT PRIMARY KEY REFERENCES chat_today_pages(id) ON DELETE CASCADE,
            run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,cipher BLOB NOT NULL)''')
        prior = store.db.execute('SELECT cipher FROM chat_today_materialized WHERE page_id=? AND run_id=?', (page_id, journal.run)).fetchone()
        if prior:
            receipt = store._decrypt(prior[0])
            counts = verify_receipt(journal, receipt, manifest, projections, source_sha)
            journal._check()
            return {**counts, 'replayed': True, 'media_saved': 0, 'window_complete': False}
        row, meta = store._run(journal.run, journal.generation, active=True)
        counts = {'messages_inserted': 0, 'exact_duplicates': 0, 'quarantined_rows': 0, 'inherited_media_attempts': 0}
        dispositions = []
        for position, projection in enumerate(projections):
            fields = projection['fields']
            key = identity(journal.policy['owner'], manifest['conversation']['id'], fields)
            if projection['kind'] == 'unresolved' or key is None:
                dispositions.append({'position': position, 'kind': 'quarantined', 'evidence': None})
                counts['quarantined_rows'] += 1; continue
            version = (key, native_digest(fields))
            if version in index.versions or version in pending_versions:
                proof = pending_evidence.get(version) or index.evidence[version]
                dispositions.append({'position': position, 'kind': 'duplicate', 'evidence': proof})
                counts['exact_duplicates'] += 1; continue
            inherited = key in index.media_attempted
            item = observation(journal, manifest, position, fields, inherited)
            identifier = uuid.uuid4().hex; sequence = row[3] + counts['messages_inserted'] + 1
            if sequence > 1000000: raise CollectorError('RUN_RECORD_LIMIT')
            payload = {'observation_id': identifier, 'run_id': journal.run, 'sequence': sequence,
                       'ingested_at': now(), 'observation': item}
            cipher = store._encrypt(payload)
            store.db.execute('INSERT INTO observations VALUES(?,?,?,?)', (identifier, journal.run, sequence, cipher))
            proof = {'run_id': journal.run, 'observation_id': identifier, 'sequence': sequence,
                     'cipher_sha256': hashlib.sha256(cipher).hexdigest()}
            dispositions.append({'position': position, 'kind': 'inserted', 'evidence': proof})
            pending_evidence[version] = proof
            counts['messages_inserted'] += 1; counts['inherited_media_attempts'] += int(inherited)
            pending_versions.add(version)
            if inherited: pending_media.add(key)
        receipt = {'format': 'b-chat-today-materialization/1', 'run_id': journal.run, 'page_id': page_id,
                   'source_manifest_sha256': source_sha, 'projection_sha256': manifest['projection_sha256'],
                   'counts': counts, 'dispositions': dispositions, 'observations_before': row[3],
                   'committed_at': now(), 'media_saved': 0,
                   'media_complete': False, 'window_complete': False}
        store.db.execute('INSERT INTO chat_today_materialized VALUES(?,?,?)', (page_id, journal.run, store._encrypt(receipt)))
        store.db.execute('UPDATE runs SET count=count+? WHERE id=?', (counts['messages_inserted'], journal.run))
        verify_receipt(journal, receipt, manifest, projections, source_sha)
        journal._check()
    index.versions.update(pending_versions); index.media_attempted.update(pending_media)
    index.evidence.update(pending_evidence)
    index.current_count += counts['messages_inserted']
    return {**counts, 'replayed': False, 'media_saved': 0, 'window_complete': False}
