"""Narrow saved-profile editor projection. Never requests chat or AI data."""
import re
from .common import AppError
from .profile import FIELDS


def editable_field(field):
    return field in FIELDS or bool(re.fullmatch(r'(free_term|entitlement\.(?:vip|star))\.[A-Za-z0-9_-]{1,100}', field))


def snapshot(app, account, person, principal):
    if not app.profiles or not app.projections:
        raise AppError(503, 'feature_not_initialized', '资料模块未初始化')
    profile = app.profiles.get(account, person)
    if not profile['verified'] or profile['object_kind'] != 'person':
        raise AppError(409, 'PROFILE_IDENTITY_REQUIRED', '仅编辑已核验个人资料')
    fields = {}
    for name, record in profile['fields'].items():
        if editable_field(name) or re.fullmatch(r'group\.[a-f0-9]{32}', name):
            fields[name] = {key: record[key] for key in ('state', 'effective_value', 'revision')}
    definitions = app.projections.groups.definitions(account)['items']
    groups = [{key: row[key] for key in ('group_id', 'name', 'deleted', 'state')} for row in definitions]
    scope = 'profile:write' in principal.scopes and not app.read_only
    notes = getattr(app, 'local_notes', None)
    note = notes.capabilities(principal) if notes is not None else None
    all_fields = scope and app.writes_enabled and notes is None
    note_only = scope and note is not None and note.get('state') == 'explicit_local_notes' and note.get('account_key') == account and note.get('profile_fields') == ['note']
    return {'format': 'native-profile-editor-1', 'account_key': account, 'person_key': person,
            'fields': fields, 'groups': groups,
            'access': {'fields': 'all' if all_fields else 'note' if note_only else 'none', 'membership': bool(all_fields)}}
