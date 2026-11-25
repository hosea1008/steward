"""Packaged skill discovery, idle disk edits, and consistent UI/model snapshots."""
from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from steward.ego.real.console.chat import RealChat
from steward.ego.real.console.skills import SkillCatalog, SKILL_DIR
from steward.ego.real.console.server import make_server
from steward.ego.real.console.service import Console
from steward.net import call, HttpError


def write_skill(root, name='fixture-skill', description='测试描述', body='正文'):
    folder = root / name
    folder.mkdir(exist_ok=True)
    path = folder / 'SKILL.md'
    path.write_text(f'---\nname: {name}\ndescription: {description}\n---\n{body}\n')
    return path


def test_catalog_freezes_exact_instructions_and_cleans_up(tmp_path):
    path = write_skill(tmp_path)
    catalog = SkillCatalog(tmp_path)
    try:
        original = path.read_text()
        path.write_text('changed after connection')
        assert catalog.summaries() == [{'name': 'fixture-skill', 'description': '测试描述',
                                       'revision': catalog.detail('fixture-skill')['revision']}]
        assert catalog.detail('fixture-skill')['text'] == original
        directory = Path(catalog.directory)
        assert (directory/'fixture-skill/SKILL.md').read_text() == original
        assert Path(catalog.directory) == directory
        for name in ['../key.txt', 'unknown']:
            with pytest.raises(HttpError):
                catalog.detail(name)
    finally:
        catalog.close()
    assert not directory.exists()


@pytest.mark.parametrize('text', ['no frontmatter', '---\n[]\n---\nbody',
    '---\nname: mismatch\ndescription: valid\n---\nbody',
    '---\nname: fixture-skill\ndescription: valid\ndisable-model-invocation: true\n---\nbody',
    '---\nname: fixture-skill\ndescription: valid\n---\n'])
def test_malformed_or_non_model_skill_fails_at_connection(tmp_path, text):
    write_skill(tmp_path).write_text(text)
    with pytest.raises(ValueError):
        SkillCatalog(tmp_path)


def test_added_skill_automatically_reaches_snapshot_and_ui_catalog(tmp_path):
    write_skill(tmp_path, 'first-skill')
    write_skill(tmp_path, 'second-skill', body='第二个规程')
    catalog = SkillCatalog(tmp_path)
    try:
        assert [s['name'] for s in catalog.summaries()] == ['first-skill', 'second-skill']
        assert sorted(p.parent.name for p in Path(catalog.directory).glob('*/SKILL.md')) == ['first-skill', 'second-skill']
    finally:
        catalog.close()


def test_failed_snapshot_write_does_not_publish_partial_directory(tmp_path, monkeypatch):
    write_skill(tmp_path)
    catalog = SkillCatalog(tmp_path)
    attempted = []
    def fail_write(path, *args, **kwargs):
        attempted.append(path.parent.parent)
        raise OSError('fixture snapshot disk failure')
    monkeypatch.setattr(Path, 'write_text', fail_write)
    with pytest.raises(OSError):
        _ = catalog.directory
    assert catalog._snapshot is None
    assert attempted and not attempted[0].exists()


def test_console_overlay_loads_only_packaged_skills_without_code_tools(tmp_path):
    motion = SimpleNamespace(begin=lambda: None, stop=lambda: None, token='fixture', http=SimpleNamespace(url='http://127.0.0.1:1'))
    chat = RealChat({'id': 'fixture', 'name': 'Fixture'}, motion, tmp_path, tmp_path/'absent.env')
    try:
        patch = {row['id']: row for row in (json.loads(line[2:]) for line in chat.patch(motion).splitlines()) if 'id' in row}
        assert patch['tool-skill']['disabled'] is False
        cfg = patch['skill-filesystem']['config']
        assert cfg['includeDefaultRoots'] is False and cfg['watch'] is False
        assert patch['ptc-runtime']['disabled'] is True
        assert patch['workflow-ptc']['disabled'] is True
        assert 'tool-bash' not in patch and 'tool-fs' not in patch
        assert [s['name'] for s in chat.skills.summaries()] == ['factory-wam-operation', 'manhattan-navigation']
        assert (Path(cfg['bundledSkillDir'])/'manhattan-navigation/SKILL.md').read_text() == (SKILL_DIR/'manhattan-navigation/SKILL.md').read_text()
    finally:
        chat.close()


