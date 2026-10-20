"""Offline candidate: fixed-day selection and commit-gated page checkpoints.

No source/store access, permissions, approval loader or production imports.
Returned rows contain private material: future callers must keep them in the
private pipe/memory or protected store. Digests do not authorize execution.
"""
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import hashlib
import re
import uuid

from .contract import encode, decode, scalar
from .security import CollectorError

BJ = timezone(timedelta(hours=8))
ISO = re.compile(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)')
KEY = re.compile(r'[0-9]{1,40}')


def _time(value):
    if not isinstance(value, str) or not ISO.fullmatch(value):
        raise CollectorError('CHAT_WINDOW_TIME_INVALID')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise CollectorError('CHAT_WINDOW_TIME_INVALID') from None
    return result.astimezone(timezone.utc)


def _scalar(value, kind='string'):
    return (isinstance(value, dict) and value.get('type') == kind
            and value.get('lossless') is True
            and (type(value.get('value')) is str if kind == 'string' else type(value.get('value')) is int)
            and (bool(value['value']) if kind == 'string' else True))


def _same(left, right):
    return (_scalar(left) and _scalar(right) and left['value'] == right['value'])


@dataclass(frozen=True)
class TodayWindow:
    start: datetime
    cutoff: datetime
    expires: datetime
    approved_at: datetime

    @classmethod
    def parse(cls, contract, *, now, parent_expiry):
        if (not isinstance(contract, dict) or set(contract) != {
                'format', 'timezone', 'start_inclusive', 'cutoff_exclusive', 'approved_at', 'expires_at'}
                or contract['format'] != 'b-chat-today-window/1' or contract['timezone'] != 'Asia/Shanghai'):
            raise CollectorError('CHAT_WINDOW_CONTRACT_INVALID')
        start, end, approved, expiry = map(_time, (contract['start_inclusive'], contract['cutoff_exclusive'],
                                                 contract['approved_at'], contract['expires_at']))
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise CollectorError('CHAT_WINDOW_TIME_INVALID')
        local = start.astimezone(BJ)
        if (local.time().isoformat() != '00:00:00' or local.date() != approved.astimezone(BJ).date()
                or not start < end <= approved <= now < expiry <= _time(parent_expiry)
                or end > start + timedelta(days=1)):
            raise CollectorError('CHAT_WINDOW_BOUNDARY_INVALID')
        return cls(start, end, expiry, approved)

    def route(self, rows, conversation):
        """Do not download media or drop unknown timestamps as if outside scope."""
        if not isinstance(rows, list) or len(rows) > 100:
            raise CollectorError('CHAT_WINDOW_PAGE_SIZE')
        if not _scalar(conversation.get('id')) or not _scalar(conversation.get('shortId')):
            raise CollectorError('CHAT_WINDOW_BINDING_INVALID')
        selected, unknown = [], []
        counts = {'selected': 0, 'before_window': 0, 'at_or_after_cutoff': 0, 'unknown_time_or_key': 0}
        for fields in rows:
            if not isinstance(fields, dict): raise CollectorError('CHAT_WINDOW_PAGE_INVALID')
            direct = fields.get('conversationId'); short = fields.get('conversationShortId')
            if direct is not None and not _same(direct, conversation['id']): raise CollectorError('SOURCE_CHANGED')
            if short is not None and not _same(short, conversation['shortId']): raise CollectorError('SOURCE_CHANGED')
            if direct is None and short is None: raise CollectorError('CHAT_WINDOW_BINDING_INVALID')
            label, basis = fields.get('b_chat.created_at_iso'), fields.get('b_chat.created_at_basis')
            moment = None
            if (_scalar(label) and _scalar(basis) and basis['value'] == 'native_Date_utc_projection'
                    and any(_scalar(fields.get(k)) for k in ('serverId', 'clientId', 'id'))):
                try: moment = _time(label['value'])
                except CollectorError: pass
            if moment is None:
                unknown.append(deepcopy(fields)); counts['unknown_time_or_key'] += 1
            elif moment < self.start:
                counts['before_window'] += 1
            elif moment >= self.cutoff:
                counts['at_or_after_cutoff'] += 1
            else:
                selected.append(deepcopy(fields)); counts['selected'] += 1
        return {'selected_rows': selected, 'unresolved_rows': unknown, 'counts': counts}


def today_targets(reviewed_plan):
    """Reuse a verified saved directory; never include a failed old conversation.

    Calling finish checks the existing prefix, owner, scope, exclusions and
    unknown-failure policy. A completed old conversation is eligible only for
    the independent new-day window, not for another historical replay.
    """
    reviewed_plan.finish()
    # Old plans preserve JSON byte keys. New-day exclusions must compare
    # typed native identity, not JSON member order: a reordered failure/claim
    # binding must never become a fresh eligible target.
    def native(value):
        scalar(value)
        if not _scalar(value): raise CollectorError('CHAT_WINDOW_BINDING_INVALID')
        return (value['type'], value['value'], value['lossless'])
    def semantic(values):
        return {native(decode(v)) for v in values}
    completed = semantic(reviewed_plan.completed)
    exclude = semantic(reviewed_plan.failed) | ((semantic(reviewed_plan.touched) | semantic(reviewed_plan.claimed)) - completed)
    directory, signatures = {}, {}
    for key, conv in reviewed_plan.conversations.items():
        identity = native(decode(key))
        if identity != native(conv['id']): raise CollectorError('CHAT_WINDOW_BINDING_INVALID')
        for value in conv.values(): scalar(value)
        signature = encode([[k, v['type'], v['value'], v['lossless']] for k, v in sorted(conv.items())])
        if identity in signatures and signatures[identity] != signature:
            raise CollectorError('CHAT_WINDOW_BINDING_INVALID')
        signatures[identity] = signature
        directory.setdefault(identity, conv)
    targets = [deepcopy(v) for k, v in directory.items() if k not in exclude]
    return targets, {'directory_count': len(directory), 'today_target_count': len(targets),
                     'excluded_conversations': len(set(directory) & exclude),
                     'target_set_sha256': hashlib.sha256(encode(targets)).hexdigest(),
                     'old_unattributed_failures': len(reviewed_plan.unknown_seen),
                     'platform_complete': False}


