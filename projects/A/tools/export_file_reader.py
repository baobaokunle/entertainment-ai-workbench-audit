"""Explicit bounded offline file reader. No discovery, account access or persistence."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys
import time

MAX_BYTES = 8 * 1024 * 1024
MAX_ROWS = 2000
MAX_FIELD = 256000
FORMATS = ('db-json', 'db-jsonl', 'chatlab-json', 'chatlab-jsonl', 'sqlite')
COLUMNS = {'msg_id':'TEXT','conv_id':'TEXT','sender_uid':'TEXT','sender_name':'TEXT',
           'content':'TEXT','msg_type':'INTEGER','media_url':'TEXT','media_local_path':'TEXT',
           'timestamp':'INTEGER','seq':'INTEGER','raw_data':'TEXT','ref_msg':'TEXT'}
SELECTED = ('msg_id','conv_id','sender_uid','content','msg_type','timestamp','raw_data','ref_msg')

def _sidecars(path):
    if any(Path(str(path) + suffix).exists() for suffix in ('-wal','-shm','-journal')):
        raise ValueError('sqlite_sidecar_present_use_clean_export_copy')

def _snapshot(path, limit):
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('file_byte_budget_exceeded')
    return data, hashlib.sha256(data).hexdigest()

def _sqlite_rows(path, max_rows, max_field, query_seconds, query_steps):
    _sidecars(path)
    conn = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)
    try:
        conn.enable_load_extension(False)
        conn.execute('PRAGMA query_only=ON')
        conn.execute('PRAGMA trusted_schema=OFF')
        deadline = time.monotonic() + query_seconds
        steps = 0
        def progress():
            nonlocal steps
            steps += 100
            return int(steps > query_steps or time.monotonic() > deadline)
        conn.set_progress_handler(progress, 100)
        table = conn.execute("SELECT type, sql FROM sqlite_schema WHERE name='messages'").fetchall()
        if len(table) != 1 or table[0][0] != 'table' or 'VIRTUAL' in (table[0][1] or '').upper():
            raise ValueError('unknown_messages_schema')
        info = conn.execute("PRAGMA table_xinfo('messages')").fetchall()
        if {r[1]: r[2].upper() for r in info} != COLUMNS or any(r[6] for r in info):
            raise ValueError('unknown_messages_schema')
        if next(r for r in info if r[1]=='msg_id')[5] != 1 or not next(r for r in info if r[1]=='conv_id')[3]:
            raise ValueError('unknown_messages_constraints')
        def authorize(action, arg1, arg2, database, origin):
            if origin is not None:
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_SELECT:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ and arg1 == 'messages' and arg2 in SELECTED:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        conn.set_authorizer(authorize)
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, max_field + 4096)
        # No user SQL, views, sorting expressions or credentials/media columns.
        cursor = conn.execute('SELECT msg_id,conv_id,sender_uid,content,msg_type,timestamp,raw_data,ref_msg FROM messages LIMIT ?', (max_rows + 1,))
        rows = []
        output_bytes = 0
        for values in cursor:
            if len(rows) == max_rows:
                raise ValueError('row_budget_exceeded')
            row = dict(zip(SELECTED, values))
            for name, value in row.items():
                if COLUMNS[name] == 'TEXT' and value is not None and not isinstance(value, str):
                    raise ValueError('sqlite_storage_type_mismatch:' + name)
                if isinstance(value, str) and len(value) > max_field:
                    raise ValueError('field_budget_exceeded:' + name)
                if COLUMNS[name] == 'INTEGER' and value is not None and type(value) is not int:
                    raise ValueError('sqlite_storage_type_mismatch:' + name)
            if not row['msg_id'] or not row['conv_id']:
                raise ValueError('missing_platform_string_id')
            if row['msg_type'] is not None and abs(row['msg_type']) > 9007199254740991:
                raise ValueError('unsafe_numeric_msg_type')
            # Preserve exact INTEGER representation across JSON -> JavaScript.
            if row['timestamp'] is not None:
                row['timestamp'] = str(row['timestamp'])
            output_bytes += len(json.dumps(row,ensure_ascii=False).encode('utf-8'))
            if output_bytes > MAX_BYTES:
                raise ValueError('aggregate_row_byte_budget_exceeded')
            rows.append(row)
        return rows
    finally:
        conn.close()

def read_export_file(path, *, format, account_namespace, source_kind='unverified_export',
                     max_bytes=MAX_BYTES, max_rows=MAX_ROWS, max_field=MAX_FIELD,
                     query_seconds=2.0, query_steps=1000000):
    if format not in FORMATS:
        raise ValueError('explicit_format_required')
    if not isinstance(account_namespace, str) or not account_namespace.strip() or len(account_namespace)>512:
        raise ValueError('explicit_account_namespace_required')
    if source_kind not in ('fictional','unverified_export'):
        raise ValueError('source_kind_unverified_only')
    for value, ceiling in ((max_bytes,MAX_BYTES),(max_rows,MAX_ROWS),(max_field,MAX_FIELD),(query_steps,1000000)):
        if type(value) is not int or not 0 < value <= ceiling:
            raise ValueError('invalid_budget')
    if not isinstance(query_seconds,(int,float)) or not math.isfinite(query_seconds) or not 0 < query_seconds <= 2:
        raise ValueError('invalid_query_budget')
    file = Path(path)
    if not file.is_absolute():
        raise ValueError('absolute_explicit_path_required')
    if file.is_symlink() or not file.is_file():
        raise ValueError('regular_existing_file_required')
    file = file.resolve(strict=True)
    if format == 'sqlite':
        _sidecars(file)
    data, digest = _snapshot(file,max_bytes)
    result = {'kind':format, 'metadata':{'sha256':digest,'size_bytes':len(data),
              'account_namespace':account_namespace,'source_kind':source_kind,
              'persisted':False,'source_approved':False,'coverage':'unknown',
              'id_precision_before_export':'unknown'}}
    if format == 'sqlite':
        if not data.startswith(b'SQLite format 3\x00'):
            raise ValueError('invalid_sqlite_header')
        result['rows'] = _sqlite_rows(file,max_rows,max_field,query_seconds,query_steps)
        result['offline_preview'] = {'persisted':False,'coverage':'unknown','identity':'unverified',
                                     'body_revisions':'defer_to_row_adapter','conflicts':'defer_to_row_adapter'}
        _sidecars(file)
    else:
        result['text'] = data.decode('utf-8-sig',errors='strict')
    if _snapshot(file,max_bytes)[1] != digest:
        raise ValueError('input_changed_during_read')
    return result

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--file',required=True)
    parser.add_argument('--format',choices=FORMATS,required=True)
    parser.add_argument('--account-namespace',required=True)
    parser.add_argument('--source-kind',choices=('fictional','unverified_export'),default='unverified_export')
    args=parser.parse_args()
    try:
        result=read_export_file(args.file,format=args.format,account_namespace=args.account_namespace,source_kind=args.source_kind)
    except UnicodeError:
        parser.exit(2, 'offline_reader_rejected: invalid_utf8\n')
    except ValueError as error:
        parser.exit(2, 'offline_reader_rejected: '+str(error)+'\n')
    except OSError:
        parser.exit(2, 'offline_reader_rejected: file_access_error\n')
    except sqlite3.Error:
        parser.exit(2, 'offline_reader_rejected: sqlite_read_or_budget_error\n')
    output=json.dumps(result,ensure_ascii=False,allow_nan=False).encode('utf-8')
    if len(output)>16*1024*1024:
        parser.exit(2, 'offline_reader_rejected: output_byte_budget_exceeded\n')
    sys.stdout.buffer.write(output+b'\n')

if __name__ == '__main__':
    main()
