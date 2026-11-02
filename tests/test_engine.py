import asyncio
import io
import json
import time
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from engine.app import create_app
from engine.config import Settings
from engine.schemas import DramaDocument,ImageRequest,VideoRequest
from engine.store import Store,new_id,dumps
from engine.tools import ToolRegistry,calculate
from engine.workflows import video_frames,video_graph,image_graph


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data=tmp_path),run_worker=False)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        c.headers["Authorization"]="Bearer "+app.state.security.local_token
        yield c


def test_private_state_requires_authentication(app):
    with TestClient(app) as c:
        assert c.get('/health').status_code==200
        assert c.get('/api/state').status_code==401
        assert c.post('/api/images',json={'prompt':'test'}).status_code==401


def test_pairing_is_single_use_revocable_and_not_admin(client,app):
    invite=app.state.security.pairing('127.0.0.1')
    token=client.post('/pair',json={'name':'iPad','code':invite['code']}).json()
    assert client.post('/pair',json={'name':'duplicate','code':invite['code']}).status_code==400
    with TestClient(app) as remote:
        remote.headers['Authorization']='Bearer '+token['token']
        assert remote.get('/api/state').status_code==200
        assert remote.post('/api/pairing').status_code==403
        assert remote.delete('/api/devices/'+token['device_id']).status_code==403
        assert client.delete('/api/devices/'+token['device_id']).status_code==200
        assert remote.get('/api/state').status_code==401


def test_pairing_expiry_and_rate_limit(app):
    security=app.state.security
    invitation=security.pairing('127.0.0.1')
    security.codes[invitation['code']]=time.time()-1
    with pytest.raises(ValueError,match='过期'):security.pair(invitation['code'],'test','source')
    for _ in range(7):
        with pytest.raises(ValueError):security.pair('invalid','test','source')
    with pytest.raises(ValueError,match='频繁'):security.pair('invalid','test','source')


def test_chat_retry_idempotency_and_cancellation(client):
    session=client.post('/api/sessions',json={}).json()
    path='/api/sessions/'+session['id']+'/messages'
    body={'content':'Hello','request_id':'request-123456'}
    first=client.post(path,json=body);assert first.status_code==200
    same=client.post(path,json=body);assert same.json()['id']==first.json()['id']
    assert len(client.get(path).json())==2
    assert client.post(path,json={**body,'content':'different'}).status_code==409
    assert client.post(path,json={**body,'request_id':'another-1234'}).status_code==409
    assert client.delete('/api/sessions/'+session['id']).status_code==409
    cancelled=client.post('/api/jobs/'+first.json()['id']+'/cancel').json()
    assert cancelled['status']=='cancelled'
    retry=client.post('/api/jobs/'+first.json()['id']+'/retry').json()
    assert retry['status']=='queued'
    assert retry['payload']['message_id']!=first.json()['payload']['message_id']


def test_recovery_keeps_pending_and_marks_inflight(app):
    store=app.state.store
    first=store.create_job('image',{'prompt':'a'})
    second=store.create_job('image',{'prompt':'b'})
    assert store.claim_next()['id']==first['id']
    store.recover()
    assert store.get_job(first['id'])['status']=='interrupted'
    assert store.get_job(second['id'])['status']=='queued'


def test_chat_retry_does_not_answer_against_later_messages(client):
    session=client.post('/api/sessions',json={}).json()
    path='/api/sessions/'+session['id']+'/messages'
    first=client.post(path,json={'content':'First question','request_id':'first-request'}).json()
    client.post('/api/jobs/'+first['id']+'/cancel')
    second=client.post(path,json={'content':'Second question','request_id':'second-request'}).json()
    client.post('/api/jobs/'+second['id']+'/cancel')
    assert client.post('/api/jobs/'+first['id']+'/retry').status_code==409
    assert client.post('/api/jobs/'+second['id']+'/retry').status_code==200


