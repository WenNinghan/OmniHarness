
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from test_m4_collaboration import InspectStageModel, prepare_v4
from test_m4_runtime import ToolModel

from omniharness.artifacts import ArtifactStore
from omniharness.m3_delegation import HierarchicalDelegation
from omniharness.m3_stages import build_stage_graph
from omniharness.m4_contract import EVIDENCE_STAGE_VERSIONS, EVIDENCE_WORKFLOW_VERSION, M4_PLUGIN_VERSIONS
from omniharness.m4_runtime import M4AgentHost, build_execution_agent
from omniharness.models import ArtifactVersion, Run, RunEvent
from omniharness.stage_contract import STAGE_KEYS, STAGE_ROLE_BINDINGS
from omniharness.worker import WorkerLock


class V6Stage(InspectStageModel):
    async def ainvoke(self,messages):
        if self.stage!='review':
            return await super().ainvoke(messages)
        packets=[json.loads(m.content)['host_evidence'] for m in messages if getattr(m,'id',None)=='omni-evidence:review']
        target=next((x for x in (packets[0]['operations'] if packets else []) if x['tool']=='workspace_patch'),None)
        refs=[{'operation_id':target['operation_id']}] if target else []
        return AIMessage(content=json.dumps({'summary':'review','findings':[{'finding_id':'f1','location':'main.py','severity':'INFO','description':'test claim','model_status':'PASS','recommendation':'inspect','candidate_refs':refs}],'unverified':[]}))

@pytest.mark.asyncio
async def test_v6_full_graph_and_same_run_view(environment,tmp_path):
    worker,run_id,workspace,broker,sandbox,project=prepare_v4(environment,tmp_path)
    with worker.sessions.begin() as db:
        run=db.get(Run,run_id)
        agent={**run.config_snapshot['agent'],'workflow_version':EVIDENCE_WORKFLOW_VERSION}
        for role in STAGE_KEYS:
            agent['roles'][role]={**agent['roles'][role],'role_version':EVIDENCE_STAGE_VERSIONS[role]}
        run.config_snapshot={**run.config_snapshot,'agent':agent}
        run.workflow_version=EVIDENCE_WORKFLOW_VERSION
    with WorkerLock(worker.engine,worker.settings) as owner:
        worker.lock=owner
        host=M4AgentHost(worker,run_id,EVIDENCE_WORKFLOW_VERSION,role_versions=EVIDENCE_STAGE_VERSIONS,plugin_versions=M4_PLUGIN_VERSIONS,role_bindings=STAGE_ROLE_BINDINGS)
        scheduler=HierarchicalDelegation()
        execution=build_execution_agent(model=ToolModel(),host=host,workspace=workspace,broker=broker,sandbox=sandbox,artifacts=ArtifactStore(worker.settings.data_dir/'artifacts'),scheduler=scheduler)
        graph=build_stage_graph(models={role:V6Stage(role) for role in STAGE_KEYS},hooks=host,checkpointer=InMemorySaver(),workflow_version=EVIDENCE_WORKFLOW_VERSION,execution_agent=execution,execution_strategy='compact')
        try:
            result=await graph.ainvoke({'messages':[HumanMessage(content='fix and test')]},{'configurable':{'thread_id':run_id},'recursion_limit':100},durability='sync')
        finally:
            await scheduler.aclose()
    assert not result.get('__interrupt__')
    assert result['review_data']['claim_support']=='UNVERIFIED'
    with worker.sessions() as db:
        events=list(db.scalars(select(RunEvent).where(RunEvent.run_id==run_id,RunEvent.event_type=='review.evidence_recorded')))
        assert len(events)==1
        report_id=events[0].payload['report_artifact_id']
        assert db.get(ArtifactVersion,report_id).kind=='review_reference_report_v1'
    client=environment[3]
    response=client.get(f'/api/v1/runs/{run_id}/evidence-view')
    assert response.status_code==200,response.text
    data=response.json()
    assert data['recording_capabilities']['review']=='supported'
    assert any(i['event_type']=='review.evidence_recorded' and not i.get('unsupported_schema') for i in data['items'])
    response=client.get(f'/api/v1/runs/{run_id}/artifacts/{report_id}/content')
    assert response.status_code==200,response.text
    payload=response.json()
    assert payload['claim_support']=='UNVERIFIED'
    assert payload['findings'][0]['model_status']=='PASS'
    assert len(payload['findings'][0]['references'])==1
    assert payload['findings'][0]['references'][0]['reference_status']=='VALIDATED'
    assert all(x['claim_support']=='UNVERIFIED' for x in payload['findings'][0]['references'])

