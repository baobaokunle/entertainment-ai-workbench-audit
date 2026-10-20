"""Closed command surface for the independently locked chat host."""
from .protocol import Dispatcher
from .contract import keys, UUID
from .security import CollectorError
from .chat_incremental import IncrementalChat, timestamp
from .chat_store import value
from datetime import datetime, timezone, timedelta
import uuid


class ChatDispatcher(Dispatcher):
    ALLOWED = {'hello', 'start', 'append', 'finish', 'list_runs', 'preview', 'review_page', 'export', 'delete_run', 'chat_prepare', 'chat_stats', 'media_preview', 'chat_resume_scan', 'chat_directory_repair_scan', 'chat_reviewed_recovery_scan', 'chat_resume_open', 'chat_resume_targets'}

    def __init__(self, store, legacy_loader=None, downloader=None, allow_initial_binding=False, directory_repair_timeout=None, reviewed_approval=None):
        super().__init__(store)
        self.processor = None; self.legacy_loader = legacy_loader; self.downloader = downloader
        self.allow_initial_binding = allow_initial_binding
        self.chat_plans = {}; self.chat_resume = None
        self.directory_repair_timeout = directory_repair_timeout
        self.reviewed_approval = reviewed_approval

    def call(self, request):
        rid = request.get('id') if isinstance(request, dict) else None
        try:
            keys(request, {'protocol', 'id', 'op', 'args'})
            if request['protocol'] != 'b-native/1' or not isinstance(rid, str) or not UUID.fullmatch(rid):
                raise CollectorError('PROTOCOL_VERSION')
            op = request['op']; a = request['args']
            if op not in self.ALLOWED: raise CollectorError('CHAT_COMMAND_NOT_ALLOWED')
            if op in ('chat_resume_scan', 'chat_directory_repair_scan', 'chat_reviewed_recovery_scan'):
                if op == 'chat_reviewed_recovery_scan' and self.reviewed_approval is None: raise CollectorError('CHAT_RECOVERY_NOT_APPROVED')
                keys(a, {'run_id', 'plan_id', 'offset'})
                if self.active: raise CollectorError('TASK_ALREADY_RUNNING')
                if type(a['offset']) is not int or a['offset'] < 0: raise CollectorError('CHAT_RESUME_CURSOR')
                key = a['plan_id']
                if key is None:
                    if a['offset'] != 0: raise CollectorError('CHAT_RESUME_CURSOR')
                    self.chat_plans.clear(); key, plan = uuid.uuid4().hex, None
                else:
                    plan = self.chat_plans.get(key)
                    if plan is None or plan.run != a['run_id'] or plan.offset != a['offset']:
                        raise CollectorError('CHAT_RESUME_CURSOR')
                plan = self.store.resume_scan_page(a['run_id'], plan, directory_repair=op == 'chat_directory_repair_scan',
                    prior_timeout=self.directory_repair_timeout if op == 'chat_directory_repair_scan' else None,
                    reviewed_approval=self.reviewed_approval if op == 'chat_reviewed_recovery_scan' else None)
                self.chat_plans[key] = plan
                done = plan.offset == plan.total
                result = {'plan_id': key, 'next_offset': plan.offset, 'total': plan.total, 'done': done}
                if done:
                    targets = plan.finish()
                    result.update(owner=plan.owner, target_count=len(targets), completed_count=len(plan.completed), directory_count=len(plan.conversations))
                return {'id': rid, 'ok': True, 'result': result}
            if op == 'chat_resume_open':
                keys(a, {'plan_id', 'owner'})
                if self.active: raise CollectorError('TASK_ALREADY_RUNNING')
                plan = self.chat_plans.get(a['plan_id'])
                if plan is None: raise CollectorError('CHAT_RESUME_NOT_READY')
                targets = plan.finish()
                result = self.store.resume_open(plan, a['owner'])
                self.active[result['run_id']] = result['generation']
                self.kinds[result['run_id']] = 'ordinary'
                self.chat_resume = {**result, 'targets': targets}
                self.chat_plans.clear(); self.processor = None
                return {'id': rid, 'ok': True, 'result': result}
            if op == 'chat_resume_targets':
                keys(a, {'run_id', 'generation', 'offset'})
                if self.active.get(a['run_id']) != a['generation']: raise CollectorError('TASK_NOT_OWNED')
                self.store._run(a['run_id'], a['generation'], active=True)
                state = self.chat_resume
                if state is None or (state['run_id'], state['generation']) != (a['run_id'], a['generation']):
                    raise CollectorError('CHAT_RESUME_NOT_READY')
                offset = a['offset']; targets = state['targets']
                if type(offset) is not int or not 0 <= offset <= len(targets): raise CollectorError('CHAT_RESUME_CURSOR')
                rows = targets[offset:offset + 100]
                return {'id': rid, 'ok': True, 'result': {'rows': rows, 'next_offset': offset + len(rows), 'total': len(targets)}}
            if op == 'media_preview':
                import base64
                keys(a, {'run_id', 'asset_id', 'offset'})
                if type(a['offset']) is not int or a['offset'] < 0: raise CollectorError('PAGE_OFFSET')
                manifest, data = self.store.read_media(a['run_id'], a['asset_id'])
                block = data[a['offset']:a['offset'] + 400000]
                return {'id': rid, 'ok': True, 'result': {'mime': manifest['mime'], 'total': len(data), 'next_offset': a['offset'] + len(block), 'data': base64.b64encode(block).decode('ascii')}}
            if op in ('chat_prepare', 'chat_stats'):
                expected = {'run_id', 'generation'} | ({'owner', 'conversation', 'rows', 'urls', 'source_times'} if op == 'chat_prepare' else set())
                keys(a, expected)
                if self.active.get(a['run_id']) != a['generation']: raise CollectorError('TASK_NOT_OWNED')
                self.store._run(a['run_id'], a['generation'], active=True)
                if self.chat_resume and op == 'chat_prepare':
                    if a['owner'] != self.chat_resume['owner'] or a['conversation'] not in self.chat_resume['targets']:
                        raise CollectorError('SOURCE_CHANGED')
                if self.processor is None and op == 'chat_stats':
                    raise CollectorError('CHAT_NOT_PREPARED')
                if self.processor is None:
                    legacy = self.legacy_loader() if self.legacy_loader else None
                    if legacy and op == 'chat_prepare':
                        binding = self.store.bind_legacy(a['owner'], legacy['source_sha256'], self.allow_initial_binding)
                        legacy['owner'] = binding['owner']
                    self.processor = IncrementalChat(self.store, a['run_id'], a['generation'], legacy, self.downloader,
                                                     resume_count=self.chat_resume['total'] if self.chat_resume else None)
                if op == 'chat_stats': result = dict(self.processor.stats)
                else:
                    if (not isinstance(a['rows'], list) or len(a['rows']) > 100
                            or not isinstance(a['urls'], list) or len(a['urls']) != len(a['rows'])
                            or any(not isinstance(u, list) or len(u) > 3 for u in a['urls'])
                            or not isinstance(a['source_times'], list) or len(a['source_times']) > 100):
                        raise CollectorError('CHAT_BATCH_SIZE')
                    result = self.processor.process(a['owner'], a['conversation'], a['rows'], a['urls'])
                    latest = self.processor.legacy['latest'].get(value(a['conversation'].get('id'))) if self.processor.legacy.get('owner') == a['owner'] else None
                    boundary = None
                    if latest is not None:
                        boundary = datetime.fromtimestamp(latest, timezone(timedelta(hours=8))).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
                    result['beforeBoundary'] = bool(a['source_times']) and boundary is not None and all(
                        type(t) in (int, float) and t < boundary for t in a['source_times'])
                return {'id': rid, 'ok': True, 'result': result}
            result = super().call(request)
            if result.get('ok') and op in ('start', 'finish'):
                self.processor = None; self.chat_resume = None; self.chat_plans.clear()
            if result.get('ok') and op == 'hello':
                result['result'].update(version='0.1.9-chat', base_repair_supported=False, gap_repair_supported=False,
                                        storage='independent_chat', media_supported=True)
            return result
        except CollectorError as exc:
            return {'id': rid, 'ok': False, 'error': str(exc)}
        except Exception:
            return {'id': rid, 'ok': False, 'error': 'CHAT_OPERATION_FAILED'}
