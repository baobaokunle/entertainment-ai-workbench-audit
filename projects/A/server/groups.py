from .common import AppError
from .sync.scalar import ScalarStore
from .profile import ProfileService
import uuid

class GroupService:
    def __init__(self,db): self.db=db;self.scalars=ScalarStore(db);self.profiles=ProfileService(db)

    def set_membership(self,account,person,group_id,member,*,expected_revision,actor):
        if not isinstance(group_id,str) or not group_id or len(group_id)>100 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in group_id) or type(member)is not bool: raise AppError(422,'INVALID_GROUP','Group ID and membership boolean are required.')
        return self.scalars.set(account,person,'group.'+group_id,member,expected_revision=expected_revision,actor=actor,operation='add' if member else 'remove')

    def create(self,account,name,*,actor):
        group_id=uuid.uuid4().hex
        result=self.scalars.set(account,account,'custom_group.'+group_id,{'name':name,'deleted':False},expected_revision=[],actor=actor)
        return {'group_id':group_id,**result}

    def definitions(self,account):
        fields=self.scalars.list_fields(account,account);items=[]
        for field,state in fields.items():
            if not field.startswith('custom_group.'):continue
            value=state['effective_value'] or {};deleted=any(isinstance(c['value'],dict) and c['value'].get('deleted') is True for c in state['candidates'])
            items.append({'group_id':field[13:],'name':value.get('name','待核对分组'),'deleted':deleted,'state':state['state'],'revision':state['revision'],'candidates':state['candidates']})
        return {'items':sorted(items,key=lambda x:(x['name'],x['group_id']))}

    def update(self,account,group_id,*,name=None,deleted=False,expected_revision,actor):
        current=self.scalars.get(account,account,'custom_group.'+group_id)
        if not current['revision']:raise AppError(404,'GROUP_NOT_FOUND','分组不存在')
        if not deleted and any(isinstance(c['value'],dict) and c['value'].get('deleted') for c in current['candidates']):raise AppError(409,'GROUP_DELETED','已删除分组不能被旧改名复活')
        value=current['effective_value'] or {}
        result=self.scalars.set(account,account,'custom_group.'+group_id,{'name':name if name is not None else value.get('name','待核对分组'),'deleted':deleted},expected_revision=expected_revision,actor=actor)
        return {'group_id':group_id,**result}

    def groups_for(self,account,person,tier='unknown'):
        profile=self.profiles.get(account,person);fields=profile['fields'];system=[]
        if profile['object_kind']=='group': system.append('group_chat')
        elif tier in ('S','A','B','C','D','E'): system.append(tier)
        for tag in ('clip','vip','star'):
            if fields['formal_tag.'+tag]['effective_value'] is True: system.append(tag)
        definitions={x['group_id']:x for x in self.definitions(account)['items']}
        custom=sorted(f[6:] for f,s in fields.items() if f.startswith('group.') and s['effective_value'] is True and not definitions.get(f[6:],{}).get('deleted'))
        return {'system':system,'custom':custom,'custom_details':[definitions.get(g,{'group_id':g,'name':'未命名分组','state':'unknown'}) for g in custom],'conflicts':[f for f,s in fields.items() if f.startswith('group.') and s['state']=='conflict']}
