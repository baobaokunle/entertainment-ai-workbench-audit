"""Finite versioned configuration: no arbitrary commands, cloud providers or bind hosts."""
import json, re
from pathlib import Path
from .common import AppError

VERSIONS = {'app':'2.0.0-candidate13','extension':'2.0.0','schema':2,'protocol':1,'rule':'1.0.0','adapter':'unverified-1'}
PORT = 8801
DEFAULTS = {'version':1,'device_key':None,'device_role':None,'extension_ids':[],
            'source_mode':'unverified','storage_protection':'unverified',
            'collection_enabled':False,'collection_time':'04:00','timezone':'Asia/Shanghai',
            'ai_enabled':False,'ai_provider':'local','model_url':'http://127.0.0.1:11434',
            'model_name':'qwen3:4b-instruct-2507-q4_K_M','local_access':None}


def validate_local_access(value):
    if value is None:return
    fields={'format','workspace_root','binding','selection','note'}
    if (type(value) is not dict or not fields<=set(value) or set(value)-fields-{'reply_on_demand'} or
            value['format'] not in ('dfa-local-notes-1','dfa-local-access-1') or
            ('reply_on_demand' in value and (value['format']!='dfa-local-access-1' or value['reply_on_demand'] is not True)) or
            value['note'] is not True or type(value['workspace_root']) is not str or
            not 1<=len(value['workspace_root'])<=1024):
        raise AppError(400,'LOCAL_ACCESS_INVALID','本地备注需要明确工作区、唯一来源及有限能力。')
    binding=value['binding'];selection=value['selection']
    if (type(binding) is not dict or set(binding)!={'account_key','source_id','source_version','account_namespace'} or
            any(type(v) is not str or not v.strip() or len(v)>256 or '\x00' in v for v in binding.values()) or
            type(selection) is not dict or set(selection)!={'format','source_path','snapshot_sha256','preview_sha256',
                'account_key','account_namespace','row_limit','selection'} or
            selection['format']!='dfa-approved-local-import-1' or selection['selection']!='first_page_binary_message_id' or
            selection['account_key']!=binding['account_key'] or selection['account_namespace']!=binding['account_namespace'] or
            type(selection['source_path']) is not str or not 1<=len(selection['source_path'])<=1024 or
            type(selection['row_limit']) is not int or not 1<=selection['row_limit']<=100 or
            any(type(selection[k]) is not str or not re.fullmatch('[0-9a-f]{64}',selection[k])
                for k in ('snapshot_sha256','preview_sha256'))):
        raise AppError(400,'LOCAL_ACCESS_INVALID','本地备注必须绑定原批准的精确来源，不能使用通配或未知字段。')

def validate_settings(value):
    if not isinstance(value, dict) or set(value)-set(DEFAULTS):
        raise AppError(400,'unsupported_configuration','配置包含不允许的字段')
    merged = DEFAULTS | value
    validate_local_access(merged['local_access'])
    if merged['ai_provider'] != 'local' or merged['model_url'] != DEFAULTS['model_url']:
        raise AppError(400,'local_only','只允许固定环回本地模型')
    if merged['timezone'] != 'Asia/Shanghai' or merged['device_role'] not in (None,'manager','baobao'):
        raise AppError(400,'invalid_configuration','设备角色或时区无效')
    if merged['source_mode'] not in ('fictional','unverified') or merged['storage_protection'] not in ('unverified','verified-volume'):
        raise AppError(400,'invalid_configuration','保护或来源状态无效')
    if not isinstance(merged['extension_ids'],list) or any(not isinstance(x,str) or not re.fullmatch('[a-p]{32}',x) for x in merged['extension_ids']):
        raise AppError(400,'invalid_extension','扩展ID必须明确登记')
    if type(merged['version']) is not int or merged['version'] != 1:
        raise AppError(400,'invalid_configuration','配置版本无效')
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',merged['collection_time']):
        raise AppError(400,'invalid_configuration','计划时间无效')
    for field in ('ai_enabled','collection_enabled'):
        if type(merged[field]) is not bool: raise AppError(400,'invalid_configuration','启停值无效')
    if merged['device_key'] is not None and (not isinstance(merged['device_key'],str) or not re.fullmatch('[A-Za-z0-9_-]{8,80}',merged['device_key'])):
        raise AppError(400,'invalid_device','设备ID无效')
    return merged

def read_settings(path):
    path = Path(path)
    if not path.exists(): return dict(DEFAULTS)
    try: return validate_settings(json.loads(path.read_text(encoding='utf-8')))
    except (ValueError,OSError): raise AppError(503,'configuration_invalid','配置不可读取；不会回退到云服务') from None

def write_settings(path, value):
    validated = validate_settings(value)
    path = Path(path); temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(validated,ensure_ascii=False,indent=2),encoding='utf-8')
    temp.replace(path)
