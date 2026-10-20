"""Unwired byte loader for separately reviewed guarded recovery contracts.

The caller must supply independently reviewed digests. This module does not
issue approval, discover files, establish a connection, or prove an external
event happened. A digest read from the same untrusted packet is not a trust
anchor. Production startup must still supply real review/connection evidence.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import re

from .contract import decode
from .security import CollectorError

FORMAT = 'b-gap-reviewed-contract/1'
EVIDENCE_FORMAT = 'b-gap-source-change/1'
MAX_PACKET = 256 * 1024
MAX_EVIDENCE = 8192


def _reject():
    raise CollectorError('GUARDED_REVIEW_CONTRACT_INVALID')


def _keys(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        _reject()


def _hex(value, length=64):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{%d}' % length, value) is None:
        _reject()
    return value


def _count(value):
    if type(value) is not int or not 0 <= value < 2 ** 63:
        _reject()


def _time(value):
    if not isinstance(value, str) or len(value) > 40:
        _reject()
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() is None:
            _reject()
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        _reject()


def _packet(data, expected, limit):
    _hex(expected)
    if type(data) is not bytes or not 0 < len(data) <= limit:
        _reject()
    if hashlib.sha256(data).hexdigest() != expected:
        raise CollectorError('GUARDED_REVIEW_DIGEST_CHANGED')
    try:
        return decode(data)
    except Exception:
        # Never echo packet content or arbitrary exception text.
        _reject()


def _checkpoint(value):
    _keys(value, {'run_id', 'generation', 'state', 'records', 'meta_sha256',
                  'targets', 'operations', 'observations'})
    _hex(value['run_id'], 32); _hex(value['generation'], 32)
    _hex(value['meta_sha256']); _count(value['records'])
    if value['state'] not in {'disconnected', 'failed'}:
        _reject()
    for name in ('targets', 'operations', 'observations'):
        _keys(value[name], {'count', 'sha256'})
        _count(value[name]['count']); _hex(value[name]['sha256'])
    if value['observations']['count'] != value['records']:
        _reject()


def _validate_inner(data, digest, evidence, evidence_digest, purpose, clock):
    # Local imports avoid circular dependency through the source manifest.
    from .gaps import CODE_HASH
    from .guarded_gap_prepare import CANDIDATE_FILES, FORMAT as PREPARE_FORMAT
    from .guarded_gap_bridge import CODE_FILES, FORMAT as OPEN_FORMAT

    if purpose not in {'prepare', 'execute'}:
        _reject()
    package = _packet(data, digest, MAX_PACKET)
    source = _packet(evidence, evidence_digest, MAX_EVIDENCE)
    _keys(package, {'format', 'purpose', 'review_id', 'reviewed_at', 'valid_until',
                    'stopped_at', 'absolute_deadline', 'external_change_evidence_sha256', 'contract'})
    _keys(source, {'format', 'evidence_id', 'run_id', 'observed_at', 'kind', 'basis'})
    if package['format'] != FORMAT or package['purpose'] != purpose or source['format'] != EVIDENCE_FORMAT:
        _reject()
    _hex(package['review_id'], 32); _hex(source['evidence_id'], 32)
    _hex(source['run_id'], 32)
    if package['external_change_evidence_sha256'] != evidence_digest:
        _reject()
    allowed = {('user_browser_reopened', 'user_confirmation'),
               ('user_debug_connection_allowed', 'user_confirmation'),
               ('official_connection_reestablished', 'official_component_observation')}
    if (source['kind'], source['basis']) not in allowed:
        _reject()
    stopped, observed, reviewed, until, deadline = map(_time, (
        package['stopped_at'], source['observed_at'], package['reviewed_at'],
        package['valid_until'], package['absolute_deadline']))
    current = clock()
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        _reject()
    if not stopped < observed <= reviewed <= current < until <= deadline:
        raise CollectorError('GUARDED_REVIEW_TIME_INVALID')

    contract = package['contract']
    if purpose == 'prepare':
        _keys(contract, {'format', 'approval_id', 'approved_to_prepare', 'checkpoint', 'code_sha256', 'frozen_code_hash'})
        if (contract['format'] != PREPARE_FORMAT or contract['approved_to_prepare'] is not True
                or contract['frozen_code_hash'] != CODE_HASH):
            _reject()
        files = CANDIDATE_FILES
    else:
        _keys(contract, {'format', 'approved_to_execute', 'approval_id', 'preparation_id',
                         'preparation_sha256', 'checkpoint', 'code_sha256', 'external_change_evidence_sha256'})
        if (contract['format'] != OPEN_FORMAT or contract['approved_to_execute'] is not True
                or contract['external_change_evidence_sha256'] != evidence_digest):
            _reject()
        _hex(contract['preparation_id'], 32); _hex(contract['preparation_sha256'])
        files = CODE_FILES
    _hex(contract['approval_id'], 32)
    if contract['approval_id'] != package['review_id']:
        _reject()
    _checkpoint(contract['checkpoint'])
    if source['run_id'] != contract['checkpoint']['run_id']:
        _reject()
    _keys(contract['code_sha256'], files)
    for value in contract['code_sha256'].values():
        _hex(value)
    return deepcopy(contract)


def _validate(*args):
    try:
        return _validate_inner(*args)
    except CollectorError:
        raise
    except (TypeError, KeyError, ValueError, OverflowError, RecursionError):
        _reject()


class ReviewedGapContract:
    """Retains exact reviewed bytes; rechecks time/digests at mutation checks.

    Only use instances returned by load_reviewed_contract. Existing low-level
    test APIs are not made trustworthy simply by passing a dict to them.
    """
    def __init__(self, data, digest, evidence, evidence_digest, purpose, clock):
        self._inputs = (data, digest, evidence, evidence_digest, purpose, clock)
        self._purpose = purpose
        self._validate()

    def _validate(self):
        return _validate(*self._inputs)

    def _verifier(self, verify_code):
        def verify():
            contract = self._validate()
            hashes = verify_code()
            # A callback may block; check validity again after it returns.
            self._validate()
            if hashes != contract['code_sha256']:
                raise CollectorError('VERSION_MISMATCH')
            return hashes
        return verify

    def _review_validator(self, store):
        def validate(meta):
            self._validate()
            if not store.db.in_transaction:
                raise CollectorError('GUARDED_TRANSACTION_REQUIRED')
            package = decode(self._inputs[0])
            # The host supplies original metadata from the exact checkpoint,
            # both before writes and at the end of the same transaction.
            if (_time(meta.get('finished_at')) != _time(package['stopped_at'])
                    or _time(meta.get('expires_at')) != _time(package['absolute_deadline'])):
                raise CollectorError('GUARDED_REVIEW_STOP_BINDING')
        return validate

    def prepare(self, store, lock, *, verify_code):
        from .guarded_gap_prepare import prepare_reviewed_stop
        if self._purpose != 'prepare':
            _reject()
        return prepare_reviewed_stop(store, lock, self._validate(), verify_code=self._verifier(verify_code),
                                     validate_review=self._review_validator(store))

    def bridge(self, store, lock, *, verify_code):
        from .guarded_gap_bridge import GuardedGapBridge
        if self._purpose != 'execute':
            _reject()
        return GuardedGapBridge(store, lock, self._validate(), verify_code=self._verifier(verify_code),
                                 validate_review=self._review_validator(store))


def load_reviewed_contract(data, *, reviewed_sha256, external_evidence,
                           reviewed_evidence_sha256, purpose, clock=None):
    """Pure bounded loading, no filesystem/database/browser access or approval.

    clock is injectable for fictional tests. A future real caller must use the
    real clock and separately reviewed pins, never hashes derived on the fly
    from untrusted input. The host separately verifies current DB/code/expiry.
    """
    return ReviewedGapContract(data, reviewed_sha256, external_evidence,
        reviewed_evidence_sha256, purpose, clock or (lambda: datetime.now(timezone.utc)))
