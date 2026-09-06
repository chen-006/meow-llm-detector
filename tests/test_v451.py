"""v4.5.1 independent fixes: synthetic requests and temporary databases only."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_benchmarks import fixture
from test_execution import EchoModel, SECRET
from test_retention_atomic import RecordedResponse
from gpt56_vnext.benchmark import build_package
from gpt56_vnext.detector import DetectorSession
from gpt56_vnext.errors import RequestError
from gpt56_vnext.server import AppState
from gpt56_vnext.store import SQLiteStateStore
from gpt56_vnext.transport import parse_stream
from gpt56_vnext.security import SecretGuard
from gpt56_vnext.generator import calibrate_package

class LastResponseTests(unittest.TestCase):
    def test_last_id_overrides_prior_answer_error_and_done(self):
        events=[{'type':'response.created','response':{'id':'A'}},
                {'type':'response.output_text.delta','delta':'Brazil'},
                {'type':'response.completed','response':{'id':'A','status':'completed','output':[{'content':[{'type':'output_text','text':'Brazil'}]}]}},
                '[DONE]', {'type':'response.created','response':{'id':'B'}},
                {'type':'response.completed','response':{'id':'B','status':'completed','output':[{'content':[{'type':'output_text','text':'Mongolia'}]}]}}]
        raw=''.join('data: '+(v if isinstance(v,str) else json.dumps(v))+'\n\n' for v in events)
        self.assertEqual(parse_stream(raw,'gpt',SecretGuard())['answer'],'Mongolia')
        events[-1]={'type':'response.failed','response':{'id':'B','status':'failed','error':{'code':'server_error'}}}
        raw=''.join('data: '+(v if isinstance(v,str) else json.dumps(v))+'\n\n' for v in events)
        with self.assertRaises(RequestError) as got:parse_stream(raw,'gpt',SecretGuard())
        self.assertEqual(got.exception.code,'upstream_response_failed')
        self.assertTrue(got.exception.retryable)

async def immediate(_): pass

class InvalidAnswers(RecordedResponse):
    async def request(self, *args, **kwargs):
        result = await super().request(*args, **kwargs)
        result['answer'] = ''
        return result

class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_round_robin_and_failure_keeps_qualified_verdict(self):
        project,observations=fixture()
        package=calibrate_package(project,observations,{'sources':[]},{'batches':dict.fromkeys(('low','medium','high'),600)})
        class Interrupt(EchoModel):
            def __init__(self):super().__init__();self.order=[]
            async def request(self,mode,base,key,model,cell,**kwargs):
                if self.calls==6:raise RuntimeError('synthetic interruption')
                self.order.append(cell['id'])
                return await super().request(mode,base,key,model,cell,**kwargs)
        with tempfile.TemporaryDirectory() as folder, SQLiteStateStore(Path(folder)/'db.sqlite3') as store:
            sender=Interrupt();session=DetectorSession(store,'order',package,{'base_url':'https://fixture.invalid/v1','claimed_model':'a','sample_ratio':.6,'runtime':{'workers':1}},SECRET,transport=sender)
            report=await session.run()
            self.assertEqual(sender.order,['ab','ac','bc']*2)
            self.assertEqual(report['operational_status'],'error')
            self.assertEqual(report['fingerprint']['quality_status'],'sufficient')
            self.assertEqual(report['fingerprint']['color'],'green')
    async def test_old_false_retry_flags_do_not_skip_normal_errors(self):
        project,observations=fixture()
        for code in ['response_incomplete','response_refused','unexpected_tool','invalid_usage','redirect_rejected']:
            sender=EchoModel();sender.failures=[RequestError(code,retryable=False)]*18
            with tempfile.TemporaryDirectory() as folder, SQLiteStateStore(Path(folder)/'db.sqlite3') as store:
                session=DetectorSession(store,'retry',build_package(project,observations),
                    {'base_url':'https://fixture.invalid/v1','claimed_model':'a','sample_ratio':.6,'runtime':{'workers':1,'retries':1}},SECRET,transport=sender)
                with patch('gpt56_vnext.executor.asyncio.sleep',immediate):await session.run()
                self.assertEqual(sender.calls,18,code)
                decisions=[v for v in store.events('retry') if v['event']=='attempt_decision']
                self.assertEqual(sum(v['will_retry'] for v in decisions),9)

    async def test_invalid_answers_bounded_and_all_evidence_retained(self):
        project, observations = fixture()
        with tempfile.TemporaryDirectory() as folder, SQLiteStateStore(Path(folder)/'db.sqlite3') as store:
            config = {'base_url':'https://fixture.invalid/v1','claimed_model':'a','sample_ratio':.6,
                      'runtime':{'workers':1,'retries':2,'retain_raw':True}}
            sender = InvalidAnswers()
            session = DetectorSession(store,'invalid',build_package(project,observations),config,SECRET,transport=sender)
            with patch('gpt56_vnext.executor.asyncio.sleep',immediate): report = await session.run()
            self.assertEqual(sender.calls,27)
            self.assertEqual(report['progress']['valid_samples'],0)
            self.assertEqual(report['fingerprint']['quality_status'],'insufficient_valid_samples')
            self.assertTrue(all(row['category']=='__INVALID_OUTPUT__' for row in report['results']))
            self.assertEqual(store.retained_exchanges('invalid')['coverage'],{'attempts':27,'retained':27})
            self.assertNotIn(SECRET,str(store.retained_exchanges('invalid')))
            self.assertTrue(all(row['exchange']['error']['code']=='invalid_answer' for row in store.retained_exchanges('invalid')['records']))
            # Reopening an exhausted job cannot replenish its budget.
            retry = InvalidAnswers()
            await DetectorSession(store,'invalid',build_package(project,observations),config,SECRET,transport=retry).run()
            self.assertEqual(retry.calls,0)

    async def test_invalid_then_valid_counts_one_sample(self):
        class OnceInvalid(RecordedResponse):
            async def request(self,*args,**kwargs):
                result=await super().request(*args,**kwargs)
                if self.calls==1:result['answer']=''
                return result
        project,observations=fixture()
        with tempfile.TemporaryDirectory() as folder, SQLiteStateStore(Path(folder)/'db.sqlite3') as store:
            sender=OnceInvalid();config={'base_url':'https://fixture.invalid/v1','claimed_model':'a','sample_ratio':.6,
                    'runtime':{'workers':1,'retries':2,'retain_raw':True}}
            session=DetectorSession(store,'good',build_package(project,observations),config,SECRET,transport=sender)
            with patch('gpt56_vnext.executor.asyncio.sleep',immediate):report=await session.run()
            self.assertEqual((sender.calls,report['progress']['valid_samples']),(10,9))
            self.assertEqual(len(report['results']),9)
            self.assertEqual(store.retained_exchanges('good')['coverage'],{'attempts':10,'retained':10})

    async def test_parser_failures_use_the_same_budget(self):
        project,observations=fixture();sender=EchoModel()
        sender.failures=[RequestError('invalid_stream')]*27
        with tempfile.TemporaryDirectory() as folder, SQLiteStateStore(Path(folder)/'db.sqlite3') as store:
            session=DetectorSession(store,'parse',build_package(project,observations),
                {'base_url':'https://fixture.invalid/v1','claimed_model':'a','sample_ratio':.6,'runtime':{'workers':1,'retries':2}},SECRET,transport=sender)
            with patch('gpt56_vnext.executor.asyncio.sleep',immediate):report=await session.run()
            self.assertEqual((sender.calls,report['progress']['errors']),(27,9))
            self.assertEqual(report['progress']['valid_samples'],0)

class PolicyTests(unittest.TestCase):
    def test_new_api_task_uses60_and_old_report_and_config_remain(self):
        project,observations=fixture();package=build_package(project,observations)
        with tempfile.TemporaryDirectory() as folder:
            app=AppState(folder,bundled=False)
            try:
                oldconfig={'base_url':'https://fixture.invalid/v1','claimed_model':'a'}
                old=DetectorSession(app.store,'old',package,oldconfig,SECRET,transport=EchoModel())
                original=old.report();app.store.save_report('old',original)
                frozen=app.store.session('old')['config']
                sender=EchoModel()
                async def start():
                    identity=await app.start_run('detection',{**oldconfig,'key':SECRET,'package_id':package['id'],'package_version':package['version']})
                    await app.active[identity][1]
                    return identity
                with patch.object(app.catalog,'get',return_value=package),patch.object(app.catalog,'local',return_value=[{'id':package['id'],'version':package['version'],'publisher':'local'}]),patch('gpt56_vnext.server.AsyncTransport',return_value=sender):
                    identity=app.call(start())
                self.assertEqual(app.store.session(identity)['config']['sample_ratio'],.6)
                self.assertEqual(app.store.report(identity)['fingerprint']['sample_policy']['version'],'60-percent-v1')
                self.assertEqual(app.store.report('old'),original)
                self.assertEqual(app.store.session('old')['config'],frozen)
                self.assertNotIn('sample_ratio',frozen)
                self.assertEqual(original['fingerprint']['sample_policy']['version'],'legacy-90')
            finally:app.close()

if __name__=='__main__':unittest.main()
