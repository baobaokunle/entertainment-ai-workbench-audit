"""One independently evidenced message from one approved pending import row.

No schema, source enrollment, platform IO, bulk promotion or background work.
The retained pending observation never changes. All readers use the same check.
"""
import hashlib
import json
import re
from types import SimpleNamespace

from .common import AppError
from .db import digest, stable_key
from .identity import IdentityService, require_text
from .local_deletion import reject_hidden, observation_selectors

FIELD = 'local_import_derivation'
FORMAT = 'dfa-local-import-derivation-1'
PROOFS = 'local_import_message_proofs'
CAPS = ('account_identity', 'person_identity', 'conversation_identity', 'message_identity',
        'message_body', 'message_direction', 'message_time', FIELD)
REFERENCE_FIELDS = {'observation_key', 'semantic_digest', 'version_digest', 'batch_id', 'source_key',
                    'binding', 'snapshot_sha256', 'preview_sha256'}
FACT_FIELDS = {'owner_uid', 'peer_uid', 'conversation_kind', 'message_kind', 'platform_conversation_key', 'platform_message_key',
               'body_sha256', 'direction', 'occurred_lower_utc', 'occurred_upper_utc', 'time_precision'}


def fail():
    raise AppError(409, 'DERIVED_IMPORT_UNVERIFIED', '此条聊天的独立依据或原导入记录已失配，保留待核验，不继续使用。')


def _json(raw):
    try:
        value = json.loads(raw)
        if type(value) is dict:return value
    except (TypeError, ValueError, RecursionError):pass
    fail()


def _registered_source(db, source_id, version):
    source = IdentityService(db).get_source(source_id, version)
    if source is None:fail()
    semantic = {key: source[key] for key in ('source_id', 'source_version', 'source_kind', 'capabilities', 'verified', 'evidence')}
    if source['registration_digest'] != digest(semantic):fail()
    return source


def is_derived_source(db, source_id, version):
    source = IdentityService(db).get_source(source_id, version)
    if not source:return False
    caps = source['capabilities']
    marked = caps.get(FIELD) is True if type(caps) is dict else FIELD in caps
    return marked or type(source['evidence']) is dict and PROOFS in source['evidence']


