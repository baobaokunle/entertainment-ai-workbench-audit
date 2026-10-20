"""Single-request private adapter; no source calls and no caller-selected paths."""
from .security import CollectorError
from .contract import encode

class TodayPrivateBridge:
    def __init__(self,runtime,*,target_budget,downloader):
        if type(target_budget) is not int or not 1<=target_budget<=20:
            raise CollectorError('CHAT_TODAY_BUDGET')
        self.runtime=runtime;self.downloader=downloader
        self.targets=list(runtime.started['target_ids'])[:target_budget]
        self.position=0;self.request=None;self.closed=False;self.blocked=False
        self.committed=0

    def call(self,op,args):
        if op=='finish':
            if self.closed: raise CollectorError('CHAT_TODAY_CLOSED')
            self.closed=True
            return self.runtime.close()
        if self.blocked or self.closed:raise CollectorError('CHAT_TODAY_TRANSPORT_STOPPED')
        try:
            if not isinstance(args,dict):raise CollectorError('CHAT_TODAY_FRAME')
            if op=='claim' and not args:
                if self.request is not None:raise CollectorError('CHAT_TODAY_FRAME')
                if self.position>=len(self.targets):return None
                self.request=self.runtime.claim(self.targets[self.position])
                if self.request.get('entry_kind')!='cache_only':raise CollectorError('CHAT_TODAY_ENTRY')
                return self.request
            if op=='page' and set(args)=={'page_id','response'} and self.request:
                if args['page_id']!=self.request['page_id']:raise CollectorError('CHAT_TODAY_FRAME')
                result=self.runtime.commit_page(args['page_id'],args['response'])
                self.committed+=1
                return result
            if op=='media' and set(args)=={'token'} and self.request:
                return self.runtime.process_media(args['token'],downloader=self.downloader)
            if op=='advance' and not args and self.request:
                result=self.runtime.advance();self.position+=1;self.request=None
                return result
            if op=='source_failure' and set(args)=={'page_id','code'} and self.request:
                if args['page_id']!=self.request['page_id']:raise CollectorError('CHAT_TODAY_FRAME')
                result=self.runtime.source_failure(args['page_id'],args['code']);self.blocked=True
                return result
            raise CollectorError('CHAT_TODAY_FRAME')
        except Exception:
            self.blocked=True
            raise CollectorError('CHAT_TODAY_TRANSPORT_STOPPED') from None
