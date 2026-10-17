"""Merge only portable model settings; keep host addresses and secrets out of ConfigMaps."""

import argparse
import json
import os
from pathlib import Path
import re

from dotenv import dotenv_values

from trpc_service.config.llm import BailianEmbeddingConfig, BailianModelConfig


def prepare(project_root: Path, output_dir: Path) -> None:
    """Write kubectl env files without sourcing executable shell configuration."""

    defaults = dotenv_values(project_root / "deploy/kubernetes/app.env", interpolate=False)
    local = dotenv_values(project_root / ".env", interpolate=False)
    config = {key: value for key, value in defaults.items() if value is not None}
    secrets: dict[str, str] = {}
    for name, schema in (("TRPC_SERVICE_LLM", BailianModelConfig), ("TRPC_SERVICE_EMBEDDING",
                                                                    BailianEmbeddingConfig)):
        raw = os.environ.get(name, local.get(name) or config[name])
        try:
            model = schema.model_validate_json(raw)
        except ValueError:
            # Validation errors can echo credentials accidentally pasted into JSON.
            raise ValueError(f"invalid {name}; check the model configuration") from None
        reference = model.api_key_ref
        if not reference.startswith("env://"):
            raise ValueError(f"{name}.api_key_ref must use env:// for Kubernetes deployment")
        secret_name = reference.removeprefix("env://")
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", secret_name) is None:
            raise ValueError(f"invalid environment reference in {name}")
        value = os.environ.get(secret_name, local.get(secret_name) or "")
        if not value.strip() or "\n" in value or "\r" in value:
            raise ValueError(f"missing or multiline credential referenced by {name}")
        secrets[secret_name] = value
        config[name] = json.dumps(model.model_dump(mode="json"),
                                  ensure_ascii=False,
                                  separators=(",", ":"))

    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for filename, values in (("app.env", config), ("runtime.env", secrets)):
        target = output_dir / filename
        with target.open("w", encoding="utf-8") as handle:
            os.chmod(target, 0o600)
            handle.write("".join(f"{key}={value}\n" for key, value in values.items()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        prepare(args.project_root, args.output_dir)
    except ValueError as error:
        parser.exit(2, f"error: {error}\n")


if __name__ == "__main__":
    main()
