"""Read an independently reviewed saved binding, never attest a Chrome page.

No registration, schema, activation, message bodies, original observation JSON,
AI or fallback person. A real native producer is a separate prerequisite.
"""
from contextlib import contextmanager
import json
import re

from .common import AppError
from .db import digest, stable_key
from .identity import IdentityService
from .local_deletion import require_ready, reject_hidden, sql_visible

REQUEST_FIELDS = {'source_id', 'source_version', 'owner_uid', 'sec_uid', 'platform_conversation_key'}
RECORD_FIELDS = {'format', 'account_key', 'owner_uid', 'peer_uid', 'peer_sec_uid',
                 'platform_conversation_key', 'conversation_type', 'native_device_key',
                 'observation_key', 'basis'}

def checked_request(value):
    if type(value) is not dict or set(value) != REQUEST_FIELDS:
        raise AppError(400, 'NATIVE_PROFILE_REQUEST_INVALID', '资料核对只接受明确来源及稳定账号、对象、会话标识')
    for field, limit in (('source_id', 200), ('source_version', 80), ('owner_uid', 256),
                         ('sec_uid', 256), ('platform_conversation_key', 256)):
        text = value[field]
        # Transport the native key verbatim; syntax never grants source authority.
        pattern = (r'(?:[A-Za-z0-9_-]+|[0-9]+(?::[0-9]+)+)'
                   if field == 'platform_conversation_key' else r'[A-Za-z0-9_-]+')
        if type(text) is not str or not text or len(text) > limit or not re.fullmatch(pattern, text):
            raise AppError(400, 'NATIVE_PROFILE_REQUEST_INVALID', '资料核对字段无效；不猜测或转换身份')
    if value['sec_uid'] == 'self':
        raise AppError(400, 'NATIVE_PROFILE_REQUEST_INVALID', '本人标记不能作为粉丝身份')
    return dict(value)