class TodayPageCheckpoint:
    """One conversation, one staged page. Not itself a durable checkpoint.

    A future host must persist the page, unknown rows, media receipts and cursor
    atomically, then call after_commit. Never call it on a lost commit response.
    No time-based early stop is supported without a separate ordering contract.
    """
    def __init__(self, window, owner, conversation, *, page_budget,entry_kind='history_only'):
        if (not isinstance(window, TodayWindow) or not _scalar(owner)
                or not isinstance(conversation, dict) or not _scalar(conversation.get('id'))
                or not _scalar(conversation.get('shortId')) or not _scalar(conversation.get('type'), 'number')
                or type(page_budget) is not int or not 1 <= page_budget <= 100
                or entry_kind not in {'history_only','cache_only'}):
            raise CollectorError('CHAT_WINDOW_CHECKPOINT_INVALID')
        self.window, self.owner, self.conversation = window, deepcopy(owner), deepcopy(conversation)
        self.page_budget = page_budget
        self.entry_kind = entry_kind
        self.cursor = None; self.pages = 0; self.cursors = set(); self.unknown = 0
        self.pending = None; self.source_ended = False; self.blocked = False

    def stage(self, response, *, requested_cursor, now):
        if self.blocked or self.pending or self.source_ended or self.pages >= self.page_budget:
            raise CollectorError('CHAT_WINDOW_NOT_READY')
        try:
            if not isinstance(now, datetime) or now.tzinfo is None or now >= self.window.expires:
                raise CollectorError('RETENTION_EXPIRED')
            if requested_cursor != self.cursor: raise CollectorError('CHAT_CURSOR')
            if not isinstance(response, dict) or response.get('ok') is not True:
                raise CollectorError('CHAT_NATIVE_EVIDENCE_REQUIRED')
            if self.entry_kind=='cache_only':
                offset=0 if self.cursor is None else int(self.cursor)
                total=response.get('cache_total');nxt=response.get('next')
                if (response.get('op')!='chat_cache' or response.get('cache_contract')!='native-cache/1'
                        or type(response.get('cache_offset')) is not int or response['cache_offset']!=offset
                        or type(total) is not int or total<offset or total>50000
                        or not isinstance(response.get('rows'),list) or len(response['rows'])!=min(100,total-offset)
                        or (nxt is not None and (type(nxt) is not int or nxt!=offset+100 or nxt>=total))
                        or (nxt is None and offset+100<total)):
                    raise CollectorError('CHAT_NATIVE_EVIDENCE_REQUIRED')
                more=nxt is not None;cursor=str(nxt if more else total)
            else:
                if (response.get('op')!='chat_history' or response.get('history_contract')!='native-history/1'
                        or type(response.get('has_more')) is not bool):
                    raise CollectorError('CHAT_NATIVE_EVIDENCE_REQUIRED')
                more=response['has_more'];cursor=response.get('cursor')
            if not _same(response.get('owner'), self.owner): raise CollectorError('SOURCE_CHANGED')
            conv = response.get('conversation')
            if not isinstance(conv, dict): raise CollectorError('SOURCE_CHANGED')
            if any(conv.get(k) != self.conversation.get(k) for k in ('id', 'shortId', 'type')):
                raise CollectorError('SOURCE_CHANGED')
            if not isinstance(cursor, str) or not KEY.fullmatch(cursor): raise CollectorError('CHAT_CURSOR')
            if more and (cursor == self.cursor or cursor in self.cursors):
                raise CollectorError('CHAT_CURSOR_NOT_ADVANCING')
            routed = self.window.route(response.get('rows'), self.conversation)
            self.pending = {'token': uuid.uuid4().hex, 'cursor': cursor,
                            'source_ended': not more,
                            'unknown': routed['counts']['unknown_time_or_key'],
                            'response_sha256': hashlib.sha256(encode(response)).hexdigest()}
            return {**routed, 'checkpoint_after_commit': deepcopy(self.pending),
                    'old_page_proves_window_end': False}
        except Exception:
            self.blocked = True
            raise

    def after_commit(self, token):
        if self.blocked or not self.pending or token != self.pending['token']:
            raise CollectorError('CHAT_WINDOW_COMMIT_MISMATCH')
        p = self.pending
        self.cursor = p['cursor']; self.cursors.add(self.cursor); self.pages += 1
        self.unknown += p['unknown']; self.source_ended = p['source_ended']; self.pending = None
        return self.status()

    def unknown_commit(self):
        self.blocked = True
        return self.status()

    def status(self):
        return {'committed_pages': self.pages, 'unresolved_time_or_key': self.unknown,
                'source_ended': self.source_ended,
                # This component does not prove how cache and the first native
                # history page cover the newest messages. End-of-chain alone
                # must not turn into a complete-window claim.
                'window_selection_complete': False,
                'coverage_basis': ('cache_pages_only_history_unread' if self.entry_kind=='cache_only' else 'history_pages_only_entry_and_cache_coverage_unverified'),
                'budget_partial': self.pages >= self.page_budget and not self.source_ended,
                'inflight_unknown': self.blocked or self.pending is not None,
                'platform_complete': False}
