"""Deployment success requires capacity, not just a zero-replica rollout."""

import copy
import json

import pytest

from deploy.kubernetes import wait_workers


def ready_state():
    return ({
        "desired_nodes": 2,
        "active_nodes": 2,
        "reconciling": False
    }, {
        "metadata": {
            "generation": 3
        },
        "spec": {
            "replicas": 2
        },
        "status": {
            "observedGeneration": 3,
            "replicas": 2,
            "updatedReplicas": 2,
            "readyReplicas": 2,
            "availableReplicas": 2
        }
    })


@pytest.mark.parametrize("change", ["zero", "unregistered", "old_image", "unobserved", "unready"])
def test_rollout_success_alone_does_not_mean_worker_capacity(change):
    pool, deployment = ready_state()
    assert wait_workers.converged(pool, deployment)
    if change == "zero":
        pool.update(desired_nodes=0, active_nodes=0)
        deployment["spec"]["replicas"] = 0
        deployment["status"] = {"observedGeneration": 3}
    elif change == "unregistered":
        pool["active_nodes"] = 0
    elif change == "old_image":
        deployment["status"]["updatedReplicas"] = 0
    elif change == "unobserved":
        deployment["status"]["observedGeneration"] = 2
    else:
        deployment["status"]["readyReplicas"] = 1
    assert not wait_workers.converged(pool, deployment)


def test_capacity_wait_observes_recovery_and_fails_when_it_never_happens(monkeypatch):
    pool, deployment = ready_state()
    empty = copy.deepcopy(deployment)
    empty["status"] = {"observedGeneration": 3}
    clock = [0]
    monkeypatch.setattr(wait_workers.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(wait_workers.time, "sleep",
                        lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def command(*args):
        return json.dumps(pool if args[0] == "exec" else (deployment if clock[0] >= 2 else empty))

    monkeypatch.setattr(wait_workers, "kubectl", command)
    wait_workers.wait(timeout=5)
    assert clock[0] == 2
    monkeypatch.setattr(wait_workers, "kubectl",
                        lambda *args: json.dumps(pool if args[0] == "exec" else empty))
    with pytest.raises(TimeoutError, match="desired=2, active=2, ready=0"):
        wait_workers.wait(timeout=3)