def _candidate(db, reference, device, account):
    from .file_import_index import candidate
    from .import_binding import ImportBindingService
    if type(reference) is not dict or set(reference) != REFERENCE_FIELDS:fail()
    binding = reference['binding']
    ImportBindingService._binding(binding)
    if binding['account_key'] != account:fail()
    for key in ('semantic_digest', 'version_digest', 'snapshot_sha256', 'preview_sha256'):
        if type(reference[key]) is not str or not re.fullmatch('[0-9a-f]{64}', reference[key]):fail()
    conn = db.conn
    row = conn.execute('SELECT * FROM message_observation WHERE observation_key=?', (reference['observation_key'],)).fetchone()
    if row is None:fail()
    payload = _json(row['payload_json']); item = candidate(payload)
    permanent = conn.execute('SELECT * FROM file_import_binding WHERE account_namespace=?', (binding['account_namespace'],)).fetchone()
    receipts = conn.execute('''SELECT i.source_key,i.batch_id,b.* FROM file_import_item i
        JOIN file_import_batch b ON b.batch_id=i.batch_id WHERE i.observation_key=? LIMIT 2''', (row['observation_key'],)).fetchall()
    if item is None or len(receipts) != 1 or permanent is None:fail()
    batch = receipts[0]
    if (any(permanent[k] != v or batch[k] != v for k, v in binding.items()) or
            any(row[k] != binding[k] for k in ('account_key', 'source_id', 'source_version')) or
            row['device_key'] != device or batch['device_key'] != device or row['message_key'] is not None or
            row['status'] != 'pending' or batch['status'] != 'committed' or
            row['semantic_digest'] != digest(payload) or row['semantic_digest'] != reference['semantic_digest'] or
            digest(item['version']) != reference['version_digest'] or
            item['batch_id'] != batch['batch_id'] or item['batch_id'] != reference['batch_id'] or
            item['source_key'] != batch['source_key'] or item['source_key'] != reference['source_key'] or
            item['account_namespace'] != binding['account_namespace'] or
            item['record'].get('source_key') != item['source_key'] or
            item['record'].get('account_namespace') != binding['account_namespace'] or
            item['record'].get('revision_state') != 'single_candidate' or
            item['version'].get('original_body') != payload.get('body') or
            batch['input_digest'] != reference['preview_sha256'] or
            conn.execute('SELECT COUNT(*) FROM file_import_item WHERE batch_id=? AND source_key=?',
                         (batch['batch_id'], batch['source_key'])).fetchone()[0] != 1):fail()
    source = _registered_source(db, binding['source_id'], binding['source_version'])
    versions = conn.execute('''SELECT DISTINCT json_extract(json_extract(o.payload_json,'$.export_candidate_json'),'$.version')
        FROM file_import_item i JOIN file_import_batch b ON b.batch_id=i.batch_id
        JOIN message_observation o ON o.observation_key=i.observation_key
        WHERE b.account_key=? AND b.source_id=? AND b.source_version=? AND b.account_namespace=? AND i.source_key=? LIMIT 2''',
        (account, binding['source_id'], binding['source_version'], binding['account_namespace'], reference['source_key'])).fetchall()
    if len(versions) != 1 or digest(_json(versions[0][0])) != reference['version_digest']:fail()
    # Export registration stays deliberately unverified and receives no fact grants.
    raw = conn.execute('SELECT * FROM source WHERE source_id=? AND source_version=?',
                       (binding['source_id'], binding['source_version'])).fetchone()
    ImportBindingService._caps(raw, account)
    approval = source['evidence'].get('local_import', {})
    if (approval.get('format') != 'dfa-approved-local-import-1' or
            approval.get('account_key') != account or approval.get('account_namespace') != binding['account_namespace'] or
            any(approval.get(k) != reference[k] for k in ('snapshot_sha256', 'preview_sha256')) or
            IdentityService(db).origin_withdrawn(device, account, binding['source_id'], binding['source_version'])):fail()
    reject_hidden(db, account, observation_key=row['observation_key'], **observation_selectors(payload))
    return row, item, payload