def test_project_optimistic_lock_and_export_guard(client):
    project=client.post('/api/projects',json={'title':'测试','brief':'一段两镜头短剧'}).json()
    doc={'logline':'一个故事','style':'写实','characters':[],'shots':[{'id':'s1','title':'开场','description':'一个房间','duration':3}]}
    body={'document':doc,'expected_updated_at':project['updated_at']}
    assert client.put('/api/projects/'+project['id'],json=body).status_code==200
    assert client.put('/api/projects/'+project['id'],json=body).status_code==409
    assert client.post('/api/projects/'+project['id']+'/export').status_code==409


def test_upload_rejects_non_images_and_sanitizes_metadata(client):
    assert client.post('/api/assets',files={'file':('bad.png',b'not an image','image/png')}).status_code==400
    buf=io.BytesIO();Image.new('RGB',(80,60),'green').save(buf,format='PNG')
    response=client.post('/api/assets',files={'file':('../../hello.png',buf.getvalue(),'image/png')})
    assert response.status_code==200
    asset=response.json();assert asset['name']=='hello.png'
    out=client.get('/api/assets/'+asset['id']+'/file');assert out.status_code==200
    with Image.open(io.BytesIO(out.content)) as image:assert image.size==(80,60)


def test_workspace_is_scoped_and_never_overwrites(app):
    tools=app.state.worker.tools
    for bad in ('../secret','/etc/passwd','.env','nested/file'):
        with pytest.raises(ValueError):tools.execute('read_file',{'name':bad})
    one=tools.execute('save_file',{'name':'note.md','content':'first'})
    two=tools.execute('save_file',{'name':'note.md','content':'second'})
    assert one['saved']!=two['saved']
    assert tools.execute('read_file',{'name':one['saved']})['content']=='first'
    outside=app.state.worker.settings.data.parent/'outside.txt';outside.write_text('private')
    (app.state.worker.settings.data/'workspace/link.txt').symlink_to(outside)
    with pytest.raises(ValueError):tools.execute('read_file',{'name':'link.txt'})


def test_calculator_has_no_code_execution():
    assert calculate('(128+64)*3')==576
    for expression in ('__import__("os").getcwd()','9**99999','[1][0]','"abc"*10'):
        with pytest.raises(ValueError):calculate(expression)


def test_media_contracts():
    assert video_frames(5)==124
    assert video_frames(3)==73
    for seconds in (1,2,4,5,10,15):assert video_frames(seconds)%17==5
    with pytest.raises(ValueError):VideoRequest(prompt='x',width=355)
    with pytest.raises(ValueError):ImageRequest(prompt='x',width=1536,height=1536)
    video=video_graph(VideoRequest(prompt='test').model_dump(),'job','rewritten','first.png')
    assert video['8']['inputs']['first_frame']==['20',0]
    assert 'last_frame' not in video['8']['inputs']
    assert video['5']['inputs']['type']=='minimax'
    image=image_graph(ImageRequest(prompt='test').model_dump(),'job',['character.png'])
    assert image['6']['inputs']['latent_image']==['4',2]
    assert '5' not in image


def test_task_idempotency_requires_same_payload(app):
    store=app.state.store
    one=store.create_job('image',{'prompt':'one'},'key')
    assert store.create_job('image',{'prompt':'one'},'key')['id']==one['id']
    with pytest.raises(ValueError):store.create_job('image',{'prompt':'two'},'key')


def test_character_references_must_exist():
    with pytest.raises(ValueError):DramaDocument.model_validate({'characters':[],'shots':[{'id':'s1','title':'x','description':'x','character_ids':['missing']}]})


@pytest.mark.asyncio
async def test_worker_cancel_stops_provider_and_persists(app):
    worker=app.state.worker
    stopped=asyncio.Event()
    async def slow(job):await asyncio.sleep(60)
    async def shutdown():stopped.set()
    worker.handle=slow;worker.runtime.shutdown=shutdown
    job=app.state.store.create_job('image',{'prompt':'test'})
    await worker.start()
    for _ in range(100):
        if worker.active_id==job['id']:break
        await asyncio.sleep(.01)
    worker.cancel(job['id'])
    await asyncio.wait_for(stopped.wait(),2)
    await asyncio.sleep(.01)
    assert app.state.store.get_job(job['id'])['status']=='cancelled'
    await worker.close()
