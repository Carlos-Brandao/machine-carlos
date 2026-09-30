from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import patch
from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware
from machine_admin.db import get_db
from machine_admin.models import Dataset, Job, Municipality, PortalCredential
from machine_admin.product_admin import TEMPLATES, install_product_admin

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime.now(UTC)
DATASET = SimpleNamespace(id=2, municipality_slug='paulista', display_name='Base teste', original_filename='teste.xlsx', row_count=12, status='ready', created_at=NOW)
MUNICIPALITY = SimpleNamespace(slug='paulista', name='Paulista', max_workers=2)
CREDENTIAL = SimpleNamespace(id=3, municipality_slug='paulista', label='Principal', status='active', cooldown_until=None, last_error=None)
JOB = Job(id=4, municipality_slug='paulista', dataset_id=2, status='queued', selected_credential_ids=[3], max_parallel_accounts=1, total_items=12, completed_items=0, failed_items=0, found_items=0, not_found_items=0, retryable_items=0, permanent_items=0, created_at=NOW)

class Session:
    def scalars(self, statement):
        model = statement.column_descriptions[0].get('entity')
        return {Dataset:[DATASET], Municipality:[MUNICIPALITY], PortalCredential:[CREDENTIAL]}.get(model, [])
    def scalar(self, statement): return JOB
    def execute(self, statement): return []
    def get(self, model, key): return {Dataset:DATASET,Municipality:MUNICIPALITY,Job:JOB}.get(model)
    def commit(self): pass
    def rollback(self): pass

class AdminSmoke(TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.add_middleware(SessionMiddleware, secret_key='x'*40)
        self.app.mount('/static', StaticFiles(directory=str(ROOT / 'machine_admin' / 'static')),name='static')
        self.app.dependency_overrides[get_db] = lambda: Session()
        def auth(request,session,write_access=False,admin_only=False):
            role=request.headers.get('x-test-role')
            if not role: return RedirectResponse('/login',303)
            if write_access and role == 'viewer': raise HTTPException(403,'read only')
            return SimpleNamespace(id=1, role=role,display_name='Operador')
        def csrf(request,candidate):
            if candidate!='valid': raise HTTPException(403,'csrf')
        def context(request,user,**values): return {'request':request,'user':user,'csrf_token':'valid','flash':None,'now_utc':NOW,**values}
        install_product_admin(self.app,None,auth,context,csrf,lambda *args:'Pausada',lambda *args:{'executable':True,'reason':'Pronta'})
        self.client=TestClient(self.app)
    def test_all_templates_compile(self):
        for path in (ROOT / 'machine_admin' / 'templates').glob('*.html'): TEMPLATES.env.get_template(path.name)
    def test_new_detail_settings_schedules_render(self):
        for route in ['/admin/consultations/new','/admin/consultations/4','/admin/settings','/admin/schedules']:
            response=self.client.get(route,headers={'x-test-role':'admin'})
            self.assertEqual(200,response.status_code,(route,response.text))
            self.assertNotIn('Telegram',response.text)
    def test_anonymous_redirect_and_json401(self):
        self.assertEqual(303,self.client.get('/admin/consultations/new',follow_redirects=False).status_code)
        self.assertEqual(401,self.client.get('/admin/consultations/4/status').status_code)
    def test_viewer_cannot_create_and_csrf_checked(self):
        payload={'dataset_id':'2','credential_ids':'3','max_parallel_accounts':'1','submission_key':'one','csrf':'bad'}
        self.assertEqual(403,self.client.post('/admin/consultations/new',data=payload,headers={'x-test-role':'viewer'}).status_code)
        self.assertEqual(403,self.client.post('/admin/consultations/new',data=payload,headers={'x-test-role':'admin'}).status_code)
    def test_create_delegates_selection_and_idempotency(self):
        payload={'dataset_id':'2','credential_ids':'3','max_parallel_accounts':'1','submission_key':'same-request','csrf':'valid'}
        with patch('machine_admin.operations.create_execution',return_value=JOB) as create:
            result=self.client.post('/admin/consultations/new',data=payload,headers={'x-test-role':'operator'},follow_redirects=False)
        self.assertEqual(303,result.status_code)
        self.assertEqual('/admin/consultations/4',result.headers['location'])
        self.assertEqual([3],create.call_args.kwargs['selected_credential_ids'])
        self.assertEqual('same-request',create.call_args.kwargs['idempotency_key'])
    def test_status_is_no_store_and_contains_no_password(self):
        response=self.client.get('/admin/consultations/4/status',headers={'x-test-role':'viewer'})
        self.assertEqual(200,response.status_code)
        self.assertEqual('no-store',response.headers['cache-control'])
        self.assertEqual('Principal',response.json()['accounts'][0]['label'])
        self.assertNotIn('password',response.text)
    def test_results_pagination_and_bad_filter(self):
        with patch('machine_admin.operations.result_page',return_value={'items':[],'next_cursor':None}) as results:
            response=self.client.get('/admin/consultations/4/results?cursor=opaque-page&outcome=found',headers={'x-test-role':'viewer'})
        self.assertEqual(200,response.status_code)
        self.assertEqual('opaque-page',results.call_args.kwargs['cursor'])
        self.assertTrue(results.call_args.kwargs['newest_first'])
        with patch('machine_admin.operations.result_page',side_effect=ValueError('bad filter')):
            response=self.client.get('/admin/consultations/4/results?outcome=bad',headers={'x-test-role':'viewer'})
        self.assertEqual(400,response.status_code)

if __name__=='__main__': main()
