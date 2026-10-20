import argparse, json, sys, unittest, tempfile, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path[:0] = [str(ROOT), str(ROOT/'vendor')]

class RecordedResult(unittest.TextTestResult):
    def __init__(self,*args,**kwargs):super().__init__(*args,**kwargs);self.outcomes=[]
    def addSuccess(self,test):super().addSuccess(test);self.outcomes.append({'test':test.id(),'result':'PASS'})
    def addError(self,test,err):super().addError(test,err);self.outcomes.append({'test':test.id(),'result':'ERROR'})
    def addFailure(self,test,err):super().addFailure(test,err);self.outcomes.append({'test':test.id(),'result':'FAIL'})
    def addSkip(self,test,reason):super().addSkip(test,reason);self.outcomes.append({'test':test.id(),'result':'SKIP','reason':reason})
if __name__ == '__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--stage',choices=['A','all'],default='all'); parser.add_argument('--report'); parser.add_argument('--pattern',default='test_*.py')
    args=parser.parse_args()
    scratch=ROOT.parent/'.test-runtime'
    scratch.mkdir(exist_ok=True)
    tempfile.tempdir=str(scratch)
    if args.stage=='A':
        sys.path.insert(0,str(ROOT/'tests'))
        suite=unittest.defaultTestLoader.loadTestsFromNames(['test_api_http','test_auth','test_backup','test_entrypoints','test_facts','test_keys','test_security'])
    else: suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'),pattern=args.pattern)
    def cases(value):
        for item in value:
            if isinstance(item,unittest.TestSuite):yield from cases(item)
            else:yield item
    discovered=list(cases(suite));unique={test.id():test for test in discovered}
    suite=unittest.TestSuite(unique.values())
    started=time.monotonic()
    result = unittest.TextTestRunner(verbosity=2,resultclass=RecordedResult).run(suite)
    if args.report:
        Path(args.report).write_text(json.dumps({'stage':args.stage,'tests_run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),
            'skipped':len(result.skipped),'passed':result.wasSuccessful(),'elapsed_seconds':round(time.monotonic()-started,3),
            'fixture_scope':'isolated_fictional_data','real_accounts':'NOT_RUN','python':sys.version.split()[0],
            'duplicate_ids_removed':len(discovered)-len(unique),'passed_methods':result.testsRun-len(result.failures)-len(result.errors)-len(result.skipped),
            'outcomes':result.outcomes},indent=2),encoding='utf-8')
    raise SystemExit(not result.wasSuccessful())
