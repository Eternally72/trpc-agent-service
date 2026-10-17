"""Wait for both registered Workers and the Deployment to reach the stored target."""

import json
import subprocess
import time


def kubectl(*args: str) -> str:
    return subprocess.run(
        [
            "kubectl", "--context", "docker-desktop", "--request-timeout=15s", "-n",
            "trpc-agent-service", *args
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout


def converged(pool: dict, deployment: dict) -> bool:
    target = pool.get("desired_nodes", 0)
    status = deployment.get("status", {})
    return (type(target) is int and 1 <= target <= 64 and pool.get("active_nodes") == target
            and pool.get("reconciling") is False
            and deployment.get("spec", {}).get("replicas") == target
            and status.get("observedGeneration", 0) >= deployment["metadata"]["generation"] and all(
                status.get(field, 0) == target
                for field in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")))


def wait(timeout: float = 300) -> None:
    # Read the bootstrap token inside the Pod: never send it as a CLI argument
    # or return it to the host. No dependency on localhost port forwarding.
    probe = ("from pathlib import Path; import urllib.request; "
             "token=Path('/run/secrets/platform/admin_bootstrap_token').read_text().strip(); "
             "request=urllib.request.Request('http://127.0.0.1:8000/api/v1/admin/worker-pool', "
             "headers={'Authorization': 'Bearer '+token}); "
             "print(urllib.request.urlopen(request, timeout=10).read().decode())")
    deadline = time.monotonic() + timeout
    last_state = "not observed"
    while time.monotonic() < deadline:
        try:
            pool = json.loads(
                kubectl("exec", "deployment/gateway", "--", "/app/.venv/bin/python", "-c", probe))
            deployment = json.loads(kubectl("get", "deployment", "agent-worker", "-o", "json"))
            if converged(pool, deployment):
                print(f"Worker capacity ready: {pool['active_nodes']}/{pool['desired_nodes']}")
                return
            last_state = (
                f"desired={pool.get('desired_nodes')}, active={pool.get('active_nodes')}, "
                f"ready={deployment.get('status', {}).get('readyReplicas', 0)}")
        except (subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
            last_state = type(error).__name__
        time.sleep(2)
    raise TimeoutError(f"Worker capacity did not converge: {last_state}")


if __name__ == "__main__":
    wait()