def build_observation(db, source_id, version, proof_id, device):
    """Construct only from an already registered, independently qualified proof."""
    from .file_import import FileImportService
    from .message_facts import normalize_time
    source = _registered_source(db, source_id, version)
    records = source['evidence'].get(PROOFS) if type(source['evidence']) is dict else None
    if (source['source_kind'] != 'qualified_adapter' or type(records) is not list or
            not 1 <= len(records) <= 100 or any(type(p) is not dict for p in records)):fail()
    matches = [p for p in records if p.get('proof_id') == proof_id]
    if len(matches) != 1:fail()
    proof = matches[0]
    if set(proof) != {'proof_id', 'candidate', 'fact'} or type(proof['fact']) is not dict or set(proof['fact']) != FACT_FIELDS:fail()
    require_text(proof_id, 'proof_id', 128)
    fact, reference = proof['fact'], proof['candidate']
    if type(reference) is not dict or type(reference.get('binding')) is not dict:fail()
    account = reference['binding'].get('account_key'); ids = IdentityService(db)
    if any(not ids.trusted(source_id, version, cap, account) for cap in CAPS) or ids.origin_withdrawn(device, account, source_id, version):fail()
    if (source_id, version) == (reference['binding'].get('source_id'), reference['binding'].get('source_version')):fail()
    for key in ('owner_uid', 'peer_uid'):
        if type(fact[key]) is not str or not re.fullmatch('[0-9]{1,128}', fact[key]):fail()
    if stable_key('account', 'douyin', fact['owner_uid']) != account or fact['owner_uid'] == fact['peer_uid']:fail()
    person = stable_key('person', account, 'douyin', 'uid', fact['peer_uid'])
    if FileImportService(db)._pending_sender_uid(account, person, SimpleNamespace(device_key=device)) != fact['peer_uid']:fail()
    owner = db.conn.execute('SELECT source_id,source_version FROM own_account WHERE account_key=?', (account,)).fetchone()
    alias = db.conn.execute("SELECT source_id,source_version FROM identity_alias WHERE account_key=? AND person_key=? AND platform='douyin' AND identity_kind='uid'",
                            (account, person)).fetchone()
    for identity in (owner, alias):
        if identity is None:fail()
        _registered_source(db, identity['source_id'], identity['source_version'])
    _, item, payload = _candidate(db, reference, device, account)
    for key in ('platform_conversation_key', 'platform_message_key'):require_text(fact[key], key, 256)
    if (fact['conversation_kind'] != 'person' or fact['message_kind'] != 'text' or
            fact['direction'] not in ('in', 'out') or fact['time_precision'] not in ('second', 'minute', 'date') or
            fact['body_sha256'] != hashlib.sha256(payload['body'].encode('utf-8')).hexdigest() or
            item['record'].get('platform_message_id') != fact['platform_message_key'] or
            item['record'].get('platform_conversation_id') != fact['platform_conversation_key'] or
            item['version'].get('platform_sender_id') != fact['peer_uid' if fact['direction'] == 'in' else 'owner_uid']):fail()
    lower, upper = normalize_time(fact['occurred_lower_utc']), normalize_time(fact['occurred_upper_utc'])
    if lower is None or upper is None or lower > upper:fail()
    # The independently reviewed record binds precision; no timestamp inference.
    lineage = {'format': FORMAT, 'candidate': reference, 'proof_id': proof_id,
               'proof_digest': digest(proof), 'source_digest': source['registration_digest']}
    return {'observation_id': stable_key('reviewed-import', proof_id), 'source_id': source_id, 'source_version': version,
            'account_key': account, 'object_kind': 'person', 'identity_kind': 'uid', 'identity_value': fact['peer_uid'],
            'platform_conversation_key': fact['platform_conversation_key'], 'platform_message_key': fact['platform_message_key'],
            'direction': fact['direction'], 'kind': 'text', 'body': payload['body'],
            'occurred_lower_utc': lower, 'occurred_upper_utc': upper, 'time_precision': fact['time_precision'],
            'platform_success': 'unknown', 'coverage': {'state': 'partial'}, FIELD: lineage}


def validate_observation(db, payload, device):
    lineage = payload.get(FIELD)
    if type(lineage) is not dict or set(lineage) != {'format', 'candidate', 'proof_id', 'proof_digest', 'source_digest'} or lineage['format'] != FORMAT:fail()
    expected = build_observation(db, payload.get('source_id'), payload.get('source_version', '1'), lineage['proof_id'], device)
    if payload != expected:fail()
    return lineage


