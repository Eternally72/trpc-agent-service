"""A running process is ready only while its own successful heartbeat is fresh."""

import json
from pathlib import Path
import time

import pytest

from trpc_service.healthcheck import is_ready, write_health
from trpc_service.agent.nodes import RuntimeNodeHeartbeatService


def test_probe_rejects_missing_corrupt_stale_draining_and_restarted_process(tmp_path: Path):
    path = tmp_path / "health.json"
    assert not is_ready(path, role="worker", max_age=20)
    path.write_text("invalid json")
    assert not is_ready(path, role="worker", max_age=20)
    write_health(path, "worker", "active")
    assert is_ready(path, role="worker", max_age=20)
    assert not is_ready(path, role="channel", max_age=20)
    original = json.loads(path.read_text())
    for change in ({
            "checked_at": time.monotonic() - 21
    }, {
            "checked_at": float("nan")
    }, {
            "state": "draining"
    }, {
            "process_start": "previous-process"
    }):
        path.write_text(json.dumps({**original, **change}))
        assert not is_ready(path, role="worker", max_age=20)


@pytest.mark.anyio
async def test_heartbeat_publishes_readiness_and_removes_it_on_shutdown(tmp_path: Path):

    class Registry:

        async def register(self, *args):
            pass

        async def stop(self, *args):
            pass

        async def drain(self, *args):
            return True

    path = tmp_path / "health.json"
    heartbeat = RuntimeNodeHeartbeatService(Registry(),
                                            node_id="test",
                                            role="worker",
                                            worker_concurrency=1,
                                            heartbeat_interval_seconds=60,
                                            health_file=path)
    await heartbeat.start()
    assert is_ready(path, role="worker", max_age=20)
    await heartbeat.begin_drain()
    assert not is_ready(path, role="worker", max_age=20)
    await heartbeat.close()
    assert not path.exists()
