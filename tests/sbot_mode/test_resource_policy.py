from types import SimpleNamespace as Obj
import pytest
from sbot.config import TeamWorkSettings, LLMSettings
from sbot.core.resource_policy import resource_decision
from tests.sbot_mode.test_missions import make_service, _two_specialists
from tests.sbot_mode.test_team_work import finish
from tests.sbot_mode.conftest import FakeProvider


def test_resource_growth_requires_new_verified_progress_and_respects_org_cap():
    policy = TeamWorkSettings(max_job_tokens=1000)
    mission = Obj(spent={'tokens': 600}, budget={'max_tokens': 500})
    nodes = [Obj(status='done'), Obj(status='pending')]
    budget, reason = resource_decision(policy, mission, nodes, {})
    assert budget['max_tokens'] == 1000 and reason == 'verified_progress'
    assert resource_decision(policy, mission, nodes, {'completed': 1})[1] == 'no_verified_progress'
    mission.spent['tokens'] = 1000
    assert resource_decision(policy, mission, nodes, {})[1] == 'organization_limit'


def test_failed_steps_and_adjustment_limit_cannot_buy_infinite_retries():
    policy = TeamWorkSettings(max_resource_adjustments=1)
    mission = Obj(spent={'tokens': 600}, budget={'max_tokens': 500})
    nodes = [Obj(status='done'), Obj(status='error', attempts=1, max_attempts=1)]
    assert resource_decision(policy, mission, nodes, {})[1] == 'step_needs_recovery'
    assert resource_decision(policy, mission, nodes, {'adjustments': 1})[1] == 'adjustment_limit'


@pytest.mark.asyncio
async def test_background_chain_renews_resources_without_user_gate(stores, tmp_path):
    user, a, b = await _two_specialists(stores, 'autobudget@sbot.ai')
    first, second = finish('checked A'), finish('checked B')
    first.usage = {'prompt_tokens': 100, 'completion_tokens': 50}
    service = make_service(stores, FakeProvider([[first], [second]]), tmp_path)
    job = await service.plan(user.id, 'Work', [
        {'id': 'a', 'bot_id': a.id, 'instruction': 'A'},
        {'id': 'b', 'bot_id': b.id, 'instruction': 'B', 'depends_on': ['a']},
    ], budget={'max_tokens': 100})
    await stores['missions'].blackboard_write(job.id, 'scope:background', True)
    assert await service.start(job.id, user.id) == 'running'
    assert await service._running[job.id] == 'completed'
    saved = await stores['missions'].get_mission(job.id, user.id)
    assert saved.spent['tokens'] == 165
    history = await stores['missions'].blackboard_read(job.id, 'policy:resources')
    assert history['adjustments'] == 1 and not history['operator_attention']


@pytest.mark.asyncio
async def test_no_progress_stops_without_asking_end_user_for_budget(stores, tmp_path):
    user, a, _ = await _two_specialists(stores, 'noprogress@sbot.ai')
    provider = FakeProvider([])
    service = make_service(stores, provider, tmp_path)
    job = await service.plan(user.id, 'Work', [{'id': 'a', 'bot_id': a.id, 'instruction': 'A'}], budget={'max_tokens': 100})
    await stores['missions'].blackboard_write(job.id, 'scope:background', True)
    await stores['missions'].update_mission(job.id, status='paused', spent={'tokens': 120})
    assert await service.start(job.id, user.id) == 'failed'
    assert not provider.calls
    history = await stores['missions'].blackboard_read(job.id, 'policy:resources')
    assert history['operator_attention']


def test_integrated_app_exposes_organization_policy(monkeypatch):
    from claw.config import Settings
    monkeypatch.setenv('CLAW_TEAM_WORK__MAX_JOB_TOKENS', '7000000')
    assert Settings(_env_file=None).team_work.max_job_tokens == 7000000
    assert LLMSettings(model_output_limits={'private/model': 2048}).model_output_limits['private/model'] == 2048

@pytest.mark.asyncio
async def test_output_recovery_adapts_within_selected_model_capacity(stores, tmp_path):
    from sbot.core.specialist import SpecialistRunner
    from sbot.providers.base import ChatResult
    from sbot.tools.finish_step import FinishStepTool
    user, a, _ = await _two_specialists(stores, 'model-cap@sbot.ai')
    class Metered(FakeProvider):
        limits = None
        async def stream_chat(self, messages, **kwargs):
            self.limits = [*(self.limits or []), kwargs['max_tokens']]
            async for event in super().stream_chat(messages, **kwargs):
                yield event
    provider = Metered([[ChatResult(content='', finish_reason='length')], [finish('done')]])
    runner = SpecialistRunner(provider, None, tmp_path, model='private/model',
        llm_settings=LLMSettings(max_tokens=1024, model_output_limits={'private/model': 1500}))
    completion = FinishStepTool(tmp_path, [])
    completion.require_record = True
    await runner.run(a, 'Task', 'Work', lambda e: None, 'cap', extra_tools=[completion])
    assert provider.limits == [1024, 1500]
    assert completion.result['status'] == 'completed'


@pytest.mark.asyncio
async def test_model_cannot_override_organization_resource_policy(stores, tmp_path):
    from sbot.core.mission_engine import InvalidGraphError
    user, a, _ = await _two_specialists(stores, 'org-cap@sbot.ai')
    service = make_service(stores, FakeProvider([]), tmp_path)
    job = await service.plan(user.id, 'Work', [{'id': 'a', 'bot_id': a.id, 'instruction': 'A'}])
    await stores['missions'].blackboard_write(job.id, 'scope:background', True)
    with pytest.raises(InvalidGraphError, match='organization policy'):
        await service.start(job.id, user.id, budget={'max_tokens': 999_999_999})