def test_skill_viewer_rejects_stale_generation_and_unknown_names():
    catalog = SkillCatalog()
    console = Console({})
    console.phase, console.generation = 'connected', 7
    console.agent = SimpleNamespace(chat=SimpleNamespace(skills=catalog))
    server = make_server(console, port=0, host='127.0.0.1').start()
    try:
        detail = call('GET', server.url+'/api/skill?generation=7&name=manhattan-navigation')
        assert detail['text'] == catalog.detail('manhattan-navigation')['text']
        for query, status in [('generation=6&name=manhattan-navigation', 409),
                              ('generation=7&name=unknown', 404), ('generation=7&name=../key.txt', 404)]:
            with pytest.raises(HttpError) as error:
                call('GET', server.url+'/api/skill?'+query)
            assert error.value.status == status
        console.phase = 'idle'
        with pytest.raises(HttpError) as error:
            call('GET', server.url+'/api/skill?generation=7&name=manhattan-navigation')
        assert error.value.status == 409
    finally:
        server.stop()
        catalog.close()


def test_saved_skill_updates_disk_snapshot_and_rejects_stale_edits(tmp_path):
    path = write_skill(tmp_path)
    catalog = SkillCatalog(tmp_path)
    original = path.read_text()
    old_directory = Path(catalog.directory)
    updated = original.replace('正文', '新规程')
    try:
        detail = catalog.save('fixture-skill', updated, original)
        assert detail['text'] == path.read_text() == updated
        assert not old_directory.exists()
        assert (Path(catalog.directory)/'fixture-skill/SKILL.md').read_text() == updated
        assert catalog.summaries()[0]['revision'] == detail['revision']
        with pytest.raises(HttpError) as error:
            catalog.save('fixture-skill', original, original)
        assert error.value.status == 409 and path.read_text() == updated
        path.write_text(updated + '外部修改\n')
        with pytest.raises(HttpError) as error:
            catalog.save('fixture-skill', original, updated)
        assert error.value.status == 409 and '外部修改' in path.read_text()
    finally: catalog.close()


@pytest.mark.parametrize('edit', ['invalid yaml', '---\nname: other\ndescription: x\n---\nbody',
    '---\nname: fixture-skill\ndescription: x\ndisable-model-invocation: true\n---\nbody', 'x'*64001])
def test_invalid_skill_save_leaves_disk_and_snapshot_unchanged(tmp_path, edit):
    path=write_skill(tmp_path); catalog=SkillCatalog(tmp_path); original=path.read_text()
    try:
        directory=catalog.directory
        with pytest.raises(HttpError): catalog.save('fixture-skill',edit,original)
        assert path.read_text()==original
        assert catalog.directory==directory
    finally: catalog.close()


def test_skill_save_failure_does_not_publish_new_text(tmp_path, monkeypatch):
    path=write_skill(tmp_path); catalog=SkillCatalog(tmp_path); original=path.read_text()
    def fail_replace(*args): raise OSError('disk failure')
    monkeypatch.setattr(Path,'replace',fail_replace)
    try:
        with pytest.raises(OSError): catalog.save('fixture-skill',original+'new\n',original)
        assert path.read_text()==catalog.detail('fixture-skill')['text']==original
        assert not list(path.parent.glob('.skill-*'))
        assert (Path(catalog.directory)/'fixture-skill/SKILL.md').read_text()==original
    finally: catalog.close()


def test_skill_editor_http_rejects_busy_stale_generation_and_paths(tmp_path):
    source=tmp_path/'skills';source.mkdir(); path=write_skill(source)
    motion=SimpleNamespace(begin=lambda:None,stop=lambda:None)
    chat=RealChat({'id':'fixture','name':'Fixture'},motion,tmp_path/'home',tmp_path/'absent.env')
    chat.skills.close(); chat.skills=SkillCatalog(source)
    console=Console({}); console.phase='connected';console.generation=7
    console.agent=SimpleNamespace(chat=chat)
    server=make_server(console,port=0,host='127.0.0.1').start()
    original=path.read_text()
    body=dict(name='fixture-skill',generation=7,previous=original,text=original+'新行\n')
    try:
        chat.agents['robot'].busy=True
        with pytest.raises(HttpError) as error: call('POST',server.url+'/api/skill',body)
        assert error.value.status==409 and path.read_text()==original
        chat.agents['robot'].busy=False
        for patch,status in [({'generation':6},409),({'name':'../key.txt'},404),({'generation':True},400)]:
            with pytest.raises(HttpError) as error: call('POST',server.url+'/api/skill',{**body,**patch})
            assert error.value.status==status
        result=call('POST',server.url+'/api/skill',body)
        assert result['text']==path.read_text()==body['text']
        assert chat.skill_updates=={'fixture-skill':result['revision']}
        assert call('GET',server.url+'/api/skill?generation=7&name=fixture-skill')['text']==body['text']
    finally:
        server.stop();chat.agents['robot'].busy=False;chat.close()
