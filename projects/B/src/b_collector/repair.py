"""Plan only exact failed base requests. No nickname, body or coverage inference."""
import re
from .contract import scalar
from .security import CollectorError


def room_key(value):
    return (isinstance(value, dict) and value.get('type') == 'string'
            and value.get('lossless') is True and isinstance(value.get('value'), str)
            and re.fullmatch(r'[0-9]{1,40}', value['value']) is not None)


def binding(owner, app):
    if not room_key(owner) or app is None:
        raise CollectorError('REPAIR_ACCOUNT_UNKNOWN')
    scalar(owner)
    scalar(app)
    if app['lossless'] is not True or app['type'] not in {'string', 'number', 'number_token'}:
        raise CollectorError('REPAIR_ACCOUNT_UNKNOWN')
    if app['type'] == 'string' and not app['value'].strip():
        raise CollectorError('REPAIR_ACCOUNT_UNKNOWN')
    return {'owner': owner, 'app': app}


class BaseRepairPlan:
    def __init__(self, parent, meta, total):
        if 'session' not in meta['scope']['domains']:
            raise CollectorError('REPAIR_SCOPE')
        self.parent, self.meta, self.total = parent, meta, total
        self.offset = 0
        self.accounts, self.failed, self.success = [], set(), []
        self.unlocated = self.other = 0
        old = meta.get('base_repair')
        if old:
            if old.get('format') != 'b-base-repair/1':
                raise CollectorError('REPAIR_VERSION')
            self.accounts.append(binding(old['owner'], old['app']))
            self.failed.update(old['rooms'])  # Explicit persisted plan, including unattempted rooms.

    def feed(self, payload, sequence):
        if sequence != self.offset + 1 or payload.get('sequence') != sequence or payload.get('run_id') != self.parent:
            raise CollectorError('REPAIR_RECORD_MISMATCH')
        self.offset = sequence
        o = payload['observation']
        if o['source']['origin'] != 'https://anchor.douyin.com':
            return
        f, c = o['fields'], o['context']
        if o['domain'] == 'account' and o['source']['path'] == '/passport/account/info/v2/':
            if f.get('user_id_str') != c['owner'] or f.get('app_id') != c['app_scope']:
                raise CollectorError('REPAIR_ACCOUNT_UNKNOWN')
            self.accounts.append(binding(c['owner'], c['app_scope']))
        elif o['domain'] == 'capability':
            if f.get('domain', {}).get('value') == 'session' and o['coverage']['status'] == 'not_available':
                if room_key(c['session']):
                    self.failed.add(c['session']['value'])
                else:
                    self.unlocated += 1
            else:
                self.other += 1
        elif o['domain'] == 'session' and o['source']['path'].endswith('/room_base_v2'):
            if room_key(c['session']) and f.get('room_id') == c['session'] and f.get('anchor_id') == c['owner']:
                self.success.append((c['session']['value'], c['owner'], c['app_scope']))
        if len(self.failed) > 10000:
            raise CollectorError('REPAIR_PLAN_SIZE')

    def finish(self):
        if self.offset != self.total:
            raise CollectorError('REPAIR_NOT_READY')
        if not self.accounts:
            raise CollectorError('REPAIR_ACCOUNT_UNKNOWN')
        account = self.accounts[0]
        if any(x != account for x in self.accounts):
            raise CollectorError('REPAIR_ACCOUNT_CONFLICT')
        completed = {room for room, owner, app in self.success if owner == account['owner'] and app == account['app']}
        rooms = sorted(self.failed - completed)
        return {'format': 'b-base-repair/1', 'parent_run': self.parent, **account, 'rooms': rooms,
                'unlocated_failures': self.unlocated, 'other_diagnostics': self.other,
                'already_saved': len(self.failed & completed)}
