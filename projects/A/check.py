"""Read-only diagnostics: no imports of application modules, mkdir, probes or DB migrations."""
import argparse, hashlib, json, sqlite3, sys
from pathlib import Path
sys.dont_write_bytecode = True
def diagnose(root):
    root = Path(root).resolve()
    result = {'python':sys.version.split()[0],'sqlite':sqlite3.sqlite_version,'runtime_exists':root.exists(),
              'schema_version':None,'writes_allowed':False,'storage_protection':'unverified'}
    db = root/'runtime'/'facts.sqlite3'
    if db.is_file():
        # immutable=1 prevents side files; do not pretend a live WAL is a complete snapshot.
        connection = sqlite3.connect(db.as_uri()+'?mode=ro&immutable=1',uri=True)
        try: result['schema_version'] = connection.execute('PRAGMA user_version').fetchone()[0]
        finally: connection.close()
        result['schema_inspection']='base-file-only; active WAL not inspected'
    return result
if __name__ == '__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--data-root',required=True)
    print(json.dumps(diagnose(parser.parse_args().data_root),ensure_ascii=False,indent=2))
