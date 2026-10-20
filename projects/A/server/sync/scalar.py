"""Causal scalar and membership registers. Clears/removes retain immutable tombstones."""
import json
from ..common import AppError,canonical_json
from ..db import digest,reject_floats
from .schema import require_schema

class ScalarStore:
    def __init__(self,db):require_schema(db);self.db=db;self.validators={}

    def register_validator(self,prefix,callback):
        """Server-owned typed field registration, also required for imported mutations."""
        if not isinstance(prefix,str) or len(prefix)>200 or not callable(callback):raise AppError(422,'SCALAR_VALIDATOR_INVALID','A finite server-owned validator registration is required.')
        self.validators[prefix]=callback

    def _read(self,conn,account,target,field):
        rows=[dict(r) for r in conn.execute('SELECT * FROM causal_revision WHERE account_key=? AND target_key=? AND field=?',(account,target,field))]
        by_id={r['event_id']:r for r in rows}
        if not rows:return {'state':'empty','effective_value':None,'revision':[],'candidates':[],'common_revision':None}
        parents={k:json.loads(r['parents_json']) for k,r in by_id.items()}
        superseded={p for ps in parents.values() for p in ps}
        tips=sorted(set(by_id)-superseded)
        def ancestors(key):
            seen={key};todo=list(parents[key])
            while todo:
                p=todo.pop()
                if p not in seen and p in parents:seen.add(p);todo.extend(parents[p])
            return seen
        common=set.intersection(*(ancestors(t) for t in tips))
        nearest=sorted(c for c in common if not any(c!=other and c in ancestors(other) for other in common))
        common_id=nearest[0] if len(nearest)==1 else None
        candidates=[{'event_id':t,'operation':by_id[t]['operation'],'value':json.loads(by_id[t]['value_json'])} for t in tips]
        same=len({canonical_json([c['operation'],c['value']]) for c in candidates})==1
        effective=candidates[0]['value'] if same else json.loads(by_id[common_id]['value_json']) if common_id else None
        return {'state':'effective' if same else 'conflict','effective_value':effective,'revision':tips,'candidates':candidates,'common_revision':common_id}

    def get(self,account,target,field):
        with self.db.lock:return self._read(self.db.conn,account,target,field)

    def list_fields(self,account,target):
        with self.db.lock:
            names=[r[0] for r in self.db.conn.execute('SELECT DISTINCT field FROM causal_revision WHERE account_key=? AND target_key=? ORDER BY field',(account,target))]
            return {name:self._read(self.db.conn,account,target,name) for name in names}

    def set(self,account,target,field,value,*,expected_revision,actor,clear=False,operation=None):
        reject_floats(value)
        operation=operation or ('clear' if clear else 'set')
        payload={'field':field,'operation':operation,'value':value}
        with self.db.transaction() as conn:
            current=self._read(conn,account,target,field)
            expected=[] if expected_revision is None else [expected_revision] if isinstance(expected_revision,str) else expected_revision
            if expected!=current['revision']:raise AppError(409,'REVISION_CONFLICT','Expected causal revision is stale.')
            self._validate(conn,account,target,payload,current['revision'])
            event_id=self.db.append_event(conn,account_key=account,target_key=target,event_type='scalar.'+operation,payload=payload,parents=current['revision'],channel='profile',actor=actor)
            event=json.loads(conn.execute('SELECT event_json FROM domain_event WHERE event_id=?',(event_id,)).fetchone()[0])
            self.apply_remote(conn,event)
            result=self._read(conn,account,target,field);result['status']='saved';return result

    def _validate(self,conn,account,target,payload,parents):
        if set(payload)!={'field','operation','value'} or not isinstance(payload['field'],str) or not payload['field'] or len(payload['field'])>200 or payload['operation'] not in ('set','clear','add','remove'):
            raise AppError(422,'INVALID_SCALAR','Finite scalar field and operation required.')
        if len(canonical_json(payload))>262144:raise AppError(413,'SCALAR_TOO_LARGE','Scalar exceeds limit.')
        registered=sorted((p for p in self.validators if payload['field'].startswith(p)),key=len,reverse=True)
        if registered:self.validators[registered[0]](payload)
        else:
            try:
                from ..profile import validate_scalar
            except ImportError as exc:raise AppError(422,'SCALAR_FIELD_UNREGISTERED','Field requires an independently registered typed domain validator.') from exc
            validate_scalar(payload)
        if payload['field'].startswith('opportunity_round.') and payload['value']=='displayed' and conn.execute("SELECT 1 FROM causal_revision WHERE account_key=? AND target_key=? AND field=? AND value_json='\"canceled\"'",(account,target,payload['field'])).fetchone():
            raise AppError(409,'ROUND_CANCELED','A canceled opportunity round cannot be revived.')
        if payload['field'].startswith('custom_group.') and payload['value']['deleted'] is False and conn.execute("SELECT 1 FROM causal_revision WHERE account_key=? AND target_key=? AND field=? AND json_extract(value_json,'$.deleted')=1",(account,target,payload['field'])).fetchone():
            raise AppError(409,'GROUP_DELETED','已删除分组不能由旧改名或后续旧数据复活')
        if payload['operation']=='clear' and payload['value']!='':raise AppError(422,'INVALID_CLEAR','Clear must retain the explicit empty string.')
        if payload['operation'] in ('add','remove') and payload['value'] is not (payload['operation']=='add'):raise AppError(422,'INVALID_MEMBERSHIP','Member edge must use a boolean tombstone.')
        if payload['field'].startswith('custom_group.'):
            if target!=account or not conn.execute('SELECT 1 FROM own_account WHERE account_key=?',(account,)).fetchone():raise AppError(409,'TARGET_UNKNOWN','分组定义必须属于明确账号')
        elif not conn.execute('SELECT 1 FROM contact WHERE account_key=? AND person_key=?',(account,target)).fetchone():raise AppError(409,'TARGET_UNKNOWN','Stable account-scoped target must be registered.')
        for parent in parents:
            row=conn.execute('SELECT account_key,target_key,field FROM causal_revision WHERE event_id=?',(parent,)).fetchone()
            if not row or tuple(row)!=(account,target,payload['field']):raise AppError(409,'SCALAR_PARENT_INVALID','Parent must reference this account, target and field.')

    def apply_remote(self,conn,event):
        p=event['payload'];self._validate(conn,event['account_key'],event['target_key'],p,event['parents'])
        if event['event_type']!='scalar.'+p['operation']:raise AppError(422,'INVALID_SCALAR','Event type must bind the scalar operation.')
        conn.execute('INSERT INTO causal_revision VALUES(?,?,?,?,?,?,?,?)',(event['event_id'],event['account_key'],event['target_key'],p['field'],p['operation'],canonical_json(p['value']).decode(),canonical_json(event['parents']).decode(),canonical_json(event).decode()))

    def register_handlers(self,importer):
        for operation in ('set','clear','add','remove'):importer.register_handler('scalar.'+operation,self.apply_remote)