@pytest.mark.asyncio
async def test_v6_dependent_tasks_emit_handoff(environment,tmp_path):
    class TwoTaskStage(V6Stage):
        async def ainvoke(self,messages):
            if self.stage=='plan':
                return AIMessage(content=json.dumps({'goal':'repair','constraints':['keep api'],'steps':[
                    {'id':'s1','task':'first','acceptance':'done','write_paths':['main.py']},
                    {'id':'s2','task':'second','acceptance':'done','depends_on':['s1'],'write_paths':['main.py']}
                ]}))
            return await super().ainvoke(messages)
    worker,run_id,workspace,broker,sandbox,project=prepare_v4(environment,tmp_path)
    with worker.sessions.begin() as db:
        run=db.get(Run,run_id)
        agent={**run.config_snapshot['agent'],'workflow_version':EVIDENCE_WORKFLOW_VERSION}
        for role in STAGE_KEYS:
            agent['roles'][role]={**agent['roles'][role],'role_version':EVIDENCE_STAGE_VERSIONS[role]}
        run.config_snapshot={**run.config_snapshot,'agent':agent}
        run.workflow_version=EVIDENCE_WORKFLOW_VERSION
    with WorkerLock(worker.engine,worker.settings) as owner:
        worker.lock=owner
        host=M4AgentHost(worker,run_id,EVIDENCE_WORKFLOW_VERSION,role_versions=EVIDENCE_STAGE_VERSIONS,plugin_versions=M4_PLUGIN_VERSIONS,role_bindings=STAGE_ROLE_BINDINGS)
        scheduler=HierarchicalDelegation()
        execution=build_execution_agent(model=ToolModel(),host=host,workspace=workspace,broker=broker,sandbox=sandbox,artifacts=ArtifactStore(worker.settings.data_dir/'artifacts'),scheduler=scheduler)
        graph=build_stage_graph(models={role:TwoTaskStage(role) for role in STAGE_KEYS},hooks=host,checkpointer=InMemorySaver(),workflow_version=EVIDENCE_WORKFLOW_VERSION,execution_agent=execution,execution_strategy='compact')
        try:
            result=await graph.ainvoke({'messages':[HumanMessage(content='fix')]},{'configurable':{'thread_id':run_id},'recursion_limit':100},durability='sync')
        finally:
            await scheduler.aclose()
    assert len(result['task_results'])==2
    with worker.sessions() as db:
        events=list(db.scalars(select(RunEvent).where(RunEvent.run_id==run_id,RunEvent.event_type=='handoff.prepared')))
        assert len(events)==1
        assert events[0].payload['operation_coverage']=='UNVERIFIED'
        assert events[0].payload['validation']['claim_support']=='UNVERIFIED'
    response=environment[3].get(f'/api/v1/runs/{run_id}/evidence-view')
    assert response.status_code==200,response.text
    assert any(x['event_type']=='handoff.prepared' and not x.get('unsupported_schema') for x in response.json()['items'])

