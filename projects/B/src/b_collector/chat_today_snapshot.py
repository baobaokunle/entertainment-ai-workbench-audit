"""Single-decryption preparation, private memory for one owning host only.

No persistence, source proof, approval issuer or restart entry. A snapshot is
valid only in the original store/lock lifetime and for an empty child created
by the separately reviewed atomic start. It never retains message bodies.
"""
from copy import deepcopy
from datetime import datetime,timezone
import hashlib,json
from types import MappingProxyType

from .contract import encode,decode
from .security import CollectorError
from .chat_today_journal import require_lock,digest
from .chat_today_start import inventory
from .chat_today_window import today_targets
from .chat_reviewed_resume import ChatReviewedRecoveryPlan
from .chat_today_materialize import identity,native_digest,InheritanceIndex

def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf8')

class TodayPreparedSnapshot:
    __slots__=('_store','_lock','_lock_handle','_database','_parent','_inventory','_codes','_targets','_evidence',
               '_versions','_proofs','_media','_seal','_claimed','_child','_indexed')

    @classmethod
    def prepare(cls,store,lock,*,parent,basis,code_sha256):
        require_lock(store,lock)
        obj=cls();obj._store=store;obj._lock=lock;obj._lock_handle=lock.handle;obj._database=store.db
        versions=set();proofs={};media=set()
        with store.transaction():
            require_lock(store,lock)
            row,meta=store._run(parent)
            if ({r[0] for r in store.db.execute('SELECT id FROM runs')}!={parent}
                or row[1] not in ('failed','disconnected')
                or datetime.now(timezone.utc)>=datetime.fromisoformat(meta['expires_at'])):
                raise CollectorError('CHAT_TODAY_SNAPSHOT_PARENT')
            if (basis.get('run_id')!=parent or type(basis.get('prefix_count')) is not int
                or basis['prefix_count']!=row[3]):
                raise CollectorError('CHAT_TODAY_SNAPSHOT_FULL_PREFIX_REQUIRED')
            pin={'run_id':parent,'generation':row[0],'state':row[1],'records':row[3],
                 'meta_sha256':hashlib.sha256(row[2]).hexdigest()}
            inv=inventory(store,parent)
            plan=ChatReviewedRecoveryPlan(parent,row,meta,basis)
            for identifier,seq,cipher in store.db.execute('SELECT id,sequence,cipher FROM observations WHERE run_id=? ORDER BY sequence',(parent,)):
                payload=store._decrypt(cipher)
                if payload.get('observation_id')!=identifier:raise CollectorError('CHAT_TODAY_SNAPSHOT_RECORD')
                # The existing planner validates scope, owner, sequence and
                # exclusions. Feed the same decrypted row to the version index.
                plan.observe_cipher(seq,cipher);plan.feed(payload,seq)
                item=payload['observation'];fields=item['fields']
                if item['domain']!='chat':continue
                if item['context']['owner']!=meta['chat_resume_owner']:raise CollectorError('SOURCE_CHANGED')
                key=identity(item['context']['owner'],item['context']['conversation'],fields)
                if key is None:continue
                if fields.get('b_chat.record_role',{}).get('value')!='media_attachment':
                    version=(key,native_digest(fields));versions.add(version)
                    proofs.setdefault(version,(parent,identifier,seq,hashlib.sha256(cipher).hexdigest()))
                status=fields.get('b_chat.media_status',{}).get('value')
                if (status is not None and status!='classification_pending'
                    or fields.get('b_chat.media_file_saved',{}).get('value') is True or 'b_chat.media_asset_id' in fields):
                    media.add(key)
            targets,evidence=today_targets(plan)
            if plan.owner!=meta['chat_resume_owner'] or inventory(store,parent)!=inv:
                raise CollectorError('CHAT_TODAY_SNAPSHOT_CHANGED')
            require_lock(store,lock)
            if store._run(parent)[0]!=row or datetime.now(timezone.utc)>=datetime.fromisoformat(meta['expires_at']):
                raise CollectorError('CHAT_TODAY_SNAPSHOT_CHANGED')
        obj._parent=canonical({**pin,'prefix_sha256':basis['prefix_sha256'],'scope_sha256':basis['scope_sha256'],
                            'expires_at':basis['expires_at'],'unattributed':basis['unattributed'],'stopped_at':meta['finished_at']})
        obj._inventory=canonical(inv);obj._codes=canonical(code_sha256)
        obj._targets=tuple(encode(t) for t in targets);obj._evidence=encode(evidence)
        obj._versions=frozenset(versions);obj._proofs=MappingProxyType(proofs);obj._media=frozenset(media)
        obj._claimed=False;obj._child=None;obj._indexed=False;obj._seal=obj._fingerprint()
        return obj

    def _fingerprint(self):
        h=hashlib.sha256()
        def add(value):h.update(len(value).to_bytes(8,'big'));h.update(value)
        if set(self._proofs)!=set(self._versions):raise CollectorError('CHAT_TODAY_SNAPSHOT_CHANGED')
        add(encode(['b-chat-today-memory-snapshot/1',len(self._targets),len(self._versions),len(self._media)]))
        for value in (self._parent,self._inventory,self._codes,self._evidence):add(value)
        for target in self._targets:add(target)
        for key,version in sorted(self._versions):
            add(key);add(encode(version));add(encode(self._proofs[(key,version)]))
        for key in sorted(self._media):add(key)
        return h.hexdigest()

    def _check(self,store,lock):
        if (store is not self._store or lock is not self._lock or store.db is not self._database
            or lock.handle is not self._lock_handle):raise CollectorError('CHAT_TODAY_SNAPSHOT_HOST')
        require_lock(store,lock)
        if self._fingerprint()!=self._seal:raise CollectorError('CHAT_TODAY_SNAPSHOT_CHANGED')

    def reviewed_projection(self):
        # Fresh decoded copies; callers cannot mutate the sealed memory.
        return {'parent':decode(self._parent),'inventory':decode(self._inventory),
                'targets':[decode(v) for v in self._targets],'evidence':decode(self._evidence),
                'snapshot_sha256':self._seal}

    def claim_start(self,store,lock,contract,actual_inventory):
        self._check(store,lock)
        if self._claimed:raise CollectorError('CHAT_TODAY_SNAPSHOT_CONSUMED')
        self._claimed=True
        if (canonical(contract['parent'])!=self._parent or canonical(contract['code_sha256'])!=self._codes
            or canonical(actual_inventory)!=self._inventory or canonical(contract['inventory'])!=self._inventory
            or len(self._targets)!=contract['target_count']
            or digest([decode(v) for v in self._targets])!=contract['targets_sha256']):
            raise CollectorError('CHAT_TODAY_SNAPSHOT_CHANGED')
        return [decode(v) for v in self._targets],decode(self._evidence)

    def bind_created(self,receipt):
        self._check(self._store,self._lock)
        if not self._claimed or self._child is not None:raise CollectorError('CHAT_TODAY_SNAPSHOT_CONSUMED')
        self._child=canonical({k:receipt[k] for k in ('run_id','generation','meta_sha256')})

    def new_index(self,journal):
        self._check(journal.store,journal.lock)
        if self._indexed or self._child!=canonical({'run_id':journal.run,'generation':journal.generation,'meta_sha256':journal.expected_meta_sha256}):
            raise CollectorError('CHAT_TODAY_SNAPSHOT_CONSUMED')
        self._indexed=True
        with journal.store.transaction():
            journal._check()
            parent=decode(self._parent)
            if journal.policy['parent']!={k:parent[k] for k in ('run_id','generation','state','records','meta_sha256')}:
                raise CollectorError('CHAT_TODAY_SNAPSHOT_CHANGED')
            if (journal.store._run(journal.run)[0][3]!=0
                or journal.store.db.execute('SELECT 1 FROM observations WHERE run_id=?',(journal.run,)).fetchone()
                or inventory(journal.store,parent['run_id'])!=decode(self._inventory)):
                raise CollectorError('CHAT_TODAY_SNAPSHOT_CHANGED')
            result=InheritanceIndex.__new__(InheritanceIndex);result.journal=journal
            result.parent=deepcopy(journal.policy['parent']);result.current_count=0
            result.versions=set(self._versions);result.media_attempted=set(self._media)
            result.evidence={key:dict(zip(('run_id','observation_id','sequence','cipher_sha256'),proof)) for key,proof in self._proofs.items()}
            result.check()
        return result
