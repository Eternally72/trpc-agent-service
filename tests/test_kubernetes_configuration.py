"""Exercise the actual deployment configuration renderer with isolated credentials."""

import json
from pathlib import Path

from dotenv import dotenv_values
import pytest

from deploy.kubernetes.prepare_config import prepare


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("TRPC_SERVICE_LLM", "TRPC_SERVICE_EMBEDDING", "DASHSCOPE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    defaults = Path(__file__).resolve().parents[1] / "deploy/kubernetes/app.env"
    target = tmp_path / "deploy/kubernetes/app.env"
    target.parent.mkdir(parents=True)
    target.write_text(defaults.read_text())
    return tmp_path


def test_render_inherits_models_but_never_host_storage_or_plaintext_credentials(project: Path):
    (project / ".env").write_text('DASHSCOPE_API_KEY="test-key-with-#-and-=characters"\n'
                                  'TRPC_SERVICE_DATABASE_URL=postgresql://localhost:15432/host\n'
                                  'TRPC_SERVICE_EMBEDDING={"model_name":"configured-embedding"}\n'
                                  'TRPC_SERVICE_LLM={"model_name":"configured-chat"}\n')
    output = project / "output"
    prepare(project, output)
    config = dotenv_values(output / "app.env", interpolate=False)
    secret = dotenv_values(output / "runtime.env", interpolate=False)
    assert json.loads(config["TRPC_SERVICE_LLM"])["model_name"] == "configured-chat"
    assert json.loads(config["TRPC_SERVICE_EMBEDDING"])["model_name"] == "configured-embedding"
    assert "@postgres:5432/" in config["TRPC_SERVICE_DATABASE_URL"]
    assert "test-key" not in (output / "app.env").read_text()
    assert secret["DASHSCOPE_API_KEY"] == "test-key-with-#-and-=characters"
    assert (output / "runtime.env").stat().st_mode & 0o777 == 0o600


def test_render_resolves_separate_model_credentials(project: Path):
    (project / ".env").write_text('DASHSCOPE_API_KEY=chat-test-key\nEMBED_KEY=embedding-test-key\n'
                                  'TRPC_SERVICE_EMBEDDING={"api_key_ref":"env://EMBED_KEY"}\n')
    prepare(project, project / "output")
    assert dotenv_values(project / "output/runtime.env") == {
        "DASHSCOPE_API_KEY": "chat-test-key",
        "EMBED_KEY": "embedding-test-key"
    }


@pytest.mark.parametrize("value", [
    '{"dimensions":768}',
    '{"api_key_ref":"file:///host/credential"}',
    '{"api_key_ref":"env://MISSING_TEST_CREDENTIAL"}',
])
def test_invalid_configuration_fails_before_writing_files(project: Path, value: str):
    (project /
     ".env").write_text(f'DASHSCOPE_API_KEY=private-test-value\nTRPC_SERVICE_EMBEDDING={value}\n')
    with pytest.raises(ValueError) as error:
        prepare(project, project / "output")
    assert "private-test-value" not in str(error.value)
    assert not (project / "output/app.env").exists()
