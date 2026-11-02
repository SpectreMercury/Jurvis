import json
import hashlib
import httpx
import pytest
from engine.catalog import MediaCatalog
from engine.config import Settings
from engine.schemas import VideoRequest
from engine.workflows import video_graph


def test_motion_and_turbo_settings_reach_the_sampling_graph():
    p=VideoRequest(prompt='A bird flies',model_profile='convrot_int8',sampling_profile='turbo_v2',turbo_steps=4,motion_strength=-2).model_dump()
    graph=video_graph(p,'job','prompt')
    assert graph['1']['class_type']=='UNETLoader'
    assert graph['22']['inputs']['strength_model']==-2
    assert graph['3']['inputs']['model']==['22',0]
    assert graph['4']['inputs']['shift_video']==6
    assert graph['12']['inputs']['steps']==4
    p['sampling_profile']='dasiwa_v33';graph=video_graph(p,'job','prompt')
    assert '2' not in graph and graph['22']['inputs']['model']==['1',0]
    assert graph['12']['inputs']['steps']==25
    assert graph['11']['inputs']['sampler_name']=='res_multistep'
    with pytest.raises(ValueError):VideoRequest(prompt='x',motion_strength=9)


@pytest.mark.asyncio
async def test_installed_files_require_exact_hash_and_size(tmp_path):
    settings=Settings(data=tmp_path/'data');settings.prepare();catalog=MediaCatalog(settings)
    payload=b'fixture';path=tmp_path/'weights.safetensors';path.write_bytes(payload)
    item={'id':'fixture','title':'fixture','directory':str(tmp_path),'filename':path.name,'bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest(),'source':'https://example.com','kind':'motion'}
    catalog.manifest={'components':[item]}
    assert not catalog.installed(item)
    await catalog.install('fixture');assert catalog.installed(item)
    path.write_bytes(b'wrong!!');assert not catalog.installed(item)
    with pytest.raises(ValueError,match='校验失败'):await catalog.install('fixture')


@pytest.mark.asyncio
async def test_range_download_resumes_in_order_and_rejects_wrong_range(tmp_path,monkeypatch):
    settings=Settings(data=tmp_path/'data');settings.prepare();catalog=MediaCatalog(settings)
    data=bytes(range(256))*(8*1024*1024//256)+b'last chunk'*10
    item={'bytes':len(data),'download_url':'https://example.com/model','title':'fixture'}
    partial=tmp_path/'model.part';partial.write_bytes(data[:13])
    ranges=[]
    def handler(request):
        start,end=map(int,request.headers['range'].removeprefix('bytes=').split('-'))
        ranges.append((start,end))
        return httpx.Response(206,headers={'Content-Range':f'bytes {start}-{end}/{len(data)}'},content=data[start:end+1])
    real_client=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:real_client(**kwargs,transport=httpx.MockTransport(handler)))
    await catalog._download_ranges(item,partial,13,None)
    assert partial.read_bytes()==data
    assert len(ranges)==2 and sorted(ranges)[0][0]==13
    partial.write_bytes(data[:13])
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:real_client(**kwargs,transport=httpx.MockTransport(lambda request:httpx.Response(200,content=b'wrong response'))))
    with pytest.raises(ValueError,match='分段请求'):
        await catalog._download_ranges(item,partial,13,None)
    assert partial.read_bytes()==data[:13]
