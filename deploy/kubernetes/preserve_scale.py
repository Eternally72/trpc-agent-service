"""Relinquish old kubectl-apply ownership without changing live Worker replicas."""

import json
import subprocess


def main() -> None:
    kubectl = ["kubectl", "--context", "docker-desktop", "--namespace", "trpc-agent-service"]
    current = subprocess.run(
        kubectl + ["get", "deployment", "agent-worker", "--ignore-not-found", "-o", "json"],
        check=True,
        capture_output=True,
        text=True).stdout
    if not current.strip():
        return
    metadata = json.loads(current)["metadata"]
    raw = metadata.get("annotations", {}).get("kubectl.kubernetes.io/last-applied-configuration")
    if not raw:
        return
    previous = json.loads(raw)
    if "replicas" not in previous.get("spec", {}):
        return
    del previous["spec"]["replicas"]
    subprocess.run(kubectl + ["apply", "set-last-applied", "--create-annotation=true", "-f", "-"],
                   input=json.dumps(previous),
                   text=True,
                   check=True)


if __name__ == "__main__":
    main()
