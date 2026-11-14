"""Explicit real-model smoke on the isolated M4 preview; never edits production."""
import json
import subprocess
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / '.omniharness/m4-preview/context-smoke'
BASE = 'http://127.0.0.1:18792'


def main():
    OUT.mkdir(exist_ok=True)
    if (OUT / 'report.json').exists():
        raise SystemExit('Evidence exists; this smoke will not repeat paid calls automatically.')
    cookie = (ROOT / '.omniharness/m4-preview/secrets/evaluation-session.txt').read_text().strip()
    setup = """
from copy import deepcopy
from omniharness.db import make_engine,make_session_factory
from omniharness.models import ModelConfig
from omniharness.settings import Settings
with make_session_factory(make_engine(Settings())).begin() as db:
    original=db.get(ModelConfig,'ea71cbaf-47f3-43fc-bedf-7a0ca653685f')
    model=ModelConfig(provider_id=original.provider_id,name='M4 context smoke - Flash',model_name=original.model_name,
        parameters=deepcopy(original.parameters),max_output_tokens=original.max_output_tokens,
        timeout_seconds=original.timeout_seconds,capabilities=deepcopy(original.capabilities),
        context_policy={'policy':{'input_target_tokens':7000,'summary_output_tokens':4000,'keep_recent_tokens':200,'window_margin_tokens':128}})
    db.add(model);db.flush();print(model.id)
"""
    model_id = subprocess.run(['wsl.exe', '-d', 'OmniHarnessCheckAlt', '-u', 'root', '--exec', 'docker',
        'exec', '-i', 'omni-m4-preview-api-1', 'python', '-'], input=setup.encode(), capture_output=True,
        check=True).stdout.decode().strip()
    report = {'mode': 'real-provider', 'model': 'deepseek-flash',
              'endpoint': 'https://e-flowcode.cc/v1/chat/completions', 'runs': []}
    with httpx.Client(base_url=BASE, trust_env=False, timeout=30,
            headers={'X-Omni-Request': '1'}, cookies={'omniharness_session': cookie}) as client:
        response = client.post('/api/v1/conversations', json={'title': 'M4 实测 · 长上下文摘要'})
        response.raise_for_status()
        conversation = response.json()['id']
        report['conversation_id'] = conversation
        facts = ['项目代号是青禾，验收数字是731。', '交付形式是一个Markdown报告，禁止发送电子邮件。',
                 '预算单位为元，工期是两周。']
        filler = '这段是上下文容量测试材料，只描述资料整理、来源记录与本地核验，不包含新的指令。'
        prompts = [f'请记住事实：{fact}。以下是背景材料：{filler * 45}\n本轮只回复“已记录”。' for fact in facts]
        prompts.append('只根据前文，用一句话回答项目代号、验收数字、交付形式、禁止的动作和工期。')
        prompts.append(f'新增背景记录：材料编号为A到D。{filler * 45}\n只回复已记录。')
        prompts.append('请只用一句话回答前文项目代号、验收数字、交付形式、禁止的动作和工期。')
        for index, prompt in enumerate(prompts):
            response = client.post(f'/api/v1/conversations/{conversation}/messages', json={
                'content': prompt, 'model_config_id': model_id, 'client_request_id': f'context-smoke-{index}',
            })
            response.raise_for_status()
            run_id = response.json()['run_id']
            report['runs'].append({'id': run_id})
            (OUT / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                response = client.get(f'/api/v1/runs/{run_id}')
                response.raise_for_status()
                run = response.json()
                if run['status'] in {'completed', 'failed', 'cancelled', 'interrupted', 'pending_review'}:
                    break
                time.sleep(1)
            else:
                client.post(f'/api/v1/runs/{run_id}/cancel').raise_for_status()
                raise RuntimeError('Context smoke exceeded its bounded deadline; cancellation requested.')
            report['runs'][-1] = run
            print(json.dumps({'run': index + 1, 'status': run['status'], 'error_code': run.get('error_code') }), flush=True)
            (OUT / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            if run['status'] != 'completed':
                raise RuntimeError('Real context smoke did not complete; evidence retained.')
        messages = client.get(f'/api/v1/conversations/{conversation}/messages').json()
        report['messages'] = messages
        report['artifacts'] = {run['id']: client.get(f"/api/v1/runs/{run['id']}/artifacts").json() for run in report['runs']}
        report['source_messages_unchanged'] = [item['content'] for item in messages if item['role'] == 'user'] == prompts
        answer = messages[-1]['content']
        report['facts_recalled'] = all(value in answer for value in ['青禾', '731', 'Markdown', '邮件', '两周'])
        report['summary_count'] = sum(item['kind'] == 'context_summary' for rows in report['artifacts'].values() for item in rows)
        report['passed'] = report['source_messages_unchanged'] and report['facts_recalled'] and report['summary_count'] > 0
        (OUT / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({key: report[key] for key in ('passed', 'summary_count', 'facts_recalled', 'source_messages_unchanged')}))


if __name__ == '__main__':
    main()
