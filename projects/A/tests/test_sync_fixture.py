"""Explicitly fabricated two-device fixture shared by phase B implementation tests."""
from pathlib import Path
import tempfile,unittest
from server.auth import Principal
from server.db import Database
from server.identity import IdentityService
from server.sync.schema import install_schema
from server.sync.crypto import PackageCodec,generate_signing_key
from server.sync.pairing import PairingStore
from server.sync.importer import Importer
from server.sync.scalar import ScalarStore

CAPS=['account_identity','person_identity','rank_identity','rank_period','rank_rows','rank_pagination','session_identity','session_viewers','session_pagination','session_boundaries','session_formal','session_catalog','session_contribution','session_watch_seconds','session_comment_count','session_attendance']
TYPES=['scalar.set','scalar.clear','scalar.add','scalar.remove','rank.revision','session.revision','session.catalog','collection.config.request','collection.config.result','fixture.fact','fixture.unsupported']
CHANNELS=['profile','collection','config','control','messages']

class TestOnlyProtector:
    def protect(self,value):return b'FICTIONAL_ONLY'+value
    def unprotect(self,value):
        if not value.startswith(b'FICTIONAL_ONLY'):raise ValueError()
        return value[len(b'FICTIONAL_ONLY'):]

def known(value='0',unit='points',precision='exact',raw=None):return {'state':'known','value':value,'unit':unit,'precision':precision,'raw_display':value if raw is None else raw}
def unknown(unit='points'):return {'state':'unknown','value':None,'unit':unit,'precision':'unknown','raw_display':''}

class SyncFixture(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)
        self.a=self.new_db('a','fictional_a');self.b=self.new_db('b','fictional_b')
        self.sa=generate_signing_key();self.sb=generate_signing_key()
        self.pa=PairingStore(self.a,TestOnlyProtector());self.pb=PairingStore(self.b,TestOnlyProtector())
        self.pa.pair('fictional_b',self.sb.public_key().public_bytes_raw(),accounts=[self.account],channels=CHANNELS,event_types=TYPES,operator_keys=['fictional_operator'])
        self.pb.pair('fictional_a',self.sa.public_key().public_bytes_raw(),accounts=[self.account],channels=CHANNELS,event_types=TYPES,operator_keys=['fictional_operator'])
        self.pa.install_key('fictional_key',b'k'*32);self.pb.install_key('fictional_key',b'k'*32)
        self.ca=PackageCodec(self.pa,'fictional_a',self.sa);self.cb=PackageCodec(self.pb,'fictional_b',self.sb)
        self.ia=Importer(self.a,self.ca);self.ib=Importer(self.b,self.cb)
        self.sca=ScalarStore(self.a);self.scb=ScalarStore(self.b)
        self.sca.register_validator('fixture.',lambda p:None);self.scb.register_validator('fixture.',lambda p:None)
        self.sca.register_handlers(self.ia);self.scb.register_handlers(self.ib)

    def new_db(self,name,device):
        db=Database(self.path/(name+'.sqlite3'),create=True);db.set_runtime_identity(device,'fictional_epoch');install_schema(db)
        ids=IdentityService(db);ids.register_source('fictional','1',capabilities=CAPS,verified=True,evidence={'synthetic':True})
        self.account=ids.register_account('fictional_anchor',source_id='fictional',verified=True)['account_key']
        self.person=ids.register_identity(self.account,'person','uid','fictional_person',source_id='fictional',verified=True)['person_key'];return db

    def tearDown(self):self.a.close();self.b.close();self.tmp.cleanup()

    def events(self,db):
        from server.sync.events import from_row
        return [from_row(r) for r in db.conn.execute('SELECT * FROM domain_event ORDER BY channel,seq')]

    def wire(self,events=None,*,channel='profile'):
        return self.ca.seal('fictional_b',channel,self.events(self.a) if events is None else events,key_id='fictional_key')

    def principal(self,device='fictional_b',role='baobao'):
        return Principal('fictional_operator',role,device,'fictional_session',frozenset({'collection:run','collection:configure','collection:request'}),frozenset({self.account}))
