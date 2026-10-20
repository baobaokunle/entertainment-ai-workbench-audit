# AUDIT-ONLY: sanitized copy; not a production runtime or authorization.
"""One fixed, authorized C-01 snapshot. Prints aggregates only."""
from pathlib import Path
import datetime
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from local_snapshot import copy_exclusive, fixed_snapshot, readonly_connection, write_json_new

PROJECT = Path(__file__).resolve().parents[3]
CROOT = Path(__file__).resolve().parents[1]
EXPECTED = 'c7fa1e39726c6377d3727529422f87e179f86484f9ca6b00ab53c52f48855694'
SOURCE = CROOT / 'audit-inputs' / 'synthetic-source.db'
BATCH = CROOT / 'audit-inputs' / 'synthetic-batch'
INPUT = BATCH / 'input' / 'synthetic-snapshot.db'


def main():
    result = {'at': datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(),
              'source': str(SOURCE), 'copy': str(INPUT), 'copy_check': copy_exclusive(SOURCE, INPUT, EXPECTED),
              'diagnostics': {}}
    with fixed_snapshot(INPUT, EXPECTED):
        conn = readonly_connection(INPUT)
        try:
            queries = {
                'flags_bounded': "SELECT CASE WHEN json_extract(raw_data,'$.is_recalled')=0 THEN 'zero' ELSE 'other' END,CASE WHEN json_extract(raw_data,'$.visible')=0 THEN 'zero' WHEN json_extract(raw_data,'$.visible')=1 THEN 'one' ELSE 'other' END,COUNT(*) FROM messages GROUP BY 1,2",
            }
            for key, sql in queries.items():
                result['diagnostics'][key] = {'sql': sql, 'rows': conn.execute(sql).fetchall()}
        finally:
            conn.close()
    write_json_new(CROOT / 'docs/evidence/C01_snapshot.json', result)
    print(json.dumps({'copy_check': result['copy_check'], 'complete': True}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Do not echo exception messages which may contain row data or identities.
        print(json.dumps({'failed': type(error).__name__}))
        raise SystemExit(1)
