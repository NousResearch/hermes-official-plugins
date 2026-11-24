"""One Graph batch carrying the same no-id notification twice runs its durable job once."""
import asyncio

import pytest

from gateway.config import GatewayConfig
from teams_pipeline_plugin.pipeline import TeamsMeetingPipeline
from teams_pipeline_plugin.store import TeamsPipelineStore


@pytest.mark.anyio
async def test_duplicate_no_id_notifications_in_one_batch_run_the_job_once(monkeypatch, tmp_path):
    from teams_pipeline_plugin import runtime as runtime_module

    class Adapter:
        def __init__(self):
            self._background_tasks = set()

        def set_notification_scheduler(self, scheduler):
            self.scheduler = scheduler

    pipeline = TeamsMeetingPipeline(graph_client=object(), store=TeamsPipelineStore(tmp_path / 'store.json'))
    release, runs = asyncio.Event(), []

    async def run_job(job_id):  # the sink writes (Notion page creation) happen in here
        runs.append(job_id)
        await release.wait()
    monkeypatch.setattr(pipeline, 'run_job', run_job)
    monkeypatch.setattr(runtime_module, 'build_pipeline_runtime', lambda gateway_config: pipeline)
    monkeypatch.setattr(runtime_module, 'load_gateway_config', lambda: GatewayConfig(platforms={}))
    adapter = Adapter()
    assert runtime_module.bind_adapter(adapter) is True
    notification = {'changeType': 'created', 'resource': 'communications/onlineMeetings/m1',
                    'resourceData': {'id': 'm1'}}  # Graph's notification ``id`` is optional
    for _ in range(2):  # the adapter schedules each entry of the batch's ``value`` in turn
        await adapter.scheduler(dict(notification), object())
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(*list(adapter._background_tasks))
    assert len(runs) == 1
