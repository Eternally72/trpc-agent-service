"""Opt-in checks against the real Docker Desktop cluster; no model quota used.

TRPC_TEST_KUBERNETES=1 uv run pytest tests/integration/test_kubernetes_cluster.py -v
The scaling test restores the original desired capacity even after a failure.
"""

import json
import os
from pathlib import Path
import subprocess
import time

import httpx
import pytest

pytestmark = pytest.mark.skipif(os.environ.get("TRPC_TEST_KUBERNETES") != "1",
                                reason="requires the explicitly selected Docker Desktop cluster")
ROOT = Path(__file__).resolve().parents[2]


def kubectl(*args: str) -> str:
    return subprocess.run([
        "kubectl", "--context", "docker-desktop", "--request-timeout=15s", "-n",
        "trpc-agent-service", *args
    ],
                          check=True,
                          capture_output=True,
                          text=True,
                          timeout=30).stdout


@pytest.fixture
def client():
    token = (ROOT / ".secrets/admin_bootstrap_token").read_text().strip()
    with httpx.Client(base_url="http://127.0.0.1:8000",
                      trust_env=False,
                      timeout=15,
                      headers={"Authorization": f"Bearer {token}"}) as value:
        yield value


def pool(client: httpx.Client) -> dict:
    response = client.get("/api/v1/admin/worker-pool")
    response.raise_for_status()
    return response.json()


def wait_capacity(client: httpx.Client, target: int) -> None:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        current = pool(client)
        deployment = json.loads(kubectl("get", "deployment", "agent-worker", "-o", "json"))
        if (current["desired_nodes"] == target and current["active_nodes"] == target
                and not current["reconciling"]
                and deployment["status"].get("readyReplicas", 0) == target
                and deployment["spec"]["replicas"] == target):
            return
        time.sleep(1)
    raise AssertionError(f"Worker capacity did not converge to {target}")


def test_cluster_roles_schema_and_console(client):
    nodes = json.loads(kubectl("get", "nodes", "-o", "json"))["items"]
    assert len(nodes) >= 3
    assert all(
        any(c["type"] == "Ready" and c["status"] == "True" for c in node["status"]["conditions"])
        for node in nodes)
    for path in ("/health", "/ready", "/admin", "/tenant", "/console/config.js"):
        response = client.get(path)
        assert response.status_code == 200, path
    deployments = json.loads(kubectl("get", "deployments", "-o", "json"))["items"]
    for deployment in deployments:
        assert deployment["status"].get("availableReplicas", 0) == deployment["spec"]["replicas"]
    for role in ("agent-worker", "channel-runtime", "worker-scaler"):
        kubectl("exec", f"deployment/{role}", "--", "/app/.venv/bin/python", "-m",
                "trpc_service.healthcheck")
    head = kubectl("exec", "postgres-0", "-c", "postgres", "--", "psql", "-U", "trpc", "-d",
                   "trpc_agent", "-Atc", "SELECT version_num FROM alembic_version").strip()
    assert head == "20260930_0029"


def test_worker_scale_out_and_in_restores_original_target(client):
    original = pool(client)["desired_nodes"]

    def scale(target: int) -> None:
        current = pool(client)
        response = client.put("/api/v1/admin/worker-pool",
                              json={
                                  "desired_nodes": target,
                                  "expected_generation": current["generation"]
                              })
        response.raise_for_status()
        wait_capacity(client, target)

    try:
        scale(3)
        scale(1)
    finally:
        scale(original)


def test_scaler_rbac_cannot_change_gateway():
    identity = "system:serviceaccount:trpc-agent-service:worker-scaler"
    allowed = kubectl("auth", "can-i", "patch", "deployments/agent-worker", "--subresource=scale",
                      f"--as={identity}")
    assert allowed.strip() == "yes"
    denied = subprocess.run([
        "kubectl", "--context", "docker-desktop", "-n", "trpc-agent-service", "auth", "can-i",
        "patch", "deployments/gateway", "--subresource=scale", f"--as={identity}"
    ],
                            capture_output=True,
                            text=True,
                            timeout=15)
    assert denied.returncode == 1 and denied.stdout.strip() == "no"


def test_worker_pod_replacement_restores_capacity(client):
    target = pool(client)["desired_nodes"]
    pods = json.loads(
        kubectl("get", "pods", "-l", "app.kubernetes.io/name=agent-worker", "-o", "json"))["items"]
    old = pods[0]["metadata"]
    kubectl("delete", "pod", old["name"], "--wait=false")
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        pods = json.loads(
            kubectl("get", "pods", "-l", "app.kubernetes.io/name=agent-worker", "-o",
                    "json"))["items"]
        if len(pods) == target and all(p["metadata"]["uid"] != old["uid"] for p in pods):
            wait_capacity(client, target)
            return
        time.sleep(1)
    raise AssertionError("Worker Pod was not replaced")


def test_monitoring_endpoints_and_prometheus_targets():
    with httpx.Client(trust_env=False, timeout=15) as client:
        health = client.get("http://127.0.0.1:3000/api/health")
        health.raise_for_status()
        assert health.json()["database"] == "ok"
        targets = client.get("http://127.0.0.1:9090/api/v1/targets")
        targets.raise_for_status()
        active = targets.json()["data"]["activeTargets"]
        assert active and all(target["health"] == "up" for target in active)
    kubectl(
        "exec", "deployment/gateway", "--", "/app/.venv/bin/python", "-c", "import urllib.request; "
        "assert urllib.request.urlopen('http://loki:3100/ready').status == 200; "
        "assert urllib.request.urlopen('http://tempo:3200/ready').status == 200")
