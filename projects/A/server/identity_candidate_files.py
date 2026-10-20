"""Explicit plaintext pending indexes; never a trusted identity store.

An externally pinned file digest identifies an exact snapshot. It cannot prove
that snapshot is the latest one. No business DB, HTTP or sync hook exists here.
"""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from .backup_scratch import reject_links
from .common import AppError, canonical_json
from .export_candidates import _Lease, CandidateError
from .identity_candidates import IdentityCandidateLedger

FILE_FORMAT = 'dfa-pending-identity-json-1'
MAX_CLEAR_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = MAX_CLEAR_BYTES
MAX_LINE_BYTES = 16 * 1024


def fail(code):
    raise AppError(422, code, '身份候选文件无法通过有限核对；未提交正式身份。')


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                fail('IDENTITY_FILE_JSON_INVALID')
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=pairs,
                           parse_constant=lambda _: fail('IDENTITY_FILE_JSON_INVALID'))
    except (ValueError, UnicodeError, RecursionError):
        fail('IDENTITY_FILE_JSON_INVALID')
    # JSON nesting varies with the interpreter's recursion limit. The file
    # format requires at most a few metadata layers; make its bound explicit.
    stack = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > 16:
            fail('IDENTITY_FILE_JSON_INVALID')
        if type(current) is dict:
            stack.extend((child, depth + 1) for child in current.values())
        elif type(current) is list:
            stack.extend((child, depth + 1) for child in current)
    return value


def checked_path(value, *, output=False):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        fail('IDENTITY_FILE_PATH_INVALID')
    reject_links(path)
    if output:
        if not path.parent.is_dir() or not path.name.endswith('.identity.json'):
            fail('IDENTITY_FILE_PATH_INVALID')
        if path.exists():
            fail('IDENTITY_FILE_EXISTS')
    else:
        if not path.is_file() or path.stat().st_nlink != 1:
            fail('IDENTITY_FILE_PATH_INVALID')
    return path


def pinned_bytes(path, expected_sha256, *, maximum=MAX_FILE_BYTES):
    if type(expected_sha256) is not str or not re.fullmatch('[0-9a-f]{64}', expected_sha256):
        fail('IDENTITY_FILE_DIGEST_REQUIRED')
    path = checked_path(path)
    # Read through the same locked Windows handle. The standard snapshot reader
    # denies WRITE and DELETE and rejects directories, reparse points/hardlinks.
    from tools.large_export_snapshot import LockedRead, SnapshotError
    try:
        with LockedRead(path) as reader:
            if reader.initial['bytes'] > maximum:
                fail('IDENTITY_FILE_LIMIT')
            raw = b''.join(bytes(block) for block in reader.blocks())
            if hashlib.sha256(raw).hexdigest() != expected_sha256:
                fail('IDENTITY_FILE_DIGEST_MISMATCH')
            return raw
    except SnapshotError:
        fail('IDENTITY_FILE_READ_FAILED')


def append_jsonl(ledger, path, expected_sha256):
    raw = pinned_bytes(path, expected_sha256, maximum=MAX_CLEAR_BYTES)
    state = ledger.export_state()
    private_ledger = IdentityCandidateLedger.from_state(state)
    batch_size = min(1000, state['limits']['max_batch'])
    # Input is metadata only. Reject body/name/verified and any extra field.
    # Build on a private copy so a late invalid row cannot change the caller's
    # ledger or publish any partially appended snapshot.
    batch = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        if len(line) > MAX_LINE_BYTES:
            fail('IDENTITY_FILE_LINE_LIMIT')
        batch.append(strict_json(line))
        if len(batch) == batch_size:
            private_ledger.append_batch(batch)
            batch = []
    if batch:
        private_ledger.append_batch(batch)
    return private_ledger


def load_index(path, expected_sha256):
    raw = pinned_bytes(path, expected_sha256)
    if len(raw) > MAX_CLEAR_BYTES:
        fail('IDENTITY_FILE_LIMIT')
    state = strict_json(raw)
    if type(state) is not dict or set(state) != {'format', 'ledger'} or state['format'] != FILE_FORMAT:
        fail('IDENTITY_FILE_FORMAT_INVALID')
    return IdentityCandidateLedger.from_state(state['ledger'])


def save_new_index(ledger, path):
    if os.name != 'nt':
        fail('IDENTITY_FILE_WINDOWS_REQUIRED')
    path = checked_path(path, output=True)
    clear = canonical_json({'format': FILE_FORMAT, 'ledger': ledger.export_state()})
    if len(clear) > MAX_CLEAR_BYTES:
        fail('IDENTITY_FILE_LIMIT')
    # The user requested efficiency-first development without new encryption.
    # This local UTF-8 metadata file is deliberately plaintext, including stage.
    blob = clear
    if len(blob) > MAX_FILE_BYTES:
        fail('IDENTITY_FILE_LIMIT')
    stage = path.parent / ('.identity-json-' + uuid.uuid4().hex + '.tmp')
    owned_identity = None
    published = False
    try:
        with ExitStack() as stack:
            stack.enter_context(_Lease(path.parent, directory=True))
            checked_path(path, output=True)
            with stage.open('xb') as stream:
                st = os.fstat(stream.fileno())
                if st.st_nlink != 1:
                    fail('IDENTITY_FILE_PATH_INVALID')
                owned_identity = (st.st_dev, st.st_ino)
                stream.write(blob)
                stream.flush()
                os.fsync(stream.fileno())
            reject_links(stage)
            st = stage.stat()
            if (st.st_dev, st.st_ino) != owned_identity or st.st_nlink != 1:
                fail('IDENTITY_FILE_CHANGED')
            if pinned_bytes(stage, hashlib.sha256(blob).hexdigest()) != blob:
                fail('IDENTITY_FILE_CHANGED')
            checked_path(path, output=True)
            # Windows rename fails if destination exists. No ReplaceFile or
            # POSIX overwrite semantics are permitted by the Windows gate above.
            os.rename(stage, path)
            published = True
            if pinned_bytes(path, hashlib.sha256(blob).hexdigest()) != blob:
                fail('IDENTITY_FILE_CHANGED')
    except Exception as error:
        if published:
            # Publication can precede a failed verification/receipt. Do not tell
            # the caller that rejection means no new candidate file exists.
            result = AppError(503, 'IDENTITY_FILE_PUBLISHED_UNVERIFIED',
                              '候选文件可能已发布；需用原摘要检查，不能作为正式身份。')
            result.publication_state = 'published_unverified'
            raise result from None
        if isinstance(error, CandidateError):
            fail('IDENTITY_FILE_LEASE_FAILED')
        raise
    finally:
        if owned_identity is not None and stage.exists():
            reject_links(stage)
            st = stage.stat()
            if (st.st_dev, st.st_ino) == owned_identity and st.st_nlink == 1:
                stage.unlink()
    return {
        'format': FILE_FORMAT, 'file_bytes': len(blob),
        'file_sha256': hashlib.sha256(blob).hexdigest(),
        'state_sha256': hashlib.sha256(clear).hexdigest(),
        'encrypted': False,
        'requires_same_windows_user': False,
        'publication_state': 'published_verified',
        'latest_head_verified': False,
        'business_writes': 0, 'identity_merges': 0,
        'summary': ledger.summary(),
    }
