"""Lightweight readiness probe for roles without an HTTP server."""

import json
import math
import os
from pathlib import Path
import time


def process_start(pid: int) -> str:
    """Linux start ticks distinguish a restarted PID 1 from its old health file."""
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


def write_health(path: Path, role: str, state: str) -> None:
    payload = {
        "pid": os.getpid(),
        "process_start": process_start(os.getpid()),
        "role": role,
        "state": state,
        "checked_at": time.monotonic()
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(path)


def is_ready(path: Path, *, role: str, max_age: float) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        age = time.monotonic() - float(payload["checked_at"])
        return (payload["role"] == role and payload["state"] == "active" and math.isfinite(age)
                and 0 <= age <= max_age
                and process_start(int(payload["pid"])) == payload["process_start"])
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return False


def main() -> None:
    path = Path(os.environ.get("TRPC_SERVICE_NODE_HEALTH_FILE", "/tmp/trpc-node-health.json"))
    role = os.environ.get("TRPC_SERVICE_RUNTIME_ROLE", "")
    max_age = float(os.environ.get("TRPC_SERVICE_NODE_STALE_AFTER_SECONDS", "20"))
    raise SystemExit(0 if is_ready(path, role=role, max_age=max_age) else 1)


if __name__ == "__main__":
    main()
