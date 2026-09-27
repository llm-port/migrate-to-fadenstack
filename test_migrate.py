"""The renaming rules, on the files an LLM.Port install really has."""
from __future__ import annotations

from pathlib import Path

import migrate_to_fadenstack as m


def test_server_env_keys_values_and_secrets() -> None:
    text = (
        "# LLM.Port server\n"
        "LLM_PORT_API_WORKERS_COUNT=4\n"
        "LLM_PORT_API_RABBIT_USER=llmport-api\n"
        "LLM_PORT_API_RABBIT_PASS=llm_port_secret_llmport\n"
        "LLM_PORT_BACKEND_DB_BASE=llm_port_backend\n"
        "LLM_PORT_BACKEND_DB_USER=llm_port_backend\n"
        "LLM_PORT_BACKEND_DB_PASS=llm_port_backend\n"
        "LLM_PORT_BACKEND_GATEWAY_URL=http://llm-port-api:8000\n"
        "LLM_PORT_MCP_DB_BASE=llm_mcp\n"
        "LLMPORT_TUNE_PROFILE=prod\n"
        "REGISTRY=ghcr.io/llm-port\n"
        "LANGFUSE_INIT_ORG_ID=llm-port-org\n"
        "HF_TOKEN=hf_llmport\n"
        "LLM_PORT_API_ENCRYPTION_KEY=abc-llm-port-def\n"
    )
    out, changes = m.rename_env(text)
    assert out == (
        "# Fadenstack server\n"
        "FADEN_GATEWAY_WORKERS_COUNT=4\n"
        "FADEN_GATEWAY_RABBIT_USER=faden-gateway\n"
        "FADEN_GATEWAY_RABBIT_PASS=llm_port_secret_llmport\n"
        "FADEN_BACKEND_DB_BASE=faden_backend\n"
        "FADEN_BACKEND_DB_USER=faden_backend\n"
        "FADEN_BACKEND_DB_PASS=llm_port_backend\n"
        "FADEN_BACKEND_GATEWAY_URL=http://faden-gateway:8000\n"
        "FADEN_MCP_DB_BASE=faden_mcp\n"
        "FADEN_TUNE_PROFILE=prod\n"
        "REGISTRY=ghcr.io/fadenstack\n"
        "LANGFUSE_INIT_ORG_ID=llm-port-org\n"
        "HF_TOKEN=hf_llmport\n"
        "FADEN_GATEWAY_ENCRYPTION_KEY=abc-llm-port-def\n"
    )
    assert not any("secret" in c and "->" in c.split("  ")[-1] for c in changes)


def test_crlf_and_export_survive() -> None:
    out, _ = m.rename_env("export LLM_PORT_X=1\r\nPLAIN=2\r\n")
    assert out == "export FADEN_X=1\r\nPLAIN=2\r\n"


def test_checkout_paths_keep_the_checkout_folder() -> None:
    assert m.rename("install_dir: /home/sachi/fresh/llm-port-core/llm_port_shared") == \
        "install_dir: /home/sachi/fresh/llm-port-core/shared"
    assert m.rename(r"C:\00_Code\llm-port-core\llm_port_shared\.env") == r"C:\00_Code\llm-port-core\shared\.env"


def test_agent_env_and_runtime_names() -> None:
    text = (
        "LLM_PORT_NODE_AGENT_BACKEND_URL=https://10.88.10.61\n"
        "LLM_PORT_NODE_AGENT_TLS_CA_BUNDLE=/home/sachi/.config/llmport-agent/llmport-agent-server-ca.crt\n"
        "LLM_PORT_NODE_AGENT_IMAGE_ALLOWLIST=llmport/,nvcr.io/\n"
        "LLM_PORT_NODE_AGENT_MODEL_STORE=/srv/llm-port/models\n"
    )
    out, _ = m.rename_env(text)
    assert out == (
        "FADEN_NODE_AGENT_BACKEND_URL=https://10.88.10.61\n"
        "FADEN_NODE_AGENT_TLS_CA_BUNDLE=/home/sachi/.config/faden-agent/faden-agent-server-ca.crt\n"
        "FADEN_NODE_AGENT_IMAGE_ALLOWLIST=faden/,nvcr.io/\n"
        "FADEN_NODE_AGENT_MODEL_STORE=/srv/faden/models\n"
    )
    # the certified runtime image keeps its command
    assert m.rename("docker exec llm-port-ray-runtime llm-port-ray-runtime versions") == \
        "docker exec llm-port-ray-runtime llm-port-ray-runtime versions"


def test_a_path_that_exists_is_kept_when_asked(tmp_path: Path) -> None:
    cache = tmp_path / "llmport-image-cache"
    cache.mkdir()
    out, changes = m.rename_env(f"LLM_PORT_BACKEND_IMAGE_CACHE_DIR={cache}\n", keep_paths=True)
    assert out == f"FADEN_BACKEND_IMAGE_CACHE_DIR={cache}\n"
    assert "path kept" in changes[0]
    out, _ = m.rename_env(f"LLM_PORT_BACKEND_IMAGE_CACHE_DIR={cache}\n")
    assert out == f"FADEN_BACKEND_IMAGE_CACHE_DIR={tmp_path / 'faden-image-cache'}\n"


def test_timer_units() -> None:
    service = (
        "[Unit]\nDescription=LLM.Port nightly backup\n\n[Service]\nType=oneshot\n"
        "Environment=PATH=/usr/bin\nEnvironment=\"LLMPORT_CONFIG=/home/sachi/fresh/llmport.yaml\"\n"
        "ExecStart=/home/sachi/.local/bin/llmport backup -y --retain 7\n"
        "StandardOutput=append:/home/sachi/fresh/llm-port-core/llm_port_shared/backups/backup.log\n"
    )
    assert m.rename(service) == (
        "[Unit]\nDescription=Fadenstack nightly backup\n\n[Service]\nType=oneshot\n"
        "Environment=PATH=/usr/bin\nEnvironment=\"FADEN_CONFIG=/home/sachi/fresh/faden.yaml\"\n"
        "ExecStart=/home/sachi/.local/bin/faden backup -y --retain 7\n"
        "StandardOutput=append:/home/sachi/fresh/llm-port-core/shared/backups/backup.log\n"
    )


def test_volumes_and_containers() -> None:
    assert m.volume_new_name("llm_port_shared_pg_data") == "fadenstack_pg_data"
    assert m.volume_new_name("llm_port_shared_llm_port_models") == "fadenstack_faden_models"
    assert m.volume_new_name("other_pg_data") is None
    assert m.container_new_name("llm-port-api") == "faden-gateway"
    assert m.container_new_name("llm-port-api-migrator") == "faden-gateway-migrator"
    assert m.container_new_name("llm-port-backend-worker") == "faden-backend-worker"
    assert m.container_new_name("llm-port-web") == "faden-web"


def test_config_path() -> None:
    home = Path.home()
    assert m.new_config_path(home / ".config" / "llmport" / "llmport.yaml") == home / ".config" / "faden" / "faden.yaml"
    assert m.new_config_path(home / "fresh" / "llmport.yaml") == home / "fresh" / "faden.yaml"
