"""Run the real console chat against caller-owned mock model and motion endpoints."""
import json
import os
from pathlib import Path
import sys
import shutil
import time
from types import SimpleNamespace

from steward.ego.real.console.chat import RealChat
from steward.ego.real.console.skills import SkillCatalog, SKILL_DIR

home, endpoint, gateway = sys.argv[1:]
os.environ['DEEPSEEK_API_KEY'] = 'fixture'
os.environ['DEEPSEEK_BASE_URL'] = gateway
motion = SimpleNamespace(http=SimpleNamespace(url=endpoint), token='fixture-token', begin=lambda: None, stop=lambda: None)
chat = RealChat({'id': 'fixture-robot', 'name': 'Fixture robot'}, motion, Path(home), Path(home)/'absent.env')
source=Path(home)/'editable-skills'
shutil.copytree(SKILL_DIR,source)
chat.skills.close()
chat.skills=SkillCatalog(source)


def turn(text):
    chat.send('robot', text)
    deadline = time.monotonic()+60
    while chat.state()['agents']['robot']['busy']:
        if time.monotonic() >= deadline:
            raise TimeoutError('fixture conversation did not settle')
        time.sleep(.02)
    result = chat.state()
    errors = [m for m in result['messages'] if m['role'] == 'error']
    if errors:
        raise RuntimeError(str(errors))
    return result


try:
    chat.set_context('模拟物料位置由操作员提供；只用于测试。', '')
    first = turn('在模拟设备依次调用已有导航与升降工具。此前模拟故障已检查，操作员确认已停稳，请先清除软件故障。')
    original=chat.skills.detail('factory-wam-operation')['text']
    chat.save_skill('factory-wam-operation', original+'\n编辑后的模拟规程标记。\n', original)
    assert (source/'factory-wam-operation/SKILL.md').read_text().endswith('编辑后的模拟规程标记。\n')
    chat.set_context('', chat.context())
    second = turn('只读确认当前上下文已清空。')
    chat.set_context('新会话保留的任务资料', '')
    chat.new_session()
    third = turn('新会话仅检查独立历史。')
    print(json.dumps({'first': first, 'second': second, 'third': third}, ensure_ascii=False))
finally:
    chat.close()