def check_message(db, row):
    """Caller holds DB lock. Invalid derived rows reject the whole read, no writes."""
    conn = db.conn
    observations = conn.execute('''SELECT o.* FROM message_observation o
        WHERE o.message_key=? ORDER BY o.observation_key''', (row['message_key'],)).fetchall()
    marked = []
    for o in observations:
        payload = _json(o['payload_json'])
        if FIELD in payload or is_derived_source(db, o['source_id'], o['source_version']):marked.append((o, payload))
    quality = _json(row['quality_json'])
    if not marked and quality.get('evidence_class') != FORMAT:return None
    if len(observations) != 1 or len(marked) != 1:fail()
    o, payload = marked[0]
    lineage = validate_observation(db, payload, o['device_key'])
    revision = conn.execute('SELECT * FROM message_revision WHERE message_key=? AND revision=?',
                            (row['message_key'], row['active_body_revision'])).fetchone()
    conversation = conn.execute('SELECT * FROM conversation WHERE conversation_key=?', (row['conversation_key'],)).fetchone()
    person = stable_key('person', payload['account_key'], 'douyin', 'uid', payload['identity_value'])
    if (o['status'] != 'accepted' or o['semantic_digest'] != digest(payload) or
            any(o[k] != payload[k] for k in ('account_key', 'source_id', 'source_version')) or
            revision is None or revision['observation_key'] != o['observation_key'] or
            revision['body'] != payload['body'] or revision['body_sha256'] != hashlib.sha256(payload['body'].encode()).hexdigest() or
            conversation is None or conversation['kind'] != 'person' or conversation['person_key'] != person or
            conversation['account_key'] != payload['account_key'] or
            conversation['platform_conversation_key'] != payload['platform_conversation_key'] or
            row['conversation_key'] != stable_key('conversation', payload['account_key'], 'person', payload['platform_conversation_key']) or
            row['person_key'] != person or row['identity_state'] != 'exact' or row['kind'] != 'text' or
            any(row[k] != payload[k] for k in ('account_key', 'platform_message_key', 'direction', 'occurred_lower_utc', 'occurred_upper_utc', 'time_precision')) or
            any(quality.get(k) != 'verified' for k in ('identity', 'body', 'conversation', 'direction', 'time'))):fail()
    reject_hidden(db, row['account_key'], message_key=row['message_key'], observation_key=o['observation_key'], **observation_selectors(payload))
    return {'lineage': lineage, 'observation_key': o['observation_key'], 'semantic_digest': o['semantic_digest']}


def dependent_messages(db, source_id, version, *, device=None, account=None):
    """Find only the fixed derived reference, for the existing withdrawal hook."""
    clauses = ["json_extract(o.payload_json,'$.local_import_derivation.candidate.binding.source_id')=?",
               "json_extract(o.payload_json,'$.local_import_derivation.candidate.binding.source_version')=?"]
    params = [source_id, version]
    if device is not None:clauses.append('o.device_key=?');params.append(device)
    if account is not None:clauses.append('o.account_key=?');params.append(account)
    return db.conn.execute('SELECT DISTINCT m.* FROM message m JOIN message_observation o ON o.message_key=m.message_key WHERE '+
                           ' AND '.join(clauses), params).fetchall()


def materialize(app, token, source_id, source_version, proof_id):
    """Explicit internal host entry. No browser route or default-start activation."""
    from .local_notes import LocalNotesAccess
    from .message_facts import MessageFacts
    notes = getattr(app, 'local_notes', None)
    if not isinstance(notes, LocalNotesAccess) or notes.app is not app:fail()
    with app.db.lock, app.auth.authorized(token, scope='observations:write', account_key=notes.account) as principal:
        notes.check(principal)
        with app.db.transaction() as conn:
            observation = build_observation(app.db, source_id, source_version, proof_id, principal.device_key)
            reference = observation[FIELD]['candidate']
            if (reference['binding'] != notes.binding or
                    any(reference[k] != notes.selection['selection'][k] for k in ('snapshot_sha256', 'preview_sha256'))):fail()
            conversation = stable_key('conversation', notes.account, 'person', observation['platform_conversation_key'])
            key = stable_key('message', notes.account, conversation, observation['platform_message_key'])
            existing = conn.execute('SELECT * FROM message WHERE message_key=?', (key,)).fetchone()
            if existing is not None:
                receipt = check_message(app.db, existing)
                if receipt is None or receipt['lineage'] != observation[FIELD]:fail()
            result = MessageFacts(app.db).ingest(observation, principal)
            if result['status'] not in ('accepted', 'duplicate') or result['message_key'] is None:fail()
            check_message(app.db, conn.execute('SELECT * FROM message WHERE message_key=?', (result['message_key'],)).fetchone())
            notes.check(principal)
            if app.auth.authenticate(token, scope='observations:write', account_key=notes.account) != principal:fail()
        return {**result, 'original_candidate_preserved': True, 'coverage': 'partial', 'model_called': False}
