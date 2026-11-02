import asyncio
import json
from pathlib import Path
import pytest
from engine.coding import Coding, digest, permitted
from engine.config import Settings
from engine.store import Store


@pytest.fixture
def coding(tmp_path):
    settings=Settings(data=tmp_path/'data');settings.prepare()
    return Coding(settings,Store(settings.data/'db.sqlite3'))


def test_only_authorized_text_sources_are_readable(coding,tmp_path):
    root=tmp_path/'project';root.mkdir();(root/'app.py').write_text('value=1')
    (root/'.env').write_text('SECRET=value')
    outside=tmp_path/'outside.py';outside.write_text('private')
    (root/'escape.py').symlink_to(outside)
    project=coding.connect(str(root))
    assert coding.read(project['id'],'app.py')=='value=1'
    assert [x['path'] for x in coding.tree(project['id'])['files']]==['app.py']
    for name in ('../outside.py','.env','escape.py','/etc/passwd','credentials.json'):
        with pytest.raises(ValueError):coding.read(project['id'],name)
    with pytest.raises(ValueError):coding.read('unregistered','app.py')


def proposal(coding,project,changes):
    job=coding.store.create_job('code',{'code_project_id':project['id']})
    coding.store.update_job(job['id'],status='succeeded',result={'summary':'fix','code_project_id':project['id'],'changes':[{'path':name,'before':before,'after':after,'base_hash':digest(before),'diff':'diff'} for name,before,after in changes]})
    return job['id']


def test_apply_is_idempotent_and_undo_preserves_other_edits(coding,tmp_path):
    root=tmp_path/'project';root.mkdir();p=root/'main.py';p.write_text('old')
    project=coding.connect(str(root));job=proposal(coding,project,[('main.py','old','new'),('added.py',None,'extra')])
    coding.apply(job);coding.apply(job)
    assert p.read_text()=='new' and (root/'added.py').read_text()=='extra'
    p.write_text('user edit')
    with pytest.raises(ValueError,match='已被修改'):coding.apply(job,revert=True)
    assert (root/'added.py').exists()
    p.write_text('new');coding.apply(job,revert=True)
    assert p.read_text()=='old' and not (root/'added.py').exists()


def test_conflict_checks_all_files_before_any_mutation(coding,tmp_path):
    root=tmp_path/'project';root.mkdir();(root/'a.py').write_text('a');(root/'b.py').write_text('changed')
    project=coding.connect(str(root));job=proposal(coding,project,[('a.py','a','edited'),('b.py','b','edited')])
    with pytest.raises(ValueError):coding.apply(job)
    assert (root/'a.py').read_text()=='a'


@pytest.mark.asyncio
async def test_model_cannot_overwrite_a_file_it_did_not_read(coding,tmp_path):
    root=tmp_path/'project';root.mkdir();(root/'a.py').write_text('a');(root/'b.py').write_text('b')
    project=coding.connect(str(root))
    class Model:
        async def complete(self,*args,**kwargs):return json.dumps({'summary':'change','changes':[{'path':'b.py','content':'edited'}]})
    with pytest.raises(ValueError,match='未阅读'):
        await coding.propose({'code_project_id':project['id'],'instruction':'fix','files':['a.py']},Model())
    assert (root/'b.py').read_text()=='b'


@pytest.mark.asyncio
async def test_test_runner_records_real_failure(coding,tmp_path):
    root=tmp_path/'project';(root/'tests').mkdir(parents=True)
    (root/'tests/test_fail.py').write_text('def test_failure():\n    assert 2 + 2 == 5\n')
    project=coding.connect(str(root));events=[]
    async def progress(output,command):events.append(output)
    result=await coding.run({'code_project_id':project['id'],'command_id':'pytest'},progress)
    assert result['passed'] is False and result['exit_code']==1 and 'FAILED' in result['output'] and events
    with pytest.raises(ValueError):await coding.run({'code_project_id':project['id'],'command_id':'rm -rf'},progress)
