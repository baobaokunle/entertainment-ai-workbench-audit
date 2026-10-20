"""Explicit offline, body-free B projection plan. Prints only safe counts/codes."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path[:0] = [str(ROOT), str(ROOT / 'vendor')]

from server.backend_candidates import plan_files, summarize
from server.common import AppError


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's usual message includes the offending argument/path/value.
        raise AppError(400, 'BACKEND_ARGUMENTS_INVALID', '离线候选参数不完整或不受支持。')


def main(argv=None):
    parser = SafeParser(prog='backend-candidate-plan', description='只读核对B后台无正文投影；不解密、不登记身份、不写业务库')
    parser.add_argument('--run-file', required=True, help='明确选择的任务JSON绝对路径')
    parser.add_argument('--run-sha256', required=True, help='任务文件完整SHA256')
    parser.add_argument('--observations-file', required=True, help='最多100条无正文观察外层JSONL绝对路径')
    parser.add_argument('--observations-sha256', required=True, help='观察JSONL完整SHA256')
    try:
        args = parser.parse_args(argv)
        result = summarize(plan_files(args.run_file, args.run_sha256,
                                      args.observations_file, args.observations_sha256))
    except Exception as error:
        code = error.code if isinstance(error, AppError) and error.code.startswith('BACKEND_') else 'BACKEND_PLAN_FAILED'
        print(json.dumps({'ok': False, 'code': code, 'business_writes': 0}, ensure_ascii=False))
        return 1
    print(json.dumps({'ok': True, **result}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
