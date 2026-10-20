"""Bounded, account-scoped necessary-deletion manifests. Read only; no purge.

This module has no filesystem, transport, schema installation or background
work. A manifest inventories explicit identifiers, never guesses from body text.
"""
import hashlib
import json

from .auth import Principal
from .common import AppError
from .db import digest
from .identity import require_text


class DeletionManifestService:
    MAX_IDENTIFIERS = 5000
    MAX_SCAN_ROWS = 50000
    MAX_SCAN_BYTES = 64 * 1024 * 1024
    MAX_CLOSURE_PASSES = 64
    FILE_PROTOCOL = 'file-import-candidate-v1'

    def __init__(self, db):
        self.db = db

    @staticmethod
    def _row_digest(row):
        return digest({key: {'blob_sha256': hashlib.sha256(value).hexdigest(),
                             'blob_bytes': len(value)} if isinstance(value, bytes) else value
                       for key, value in dict(row).items()})

    @staticmethod
    def _json(value):
        if not isinstance(value, str):
            return None
        try:
            result = json.loads(value, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, RecursionError):
            return None
        return result if isinstance(result, dict) else None

    def _file_alias(self, raw):
        candidate = self._json(raw.get('export_candidate_json')) if isinstance(raw, dict) else None
        if not candidate or candidate.get('import_protocol') != self.FILE_PROTOCOL:
            return None
        namespace, key = candidate.get('account_namespace'), candidate.get('source_key')
        if (not isinstance(namespace, str) or not 1 <= len(namespace) <= 256 or '\x00' in namespace
            or not isinstance(key, str) or not 1 <= len(key) <= 2048 or '\x00' in key):
            return None
        source, version = raw.get('source_id'), raw.get('source_version')
        if (not isinstance(source, str) or not 1 <= len(source) <= 200 or '\x00' in source
            or not isinstance(version, str) or not 1 <= len(version) <= 80 or '\x00' in version):
            return None
        return namespace, key, source, version

    @staticmethod
    def _references(value):
        """Only typed explicit IDs; quote/body substrings confer no association."""
        result = set()
        stack = [(value, 0)]
        nodes = 0
        while stack:
            current, depth = stack.pop()
            nodes += 1
            if nodes > 200000 or depth > 40:
                raise AppError(413, 'DELETION_MANIFEST_LIMIT', 'Reference structure exceeds the finite budget.')
            if isinstance(current, dict):
                for key, child in current.items():
                    if key in ('message_key', 'observation_key', 'job_key', 'action_key') and isinstance(child, str):
                        result.add((key, child))
                    elif key not in ('body', 'quote', 'export_candidate_json'):
                        stack.append((child, depth + 1))
            elif isinstance(current, list):
                stack.extend((child, depth + 1) for child in current)
        return result

    def plan(self, account_key, target_kind, target_key, principal):
        require_text(account_key, 'account_key', 200)
        require_text(target_key, 'target_key', 200)
        if target_kind not in ('message', 'observation'):
            raise AppError(422, 'DELETION_TARGET_KIND_INVALID', 'Select a message or observation identifier.')
        if not isinstance(principal, Principal) or principal.role != 'manager':
            raise AppError(403, 'DELETION_MANAGER_REQUIRED', 'An enrolled local manager is required.')
        principal.require('deletion:plan', account_key)
        with self.db.lock:
            conn = self.db.conn
            owns_snapshot = not conn.in_transaction
            if owns_snapshot:
                conn.execute('BEGIN')
            try:
                result = self._plan(conn, account_key, target_kind, target_key, principal)
                if owns_snapshot:
                    conn.execute('COMMIT')
                return result
            except BaseException:
                if owns_snapshot:
                    conn.execute('ROLLBACK')
                raise

    def _plan(self, conn, account, kind, key, principal):
        meta = dict(conn.execute('SELECT key,value FROM app_meta'))
        if principal.device_key != meta.get('device_key'):
            raise AppError(403, 'DELETION_LOCAL_DEVICE_REQUIRED', 'Plan must originate on the enrolled local device.')
        if not conn.execute('SELECT 1 FROM own_account WHERE account_key=?', (account,)).fetchone():
            raise AppError(404, 'DELETION_ACCOUNT_UNKNOWN', 'The selected account is not registered.')
        schema_rows = [dict(row) for row in conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name")]
        tables = {row['name'] for row in schema_rows if row['type'] == 'table'}
        scan_rows = 0
        scan_bytes = 0

        def read(table, where='', params=()):
            nonlocal scan_rows, scan_bytes
            if table not in tables:
                return []
            remaining = self.MAX_SCAN_ROWS - scan_rows
            sql = 'SELECT * FROM "' + table + '"' + (' WHERE ' + where if where else '') + ' LIMIT ?'
            rows = []
            for record in conn.execute(sql, (*params, remaining + 1)):
                row = dict(record)
                scan_rows += 1
                for value in row.values():
                    scan_bytes += len(value) if isinstance(value, bytes) else len(value.encode('utf-8')) if isinstance(value, str) else 16
                if scan_rows > self.MAX_SCAN_ROWS or scan_bytes > self.MAX_SCAN_BYTES:
                    raise AppError(413, 'DELETION_MANIFEST_LIMIT', 'Inventory scan exceeds the finite budget; no partial plan is returned.')
                rows.append(row)
            return rows

        def read_ids(table, field, identifiers):
            rows = []
            values = sorted(identifiers)
            for offset in range(0, len(values), 400):
                block = values[offset:offset + 400]
                rows.extend(read(table, field + ' IN (' + ','.join('?' for _ in block) + ')', block))
            return rows

        messages = {row['message_key']: row for row in read('message', 'account_key=?', (account,))}
        observations = {row['observation_key']: row for row in read('message_observation', 'account_key=?', (account,))}
        if key not in (messages if kind == 'message' else observations):
            raise AppError(404, 'DELETION_TARGET_UNKNOWN', 'No target exists in the selected account.')
        raws = {obs: self._json(row['payload_json']) for obs, row in observations.items()}
        bindings = {row['account_namespace']: row for row in read('file_import_binding')}
        selected_messages = {key} if kind == 'message' else set()
        selected_observations = {key} if kind == 'observation' else set()
        selected_jobs = set()
        selected_actions = set()
        file_aliases = set()
        platform_aliases = set()
        jobs = {row['job_key']: row for row in read('ai_job', 'account_key=?', (account,))}
        event_rows = []
        for row in read('domain_event', 'account_key=?', (account,)):
            event_rows.append(('domain_event', row, self._json(row['event_json'])))
        # Receipt account belongs to the signed event JSON, not a separate column.
        for row in read('sync_received', "json_extract(event_json,'$.account_key')=?", (account,)):
            event_rows.append(('sync_received', row, self._json(row['event_json'])))
        event_selection = set()
        synthetic_observation_targets = {}
        related = {}
        conversations = {row['conversation_key']: row for row in read('conversation', 'account_key=?', (account,))}
        links = read('send_link', 'message_key IN (SELECT message_key FROM message WHERE account_key=?)', (account,))
        actions = {row['action_key']: row for row in read('manual_action', 'account_key=?', (account,))}

        def ids():
            return {('message', value) for value in selected_messages} | {('observation', value) for value in selected_observations} | {
                ('ai_job', value) for value in selected_jobs} | {('action', value) for value in selected_actions} | {
                ('event', event_rows[index][1]['event_id']) for index in event_selection} | {
                ('observation', value) for value in synthetic_observation_targets}

        def check_identifiers(extra=()):
            if len(ids() | set(extra)) > self.MAX_IDENTIFIERS:
                raise AppError(413, 'DELETION_MANIFEST_LIMIT', 'Identifier closure exceeds 5000; no truncated plan is returned.')

        def raw_matches(raw):
            if not isinstance(raw, dict) or raw.get('account_key') != account:
                return False
            return self._file_alias(raw) in file_aliases or (raw.get('platform_conversation_key'), raw.get('platform_message_key')) in platform_aliases

        for _ in range(self.MAX_CLOSURE_PASSES):
            before = (frozenset(ids()), frozenset(file_aliases), frozenset(platform_aliases), frozenset(event_selection))
            for message in list(selected_messages):
                row = messages.get(message)
                if not row:
                    continue
                conversation = conversations.get(row['conversation_key'])
                if row['identity_state'] == 'exact' and row['platform_message_key'] and conversation:
                    platform_aliases.add((conversation['platform_conversation_key'], row['platform_message_key']))
            for observation in list(selected_observations):
                row = observations.get(observation)
                if not row:
                    continue
                if row['message_key'] in messages:
                    selected_messages.add(row['message_key'])
                alias = self._file_alias(raws[observation])
                if alias:
                    binding = bindings.get(alias[0])
                    if ((raws[observation].get('account_key'), alias[2], alias[3]) !=
                            (account, row['source_id'], row['source_version']) or
                        binding and (binding['account_key'], binding['source_id'], binding['source_version']) !=
                            (account, alias[2], alias[3]) or
                        not conn.execute('SELECT 1 FROM source WHERE source_id=? AND source_version=?', alias[2:]).fetchone()):
                        raise AppError(409, 'DELETION_FILE_BINDING_CONFLICT',
                                       'Selected file observation does not match its registered source and namespace binding.')
                    file_aliases.add(alias)
            for observation, row in observations.items():
                if row['message_key'] in selected_messages or raw_matches(raws[observation]):
                    selected_observations.add(observation)
            for job, row in jobs.items():
                refs = self._references(self._json(row['context_json']))
                if any(ref_kind == 'message_key' and value in selected_messages or
                       ref_kind == 'observation_key' and value in selected_observations
                       for ref_kind, value in refs):
                    selected_jobs.add(job)
            for row in links:
                if row['message_key'] in selected_messages and row['action_key'] in actions:
                    selected_actions.add(row['action_key'])
            selected_refs = {('message_key', value) for value in selected_messages} | {
                ('observation_key', value) for value in selected_observations} | {
                ('observation_key', value) for value in synthetic_observation_targets} | {
                ('job_key', value) for value in selected_jobs} | {('action_key', value) for value in selected_actions}
            for index, (_, row, event) in enumerate(event_rows):
                if not event or event.get('account_key') != account:
                    continue
                payload = event.get('payload')
                if not isinstance(payload, dict):
                    continue
                refs = self._references(payload)
                matched = (event.get('target_key') in selected_messages | selected_observations | selected_jobs | selected_actions
                           or bool(refs & selected_refs) or raw_matches(payload.get('observation')))
                if not matched:
                    continue
                event_selection.add(index)
                if event.get('event_type') in ('ai.request', 'ai.result', 'ai.control'):
                    job = payload.get('job_key')
                    if isinstance(job, str) and job:
                        selected_jobs.add(job)
                if event.get('event_type') in ('message.observed', 'observation.pending') and isinstance(payload.get('observation'), dict):
                    raw = payload['observation']
                    alias = self._file_alias(raw)
                    binding = bindings.get(alias[0]) if alias else None
                    if (alias and raw.get('account_key') == account and
                        (not binding or (binding['account_key'], binding['source_id'], binding['source_version']) ==
                         (account, alias[2], alias[3])) and
                        conn.execute('SELECT 1 FROM source WHERE source_id=? AND source_version=?', alias[2:]).fetchone()):
                        file_aliases.add(alias)
                    observation = payload.get('observation_key') or event.get('target_key')
                    if isinstance(observation, str) and observation not in observations:
                        synthetic_observation_targets[observation] = row['event_digest']
            check_identifiers()
            after = (frozenset(ids()), frozenset(file_aliases), frozenset(platform_aliases), frozenset(event_selection))
            if after == before:
                break
        else:
            raise AppError(413, 'DELETION_MANIFEST_LIMIT', 'Closure passes exceed the finite budget.')

        selected = {}
        selected['message'] = [messages[value] for value in sorted(selected_messages) if value in messages]
        selected['message_observation'] = [observations[value] for value in sorted(selected_observations)]
        selected['ai_job'] = [jobs[value] for value in sorted(selected_jobs) if value in jobs]
        selected['manual_action'] = [actions[value] for value in sorted(selected_actions)]

        # SQL subqueries keep all joins account scoped; optional feature tables
        # are inventories, never implicitly initialized by this read.
        selected['message_revision'] = [row for row in read('message_revision',
            'message_key IN (SELECT message_key FROM message WHERE account_key=?)', (account,))
            if row['message_key'] in selected_messages]
        selected['send_link'] = [row for row in links if row['message_key'] in selected_messages]
        for table in ('history_gram', 'history_watermark'):
            selected[table] = [row for row in read(table,
                'message_key IN (SELECT message_key FROM message WHERE account_key=?)', (account,))
                if row['message_key'] in selected_messages]
        selected['ai_control'] = [row for row in read('ai_control',
            'job_key IN (SELECT job_key FROM ai_job WHERE account_key=?)', (account,)) if row['job_key'] in selected_jobs]
        selected['file_import_item'] = [row for row in read('file_import_item',
            'batch_id IN (SELECT batch_id FROM file_import_batch WHERE account_key=?)', (account,))
            if row['observation_key'] in selected_observations]
        batch_ids = {row['batch_id'] for row in selected['file_import_item']}
        for observation in selected_observations:
            candidate = self._json((raws[observation] or {}).get('export_candidate_json'))
            if candidate and isinstance(candidate.get('batch_id'), str):
                batch_ids.add(candidate['batch_id'])
        selected['file_import_batch'] = [row for row in read('file_import_batch', 'account_key=?', (account,))
                                         if row['batch_id'] in batch_ids]
        selected['file_import_binding'] = [row for row in bindings.values() if row['account_key'] == account
                                           if row['account_namespace'] in {alias[0] for alias in file_aliases}]
        selected['deletion_tombstone'] = [row for row in read('deletion_tombstone')
            if row['target_key'] in selected_messages | selected_observations]
        event_ids = {event_rows[index][1]['event_id'] for index in event_selection}
        selected['domain_event'] = [row for storage, row, _ in (event_rows[index] for index in sorted(event_selection)) if storage == 'domain_event']
        selected['sync_received'] = [row for storage, row, _ in (event_rows[index] for index in sorted(event_selection)) if storage == 'sync_received']
        selected['outbox'] = [row for row in read('outbox',
            'event_id IN (SELECT event_id FROM domain_event WHERE account_key=?)', (account,)) if row['event_id'] in event_ids]

        packages = {}
        for member_table, package_table in (('sync_publication_event', 'sync_publication'), ('sync_package_event', 'sync_package')):
            matching_memberships = read_ids(member_table, 'event_id', event_ids)
            package_ids = {row['package_id'] for row in matching_memberships}
            memberships = read_ids(member_table, 'package_id', package_ids)
            selected[member_table] = memberships
            selected[package_table] = read_ids(package_table, 'package_id', package_ids)
            packages[package_table] = [{'package_id': row['package_id'],
                'related_events': sum(member['event_id'] in event_ids for member in memberships if member['package_id'] == row['package_id']),
                'total_events': sum(1 for member in memberships if member['package_id'] == row['package_id']),
                'classification': 'ledger_membership_only', 'content_inspected': False,
                'version_digest': self._row_digest(row)} for row in selected[package_table]]

        people = {row['person_key'] for row in selected['message']}
        for observation in selected_observations:
            raw = raws[observation] or {}
            if all(isinstance(raw.get(field), str) for field in ('object_kind', 'identity_kind', 'identity_value')):
                alias = conn.execute('SELECT person_key FROM identity_alias WHERE account_key=? AND object_kind=? AND identity_kind=? AND identity_value=?',
                                     (account, raw['object_kind'], raw['identity_kind'], raw['identity_value'])).fetchone()
                if alias:
                    people.add(alias[0])
        profiles = [row for row in read('causal_revision', 'account_key=?', (account,)) if row['target_key'] in people
            and (row['field'] in ('note', 'birthday', 'anniversary') or row['field'].startswith(('free_term.', 'entitlement.')))]
        legacy = read('legacy_record')
        source_keys = {(row['source_id'], row['source_version']) for row in selected['message_observation']}
        selected['source'] = [row for row in read('source') if (row['source_id'], row['source_version']) in source_keys]
        selected['own_account'] = read('own_account', 'account_key=?', (account,))
        selected['contact'] = [row for row in read('contact', 'account_key=?', (account,)) if row['person_key'] in people]
        selected['identity_alias'] = [row for row in read('identity_alias', 'account_key=?', (account,)) if row['person_key'] in people]
        selected['conversation'] = [row for row in conversations.values() if row['conversation_key'] in {msg['conversation_key'] for msg in selected['message']}]
        selected['profile_unresolved'] = profiles
        selected['legacy_unresolved'] = legacy
        extra = {('revision', row['message_key'] + ':' + str(row['revision'])) for row in selected['message_revision']} | {
            ('batch', value) for value in batch_ids} | {('package', row['package_id']) for table in packages for row in selected[table]} | {
            ('profile_review', row['event_id']) for row in profiles} | {('legacy_review', row['legacy_key']) for row in legacy}
        check_identifiers(extra)

        targets = [{'account_key': account, 'target_kind': 'message', 'target_key': row['message_key'],
                    'version_digest': self._row_digest(row)} for row in selected['message']] + [
            {'account_key': account, 'target_kind': 'observation', 'target_key': row['observation_key'],
             'version_digest': self._row_digest(row)} for row in selected['message_observation']] + [
            {'account_key': account, 'target_kind': 'observation', 'target_key': target,
             'version_digest': version} for target, version in sorted(synthetic_observation_targets.items())]
        alias_selectors = [{'kind': 'platform_message', 'account_key': account,
            'platform_conversation_key': conv, 'platform_message_key': message} for conv, message in sorted(platform_aliases)] + [
            {'kind': 'file_record', 'account_key': account, 'account_namespace': namespace, 'source_key': source_key,
             'source_id': source, 'source_version': version}
            for namespace, source_key, source, version in sorted(file_aliases)]
        event_refs = []
        for index in sorted(event_selection):
            storage, row, event = event_rows[index]
            event_refs.append({name: row.get(name, event.get(name)) for name in
                ('event_id', 'event_digest', 'device_key', 'epoch', 'channel', 'seq', 'target_key', 'event_type')} |
                {'account_key': account, 'storage': storage})
        observation_versions = []
        for row in selected['message_observation']:
            body = (raws[row['observation_key']] or {}).get('body')
            observation_versions.append({name: row[name] for name in
                ('observation_key', 'source_id', 'source_version', 'observation_id', 'message_key', 'device_key', 'semantic_digest')} |
                {'body_sha256': hashlib.sha256(body.encode('utf-8')).hexdigest() if isinstance(body, str) else None,
                 'version_digest': self._row_digest(row)})
        row_hashes = {table: sorted(self._row_digest(row) for row in rows) for table, rows in sorted(selected.items())}
        metadata = {'schema_user_version': conn.execute('PRAGMA user_version').fetchone()[0],
                    'schema_digest': digest(schema_rows), 'data_version': meta.get('data_version'),
                    'device_key': meta.get('device_key'), 'epoch': meta.get('epoch'),
                    'snapshot_digest': digest({'rows': row_hashes, 'app_meta': digest(meta)})}
        counts = {table: len(rows) for table, rows in selected.items()}
        unresolved = ['encrypted_packages_and_journal_need_authenticated_record_scan',
            'peer_business_copy_and_independent_deletion_receipt', 'external_deletion_log_and_restore_gate',
            'sqlite_wal_free_pages_and_external_copies', 'mixed_package_survivors_must_be_preserved',
            'manual_profile_and_legacy_excerpts_require_explicit_review']
        related = {'manual_action_keys': sorted(selected_actions), 'file_import_batch_ids': sorted(batch_ids),
            'outbox_event_ids': sorted(row['event_id'] for row in selected['outbox']),
            'publication_packages': packages['sync_publication'], 'received_packages': packages['sync_package'],
            'existing_tombstones': [{name: row[name] for name in ('target_key', 'deletion_state', 'created_at_utc')}
                                    for row in selected['deletion_tombstone']]}
        revisions = [{name: row[name] for name in ('message_key', 'revision', 'body_sha256', 'semantic_digest', 'observation_key')}
                     for row in selected['message_revision']]
        result = {'format': 'dfa-deletion-manifest-1', 'account_key': account, 'target_kind': kind,
            'target_key': key, 'targets': sorted(targets, key=lambda row: (row['target_kind'], row['target_key'])),
            'alias_selectors': alias_selectors, 'event_refs': sorted(event_refs, key=lambda row: (row['event_id'], row['storage'])),
            'ai_job_keys': sorted(selected_jobs), 'revisions': sorted(revisions, key=lambda row: (row['message_key'], row['revision'])),
            'observation_versions': observation_versions, 'related_ids': related, 'counts': counts,
            'review_counts': {'unselected_pending_observations': sum(row['message_key'] is None and obs not in selected_observations for obs, row in observations.items()),
                             'profile_excerpt_revisions': len(profiles), 'legacy_records': len(legacy)},
            'copy_classes': {'business_rows': 'plaintext_inventory', 'event_payloads': 'immutable_payload_retained',
                             'package_wires': 'encrypted_membership_inventory_not_rescanned',
                             'journal_backup_staging_transport': 'not_inspected', 'peer_external_copies': 'uncontrolled'},
            'metadata': metadata, 'identifier_count': len(ids() | extra), 'unresolved': unresolved,
            'mode': 'read_only_manifest', 'execution_available': False, 'copies_state': 'copies_pending',
            'physical_purged': False, 'complete_inventory': False, 'body_exported': False, 'deleted': False}
        result['plan_hash'] = digest({'manifest': result, 'principal': principal.as_dict()})
        return result
