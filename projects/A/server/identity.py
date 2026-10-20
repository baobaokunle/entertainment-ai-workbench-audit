"""Stable account scoped identities; display names never participate."""
import json
from .common import AppError, canonical_json, utc_now
from .db import stable_key, digest, reject_floats


def require_text(value, field, limit=512):
    if not isinstance(value,str) or not value.strip() or len(value)>limit or '\x00' in value:
        raise AppError(400, 'INVALID_' + field.upper(), 'A valid ' + field + ' is required.')
    return value


class IdentityService:
    def __init__(self, db):
        self.db = db

    def get_source(self, source_id, source_version='1'):
        with self.db.lock:
            row = self.db.conn.execute('SELECT * FROM source WHERE source_id=? AND source_version=?', (source_id,source_version)).fetchone()
        if not row:
            return None
        result = dict(row)
        result['capabilities'] = json.loads(result.pop('capabilities_json'))
        result['evidence'] = json.loads(result.pop('evidence_json'))
        result['verified'] = bool(result['verified'])
        return result

    def trusted(self, source_id, source_version, capability, account_key=None):
        source = self.get_source(source_id,source_version)
        if not source or not source['verified'] or not source['evidence']:
            return False
        capabilities = source['capabilities']
        allowed = capability in capabilities if isinstance(capabilities,list) else capabilities.get(capability) is True
        if isinstance(capabilities,dict) and 'account_keys' in capabilities:
            allowed = allowed and account_key in capabilities['account_keys']
        return bool(allowed)

    def register_source(self, source_id, source_version, *, source_kind='fictional', capabilities=None, verified=False, evidence=None):
        require_text(source_id,'source_id',200)
        require_text(source_version,'source_version',80)
        if source_kind not in ('fictional','dom_adapter','qualified_adapter','trusted_local_action','legacy','unverified_export'):
            raise AppError(400,'INVALID_SOURCE_KIND','Source kind is unsupported.')
        capabilities = capabilities or {}
        if not isinstance(capabilities,(dict,list)) or (isinstance(capabilities,list) and not all(isinstance(x,str) for x in capabilities)):
            raise AppError(400,'INVALID_CAPABILITIES','Capabilities must be explicit names or boolean grants.')
        if isinstance(capabilities,dict):
            for key,value in capabilities.items():
                if key == 'account_keys':
                    if not isinstance(value,list) or not all(isinstance(x,str) for x in value):
                        raise AppError(400,'INVALID_CAPABILITIES','Account restrictions must be a list.')
                elif not isinstance(value,bool):
                    raise AppError(400,'INVALID_CAPABILITIES','Capability grants must be boolean.')
        evidence = evidence or {}
        reject_floats(evidence)
        # Boolean is a request to assess evidence, not evidence itself. Actual DOM starts unverified.
        trusted = bool(verified and evidence and source_kind in ('fictional','qualified_adapter','trusted_local_action'))
        semantic = {'source_id':source_id,'source_version':source_version,'source_kind':source_kind,'capabilities':capabilities,'verified':trusted,'evidence':evidence}
        hashed = digest(semantic)
        with self.db.transaction() as conn:
            old = conn.execute('SELECT registration_digest FROM source WHERE source_id=? AND source_version=?', (source_id,source_version)).fetchone()
            if old and old[0] != hashed:
                raise AppError(409,'SOURCE_VERSION_REUSED','Source version already has different provenance.')
            if not old:
                conn.execute('INSERT INTO source VALUES (?,?,?,?,?,?,?,?)', (source_id,source_version,source_kind,canonical_json(capabilities).decode(),int(trusted),canonical_json(evidence).decode(),hashed,utc_now()))
        return self.get_source(source_id,source_version)

    def register_account(self, platform_uid, *, source_id, source_version='1', verified=False):
        require_text(platform_uid,'platform_uid')
        if not self.get_source(source_id,source_version):
            raise AppError(409,'SOURCE_NOT_REGISTERED','Register source provenance before identities.')
        key = stable_key('account','douyin',platform_uid)
        with self.db.transaction() as conn:
            trusted = bool(verified and self.trusted(source_id,source_version,'account_identity',key))
            conn.execute('INSERT INTO own_account VALUES (?,?,?,?,?,?) ON CONFLICT(account_key) DO UPDATE SET verified=MAX(own_account.verified,excluded.verified),source_id=CASE WHEN excluded.verified THEN excluded.source_id ELSE own_account.source_id END,source_version=CASE WHEN excluded.verified THEN excluded.source_version ELSE own_account.source_version END', (key,'douyin',platform_uid,source_id,source_version,int(trusted)))
            row = dict(conn.execute('SELECT * FROM own_account WHERE account_key=?',(key,)).fetchone())
        row['verified']=bool(row['verified'])
        return row

    @staticmethod
    def _withdrawal_key(device_key,account_key,source_id,source_version):
        return 'origin_withdrawal:'+digest([device_key,account_key,source_id,source_version])

    def origin_withdrawn(self,device_key,account_key,source_id,source_version='1'):
        key=self._withdrawal_key(device_key,account_key,source_id,source_version)
        with self.db.lock:
            return self.db.conn.execute('SELECT 1 FROM app_meta WHERE key=?',(key,)).fetchone() is not None

    def _suspend_messages(self,conn,rows,*,emit_event):
        from .send_evidence import SendEvidence
        for row in rows:
            quality=json.loads(row['quality_json'])
            for dimension in ('identity','body','conversation','direction','time','success','message_identity'):
                if quality.get(dimension)!='conflict':quality[dimension]='source_revoked'
            quality['manual']='manual_unknown';quality['actor']='actor_unknown'
            conn.execute("UPDATE message SET direction='unknown',success_state='unknown',occurred_lower_utc=NULL,occurred_upper_utc=NULL,time_precision='unknown',platform_correlation_key=NULL,quality_json=? WHERE message_key=?",(canonical_json(quality).decode(),row['message_key']))
            SendEvidence(self.db).link_success(row['message_key'],emit_event=emit_event)

    def withdraw_origin(self,device_key,account_key,source_id,source_version='1',*,reason='provenance_withdrawn',emit_event=False):
        """Authenticated origin withdrawal; never revoke another device's source.

        The durable account/device/source/version tombstone prevents delayed
        observations or action events from restoring withdrawn authority.
        """
        for field,value in [('device_key',device_key),('account_key',account_key),('source_id',source_id),('source_version',source_version),('reason',reason)]:require_text(value,field,200)
        with self.db.transaction() as conn:
            local=dict(conn.execute('SELECT key,value FROM app_meta'))['device_key']
            if emit_event and device_key!=local:raise AppError(403,'WITHDRAWAL_ORIGIN_INVALID','Only this device can publish its own evidence withdrawal.')
            key=self._withdrawal_key(device_key,account_key,source_id,source_version)
            prior=conn.execute('SELECT 1 FROM app_meta WHERE key=?',(key,)).fetchone()
            if prior:return {'status':'duplicate','affected_messages':0}
            conn.execute('INSERT INTO app_meta VALUES(?,?)',(key,canonical_json({'device_key':device_key,'account_key':account_key,'source_id':source_id,'source_version':source_version,'reason':reason}).decode()))
            if device_key==local:
                # Local journal replay must also restore the origin's own source
                # withdrawal; peers only receive the scoped tombstone below.
                conn.execute('UPDATE source SET verified=0 WHERE source_id=? AND source_version=?',(source_id,source_version))
            rows=conn.execute('SELECT DISTINCT m.* FROM message m JOIN message_observation o ON o.message_key=m.message_key WHERE o.device_key=? AND o.account_key=? AND o.source_id=? AND o.source_version=?',(device_key,account_key,source_id,source_version)).fetchall()
            from .derived_import import dependent_messages
            rows=list({r['message_key']:r for r in [*rows,*dependent_messages(self.db,source_id,source_version,device=device_key,account=account_key)]}.values())
            links=conn.execute('SELECT DISTINCT l.message_key FROM send_link l JOIN manual_action a ON a.action_key=l.action_key WHERE a.device_key=? AND a.account_key=? AND a.source_id=? AND a.source_version=?',(device_key,account_key,source_id,source_version)).fetchall()
            conn.execute('UPDATE manual_action SET trusted=0 WHERE device_key=? AND account_key=? AND source_id=? AND source_version=?',(device_key,account_key,source_id,source_version))
            self._suspend_messages(conn,rows,emit_event=emit_event)
            from .send_evidence import SendEvidence
            for row in links:SendEvidence(self.db).link_success(row[0],emit_event=emit_event)
            if emit_event:
                self.db.append_event(conn,account_key=account_key,target_key=stable_key('source-withdrawal',device_key,account_key,source_id,source_version),event_type='source.revoked',payload={'source_id':source_id,'source_version':source_version,'reason':reason},channel='messages')
            return {'status':'revoked','affected_messages':len(rows)}

    def revoke_source(self,source_id,source_version='1',*,reason='provenance_withdrawn',emit_event=True):
        """Administrative local operation; conservatively suspend all linked evidence."""
        require_text(reason,'reason',200)
        with self.db.transaction() as conn:
            source=self.get_source(source_id,source_version)
            if source is None:raise AppError(404,'SOURCE_NOT_FOUND','Source was not found.')
            from .derived_import import dependent_messages
            derived=dependent_messages(self.db,source_id,source_version)
            if not source['verified'] and not any(json.loads(r['quality_json']).get('body')!='source_revoked' for r in derived):
                return {'status':'duplicate','affected_messages':0}
            local=dict(conn.execute('SELECT key,value FROM app_meta'))['device_key']
            # Publish withdrawal only for evidence originating here. Local distrust
            # of a peer's source does not impersonate that peer's authority.
            accounts={r[0] for r in conn.execute('SELECT account_key FROM message_observation WHERE device_key=? AND source_id=? AND source_version=? UNION SELECT account_key FROM manual_action WHERE device_key=? AND source_id=? AND source_version=?',(local,source_id,source_version,local,source_id,source_version))}
            for account in sorted(accounts):self.withdraw_origin(local,account,source_id,source_version,reason=reason,emit_event=emit_event)
            conn.execute('UPDATE source SET verified=0 WHERE source_id=? AND source_version=?',(source_id,source_version))
            conn.execute('UPDATE own_account SET verified=0 WHERE source_id=? AND source_version=?',(source_id,source_version))
            conn.execute('UPDATE identity_alias SET verified=0 WHERE source_id=? AND source_version=?',(source_id,source_version))
            conn.execute('UPDATE manual_action SET trusted=0 WHERE source_id=? AND source_version=?',(source_id,source_version))
            rows=conn.execute('SELECT DISTINCT m.* FROM message m JOIN message_observation o ON o.message_key=m.message_key WHERE o.source_id=? AND o.source_version=?',(source_id,source_version)).fetchall()
            from .derived_import import dependent_messages
            rows=list({r['message_key']:r for r in [*rows,*dependent_messages(self.db,source_id,source_version)]}.values())
            self._suspend_messages(conn,rows,emit_event=emit_event)
            # Action-only collector withdrawal also invalidates previously attributed links.
            links=conn.execute('SELECT l.message_key FROM send_link l JOIN manual_action a ON a.action_key=l.action_key WHERE a.source_id=? AND a.source_version=?',(source_id,source_version)).fetchall()
            from .send_evidence import SendEvidence
            for row in links:SendEvidence(self.db).link_success(row[0],emit_event=emit_event)
            return {'status':'revoked','affected_messages':len(rows)}

    def register_identity(self, account_key, object_kind, identity_kind, identity_value, *, source_id, source_version='1', verified=False, evidence=None):
        if object_kind not in ('person','group') or identity_kind not in ('uid','sec_uid','group_id') or (object_kind=='group') != (identity_kind=='group_id'):
            raise AppError(400,'UNSTABLE_IDENTITY','Stable person or group identity is required.')
        require_text(identity_value,'identity_value')
        if not self.get_source(source_id,source_version):
            raise AppError(409,'SOURCE_NOT_REGISTERED','Register source provenance before identities.')
        with self.db.transaction() as conn:
            account = conn.execute('SELECT verified FROM own_account WHERE account_key=?',(account_key,)).fetchone()
            if not account:
                raise AppError(409,'ACCOUNT_UNKNOWN','Account must be registered.')
            trusted = bool(verified and account[0] and self.trusted(source_id,source_version,object_kind+'_identity',account_key))
            existing = conn.execute('SELECT * FROM identity_alias WHERE account_key=? AND object_kind=? AND identity_kind=? AND identity_value=?',(account_key,object_kind,identity_kind,identity_value)).fetchone()
            if existing and existing['mapping_state']=='verified_direct':
                mapping_source=self.get_source(existing['source_id'],existing['source_version'])
                mapping_proof=json.loads(existing['evidence_json'])
                mapping_records=mapping_source['evidence'].get('direct_identity_records',[]) if mapping_source and isinstance(mapping_source['evidence'],dict) else []
                mapping_active=(self.trusted(existing['source_id'],existing['source_version'],'person_identity',account_key)
                                and self.trusted(existing['source_id'],existing['source_version'],'direct_identity_mapping',account_key)
                                and isinstance(mapping_records,list) and mapping_proof in mapping_records)
                # A single-field identity observation cannot revive a withdrawn
                # cross-key correspondence. Keep the alias pending until the
                # exact direct pair is independently approved again.
                trusted=bool(trusted and mapping_active)
            # A reviewed direct alias already determines the object. Re-observing
            # sec_uid must not create an orphan or split its canonical UID object.
            key = existing['person_key'] if existing else stable_key(object_kind,account_key,'douyin',identity_kind,identity_value)
            proof = evidence or self.get_source(source_id,source_version)['evidence']
            reject_floats(proof)
            conn.execute('INSERT INTO contact VALUES (?,?,?,?) ON CONFLICT(person_key) DO UPDATE SET verified=MAX(contact.verified,excluded.verified)',(key,account_key,object_kind,int(trusted)))
            conn.execute('INSERT INTO identity_alias(account_key,person_key,object_kind,identity_kind,identity_value,source_id,source_version,verified,evidence_json) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(account_key,object_kind,platform,identity_kind,identity_value) DO UPDATE SET verified=MAX(identity_alias.verified,excluded.verified)',(account_key,key,object_kind,identity_kind,identity_value,source_id,source_version,int(trusted),canonical_json(proof).decode()))
        return self.resolve(account_key,object_kind,identity_kind,identity_value)

    def register_direct_person_mapping(self, account_key, uid, sec_uid, *, source_id, source_version='1', observation_id):
        """Bind one independently approved same-record UID/sec_uid pair.

        This never merges existing separate histories. A source's approval must
        include the exact finite record as well as the direct-mapping grant;
        field presence, names, avatar or caller booleans are insufficient.
        """
        for field,value in (('account_key',account_key),('uid',uid),('sec_uid',sec_uid),('source_id',source_id),('source_version',source_version),('observation_id',observation_id)):
            require_text(value,field)
        proof={'account_key':account_key,'uid':uid,'sec_uid':sec_uid,'observation_id':observation_id}
        canonical=stable_key('person',account_key,'douyin','uid',uid)
        with self.db.transaction() as conn:
            # Assess current approval in the same write transaction as aliases.
            # A concurrent withdrawal cannot be overwritten by a stale grant.
            source=self.get_source(source_id,source_version)
            if not source or not self.trusted(source_id,source_version,'person_identity',account_key) or not self.trusted(source_id,source_version,'direct_identity_mapping',account_key):
                raise AppError(403,'DIRECT_MAPPING_UNVERIFIED','独立来源尚未核验直接身份对应能力')
            approved=source['evidence'].get('direct_identity_records',[]) if isinstance(source['evidence'],dict) else []
            if not isinstance(approved,list) or proof not in approved:
                raise AppError(403,'DIRECT_MAPPING_PROOF_MISSING','需要已审查的同一原始记录对应证据')
            account=conn.execute('SELECT verified FROM own_account WHERE account_key=?',(account_key,)).fetchone()
            if not account or not account[0]:raise AppError(409,'ACCOUNT_IDENTITY_UNKNOWN','账号稳定身份尚未核验')
            aliases=conn.execute("SELECT * FROM identity_alias WHERE account_key=? AND object_kind='person' AND ((identity_kind='uid' AND identity_value=?) OR (identity_kind='sec_uid' AND identity_value=?))",(account_key,uid,sec_uid)).fetchall()
            if any(row['person_key']!=canonical for row in aliases):
                raise AppError(409,'IDENTITY_RECONCILIATION_REQUIRED','已存在分离对象；需保全并审查映射迁移，不能静默合并历史')
            proof_json=canonical_json(proof).decode()
            changed=len(aliases)!=2 or any(not row['verified'] or row['mapping_state']!='verified_direct' or row['source_id']!=source_id or row['source_version']!=source_version or row['evidence_json']!=proof_json for row in aliases)
            conn.execute("INSERT INTO contact VALUES(?,?,'person',1) ON CONFLICT(person_key) DO UPDATE SET verified=1",(canonical,account_key))
            for kind,value in (('uid',uid),('sec_uid',sec_uid)):
                conn.execute("INSERT INTO identity_alias(account_key,person_key,object_kind,identity_kind,identity_value,source_id,source_version,verified,evidence_json,mapping_state) VALUES(?,?,'person',?,?,?,?,1,?,'verified_direct') ON CONFLICT(account_key,object_kind,platform,identity_kind,identity_value) DO UPDATE SET verified=1,source_id=excluded.source_id,source_version=excluded.source_version,evidence_json=excluded.evidence_json,mapping_state='verified_direct'",(account_key,canonical,kind,value,source_id,source_version,proof_json))
            if changed:conn.execute("UPDATE app_meta SET value=CAST(value AS INTEGER)+1 WHERE key='data_version'")
        return {'status':'verified_direct','person_key':canonical,'uid_alias':self.resolve(account_key,'person','uid',uid),'sec_uid_alias':self.resolve(account_key,'person','sec_uid',sec_uid),'histories_merged':False}

    def resolve(self, account_key, object_kind, identity_kind, identity_value):
        with self.db.lock:
            row = self.db.conn.execute('SELECT * FROM identity_alias WHERE account_key=? AND object_kind=? AND identity_kind=? AND identity_value=?',(account_key,object_kind,identity_kind,identity_value)).fetchone()
        if not row:
            return None
        result=dict(row)
        result['verified']=bool(result['verified'])
        result['evidence']=json.loads(result.pop('evidence_json'))
        return result