class NativeProfileLookup:
    def __init__(self, app):
        self.app = app
        self.db = app.db
        self.identities = IdentityService(self.db)

    def _source(self, source_id, version, account, capabilities):
        source = self.identities.get_source(source_id, version)
        if not source or source['source_kind'] not in ('qualified_adapter', 'fictional'):
            return None
        if source['source_kind'] == 'fictional' and self.app.config['source_mode'] != 'fictional':
            return None
        if not all(self.identities.trusted(source_id, version, name, account) for name in capabilities):
            return None
        return source

    def _douyin_alias(self, account, kind, value):
        # The inherited resolver has no platform argument. Do not let its first
        # matching row select a foreign or ambiguous identity for this seam.
        rows = self.db.conn.execute(
            'SELECT platform FROM identity_alias WHERE account_key=? AND object_kind=? '
            'AND identity_kind=? AND identity_value=? LIMIT 2',
            (account, 'person', kind, value)).fetchall()
        if len(rows) != 1 or rows[0]['platform'] != 'douyin':
            return None
        return self.identities.resolve(account, 'person', kind, value)

    def _chain(self, query, principal, account):
        native = self._source(query['source_id'], query['source_version'], account,
                              ('account_identity', 'person_identity', 'conversation_identity', 'native_profile_binding'))
        if not native:
            return None
        from .native_registration import PREFIX, native_approval_current, _record
        if native['source_id'].startswith(PREFIX) and not native_approval_current(
                self.db, self.app.config['source_mode'], native, account, principal.device_key):
            return None
        caps = native['capabilities']
        if type(caps) is not dict or type(caps.get('account_keys')) is not list or account not in caps['account_keys']:
            return None
        records = native['evidence'].get('native_profile_records') if type(native['evidence']) is dict else None
        if type(records) is not list or not 1 <= len(records) <= 32:
            return None
        matches = [record for record in records if type(record) is dict and
                   record.get('owner_uid') == query['owner_uid'] and record.get('peer_sec_uid') == query['sec_uid'] and
                   record.get('platform_conversation_key') == query['platform_conversation_key']]
        if len(matches) != 1:
            return None
        record = matches[0]
        if not _record(record, account, principal.device_key):
            return None
        from .native_identity_metadata import FORMAT as METADATA_FORMAT, checked_basis
        metadata = record['format'] == METADATA_FORMAT
        if metadata and not native['source_id'].startswith(PREFIX):
            return None
        if self.identities.origin_withdrawn(record['native_device_key'], account, query['source_id'], query['source_version']):
            return None
        own = self.db.conn.execute('SELECT * FROM own_account WHERE account_key=?', (account,)).fetchone()
        if not own or own['platform'] != 'douyin' or own['platform_uid'] != query['owner_uid'] or not own['verified']:
            return None
        own_source = self._source(own['source_id'], own['source_version'], account, ('account_identity',))
        alias = self._douyin_alias(account, 'sec_uid', query['sec_uid'])
        if not own_source or not alias or not alias['verified'] or alias['mapping_state'] != 'verified_direct':
            return None
        alias_source = self._source(alias['source_id'], alias['source_version'], account,
                                    ('person_identity', 'direct_identity_mapping'))
        proof = alias.get('evidence')
        approved = alias_source['evidence'].get('direct_identity_records') if alias_source and type(alias_source['evidence']) is dict else None
        if (type(proof) is not dict or set(proof) != {'account_key', 'uid', 'sec_uid', 'observation_id'} or
            proof['account_key'] != account or proof['uid'] != record['peer_uid'] or proof['sec_uid'] != query['sec_uid'] or
            type(approved) is not list or proof not in approved):
            return None
        person = stable_key('person', account, 'douyin', 'uid', proof['uid'])
        uid_alias = self._douyin_alias(account, 'uid', proof['uid'])
        if (alias['person_key'] != person or not uid_alias or not uid_alias['verified'] or
            uid_alias['person_key'] != person or uid_alias['mapping_state'] != 'verified_direct' or
            uid_alias['source_id'] != alias['source_id'] or uid_alias['source_version'] != alias['source_version'] or
            uid_alias['evidence'] != proof):
            return None
        contact = self.db.conn.execute('SELECT * FROM contact WHERE account_key=? AND person_key=?', (account, person)).fetchone()
        if not contact or contact['object_kind'] != 'person' or not contact['verified']:
            return None
        conversations = self.db.conn.execute('SELECT * FROM conversation WHERE account_key=? AND platform_conversation_key=? LIMIT 2',
                                               (account, query['platform_conversation_key'])).fetchall()
        if metadata:
            if len(conversations) > 1 or any(c['kind'] != 'person' or c['person_key'] != person for c in conversations):
                return None
            basis = checked_basis(self.db, self.app.config['source_mode'], native, record, account, principal.device_key)
            if not basis or any(self.identities.origin_withdrawn(principal.device_key, account, s['source_id'], s['source_version'])
                                for s in (own_source, alias_source)):
                return None
            # This is a derived binding key, not an inserted conversation or
            # accepted chat. Existing contradictory conversation rows deny it.
            conversation = dict(conversations[0]) if conversations else {
                'conversation_key': stable_key('conversation', account, 'person', query['platform_conversation_key']),
                'persistence': 'not_materialized'}
            return {'binding': {'account_key': account, 'person_key': person, 'conversation_key': conversation['conversation_key']},
                    'signature': digest({'native': native, 'own': dict(own), 'own_source': own_source,
                        'alias': alias, 'uid_alias': uid_alias, 'alias_source': alias_source, 'contact': dict(contact),
                        'conversation': conversation, 'metadata_basis': basis})}
        if len(conversations) != 1 or conversations[0]['kind'] != 'person' or conversations[0]['person_key'] != person:
            return None
        conversation = conversations[0]
        visible_message = sql_visible(self.db, 'm', 'message')
        visible_observation = sql_visible(self.db, 'o', 'observation')
        observed = self.db.conn.execute('''SELECT m.message_key,m.person_key,m.conversation_key,m.platform_message_key,
                m.identity_state,m.quality_json AS message_quality,o.observation_key,o.device_key,
                o.source_id,o.source_version,o.quality_json AS observation_quality,o.status
            FROM message m JOIN message_observation o ON o.message_key=m.message_key AND o.account_key=m.account_key
            WHERE m.account_key=? AND m.person_key=? AND m.conversation_key=? AND o.observation_key=?
              AND ''' + visible_message + ' AND ' + visible_observation,
            (account, person, conversation['conversation_key'], record['observation_key'])).fetchone()
        if not observed or observed['identity_state'] != 'exact' or observed['status'] != 'accepted':
            return None
        for name in ('message_quality', 'observation_quality'):
            quality = json.loads(observed[name])
            if quality.get('identity') != 'verified' or quality.get('conversation') != 'verified' or quality.get('message_identity') != 'exact':
                return None
        observed_source = self._source(observed['source_id'], observed['source_version'], account,
                                       ('account_identity', 'person_identity', 'conversation_identity', 'message_identity'))
        if not observed_source or self.identities.origin_withdrawn(observed['device_key'], account, observed['source_id'], observed['source_version']):
            return None
        reject_hidden(self.db, account, message_key=observed['message_key'], observation_key=observed['observation_key'],
                      source_id=observed['source_id'], source_version=observed['source_version'],
                      platform_conversation_key=query['platform_conversation_key'], platform_message_key=observed['platform_message_key'])
        return {'binding': {'account_key': account, 'person_key': person, 'conversation_key': conversation['conversation_key']},
                'signature': digest({'native': native, 'own': dict(own), 'own_source': own_source,
                    'alias': alias, 'uid_alias': uid_alias, 'alias_source': alias_source, 'contact': dict(contact),
                    'conversation': dict(conversation), 'observed': dict(observed), 'observed_source': observed_source})}

    @contextmanager
    def _snapshot(self):
        with self.db.lock:
            require_ready(self.db)
            if self.db.conn.in_transaction:
                raise AppError(409, 'NATIVE_PROFILE_READ_BUSY', '资料核对已有事务；不重试或改变原事务')
            self.db.conn.execute('BEGIN')
            try:
                yield
            finally:
                self.db.conn.rollback()

    def _fence(self):
        with self.db.lock:
            return (self.db.conn.total_changes, self.db.conn.execute('PRAGMA data_version').fetchone()[0])

    def lookup(self, value, principal, reauthorize):
        query = checked_request(value)
        account = stable_key('account', 'douyin', query['owner_uid'])
        principal.require('sidebar:read', account)  # Before even reading owner/source rows.
        def authorize():
            if reauthorize(principal, account) != principal:
                raise AppError(403, 'NATIVE_PROFILE_AUTH_CHANGED', '本机授权已变化，不返回旧资料')
        unknown = {'format': 'native-profile-lookup-1', 'state': 'unknown', 'reason': 'binding_not_qualified',
                   'request_digest': digest(query), 'binding': None, 'card': None,
                   'native_page_verification': 'background_required', 'historical_device_provenance': 'not_assessed',
                   'message_history': 'not_loaded', 'AI_reference': 'not_requested'}
        authorize()
        with self._snapshot():
            fence = self._fence()
            chain = self._chain(query, principal, account)
            projection = getattr(self.app, 'projections', None)
            card = projection.native_profile_card(account, chain['binding']['person_key']) if chain and projection else None
        authorize()
        if not chain or card is None:
            return unknown
        with self._snapshot():
            current = self._chain(query, principal, account)
            unchanged = current and current['signature'] == chain['signature'] and self._fence() == fence
        authorize()
        if not unchanged or self._fence() != fence:
            return dict(unknown, reason='binding_changed')
        return dict(unknown, state='saved_binding_ready', reason=None, binding=chain['binding'], card=card,
                    authority_scope='current_independent_approval')