def test_v6_fixed_snapshot_and_cross_run_artifact_denial(environment,tmp_path):
    from conftest import create_chat

    from omniharness.handoff_contract import build_handoff_v6, handoff_event_payload
    from omniharness.services import add_event
    worker,run_id,workspace,broker,sandbox,project=prepare_v4(environment,tmp_path)
    with worker.sessions.begin() as db:
        run=db.get(Run,run_id)
        run.workflow_version=EVIDENCE_WORKFLOW_VERSION
        store=ArtifactStore(worker.settings.data_dir/'artifacts')
        art=store.put(db,run_id=run_id,workspace_id=run.workspace_id,kind='task_result',name='example',content='report')
        for i in range(3):
            env=build_handoff_v6(run_id=run_id,workspace_id=run.workspace_id,workflow_version=EVIDENCE_WORKFLOW_VERSION,
                source_task_id=f's{i}',target_task_id='t',goal_message_id='g',artifact_id=art['id'],
                artifact_sha256=art['sha256'],report_text='report',operations=[],task_fingerprint='a'*64)
            add_event(db,run,'handoff.prepared',handoff_event_payload(env))
    client=environment[3]
    first=client.get(f'/api/v1/runs/{run_id}/evidence-view?limit=1').json()
    assert first['snapshot_total']==3 and not first['snapshot_complete'] and first['next_cursor']
    cursor=first['next_cursor']
    pos=len(cursor)//2
    bad=cursor[:pos]+('A' if cursor[pos]!='A' else 'B')+cursor[pos+1:]
    assert client.get(f'/api/v1/runs/{run_id}/evidence-view',params={'cursor':bad}).status_code==409
    with worker.sessions.begin() as db:
        run=db.get(Run,run_id)
        env=build_handoff_v6(run_id=run_id,workspace_id=run.workspace_id,workflow_version=EVIDENCE_WORKFLOW_VERSION,
            source_task_id='later',target_task_id='t',goal_message_id='g',artifact_id=art['id'],
            artifact_sha256=art['sha256'],report_text='report',operations=[],task_fingerprint='a'*64)
        add_event(db,run,'handoff.prepared',handoff_event_payload(env))
    seen=list(first['items'])
    page=first
    while page['next_cursor']:
        response=client.get(f'/api/v1/runs/{run_id}/evidence-view',params={'cursor':page['next_cursor'],'limit':1})
        assert response.status_code==200,response.text
        page=response.json()
        seen.extend(page['items'])
    assert page['snapshot_complete'] and len(seen)==3 and page['watermark']==first['watermark']
    latest=client.get(f'/api/v1/runs/{run_id}/evidence-view').json()
    assert latest['snapshot_total']==4 and latest['watermark']>first['watermark']
    _,_,other=create_chat(client)
    denied=client.get(f"/api/v1/runs/{other['run_id']}/artifacts/{art['id']}/content")
    assert denied.status_code==404

@pytest.mark.asyncio
async def test_v6_forged_and_unmapped_citations_remain_unverified(environment,tmp_path):
    class FalseRefStage(V6Stage):
        async def ainvoke(self,messages):
            if self.stage!='review':
                return await super().ainvoke(messages)
            packet=next(json.loads(m.content)['host_evidence'] for m in messages if getattr(m,'id',None)=='omni-evidence:review')
            artifact=next((a for op in packet['operations'] for a in op['artifacts'] if a.get('sha256')),None)
            assert artifact is not None
            refs=[{'operation_id':'f'*64},{'artifact_id':artifact['artifact_id'],'expected_sha256':artifact['sha256'],'start':0,'end':1}]
            return AIMessage(content=json.dumps({'summary':'fake refs','findings':[{'finding_id':'f1','location':'main.py','severity':'INFO','description':'claim','model_status':'PASS','recommendation':'inspect','candidate_refs':refs}],'unverified':[]}))
    worker,run_id,workspace,broker,sandbox,project=prepare_v4(environment,tmp_path)
    with worker.sessions.begin() as db:
        run=db.get(Run,run_id)
        agent={**run.config_snapshot['agent'],'workflow_version':EVIDENCE_WORKFLOW_VERSION}
        for role in STAGE_KEYS:
            agent['roles'][role]={**agent['roles'][role],'role_version':EVIDENCE_STAGE_VERSIONS[role]}
        run.config_snapshot={**run.config_snapshot,'agent':agent}
        run.workflow_version=EVIDENCE_WORKFLOW_VERSION
    with WorkerLock(worker.engine,worker.settings) as owner:
        worker.lock=owner
        host=M4AgentHost(worker,run_id,EVIDENCE_WORKFLOW_VERSION,role_versions=EVIDENCE_STAGE_VERSIONS,plugin_versions=M4_PLUGIN_VERSIONS,role_bindings=STAGE_ROLE_BINDINGS)
        scheduler=HierarchicalDelegation()
        execution=build_execution_agent(model=ToolModel(),host=host,workspace=workspace,broker=broker,sandbox=sandbox,artifacts=ArtifactStore(worker.settings.data_dir/'artifacts'),scheduler=scheduler)
        graph=build_stage_graph(models={role:FalseRefStage(role) for role in STAGE_KEYS},hooks=host,checkpointer=InMemorySaver(),workflow_version=EVIDENCE_WORKFLOW_VERSION,execution_agent=execution,execution_strategy='compact')
        try:
            result=await graph.ainvoke({'messages':[HumanMessage(content='fix')]},{'configurable':{'thread_id':run_id},'recursion_limit':100},durability='sync')
        finally:
            await scheduler.aclose()
    references=result['review_data']['findings'][0]['references']
    assert [r['reference_status'] for r in references]==['UNVERIFIED','UNVERIFIED']
    assert references[1]['reason']=='RANGE_NOT_MAPPABLE_TO_BOUND_INPUT'
    assert result['review_data']['claim_support']=='UNVERIFIED'
