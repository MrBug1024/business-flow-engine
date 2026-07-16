from __future__ import annotations

import asyncio
import tempfile
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.studio import mcp_runtime
from app.studio.capability_runtime import CapabilityResult, _record_usage, discover_capabilities
from app.studio.mcp_runtime import (
    MASKED_SECRET,
    call_mcp_tool,
    normalize_mcp_payload,
    public_mcp_configs,
    sanitize_mcp_error,
)
from app.studio.models import BusinessContext, BusinessRecord
from app.studio.settings import studio_settings


client = TestClient(app)


@pytest.fixture
def isolated_settings(monkeypatch):
    temp_root = Path(".tmp")
    temp_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pytest-mcp-", dir=temp_root) as temporary:
        root = Path(temporary) / "business_studio"
        monkeypatch.setattr(studio_settings, "root", root)
        monkeypatch.setattr(studio_settings, "path", root / "studio_settings.json")
        yield


class FakeCallResult:
    def __init__(self, *, is_error: bool = False, text: str = "ok") -> None:
        self.isError = is_error
        self.content = [SimpleNamespace(text=text)]

    def model_dump(self, **_kwargs):
        return {
            "content": [{"type": "text", "text": self.content[0].text}],
            "isError": self.isError,
        }


class FakeSession:
    def __init__(self, calls: list[dict], *, call_error: bool = False) -> None:
        self.calls = calls
        self.call_error = call_error

    async def initialize(self):
        self.calls.append({"operation": "initialize"})
        return SimpleNamespace(serverInfo=SimpleNamespace(name="Fixture MCP", version="1.0"))

    async def list_tools(self, cursor=None):
        self.calls.append({"operation": "list_tools", "cursor": cursor})
        if cursor is None:
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="scrape",
                        title="Scrape",
                        description="Scrape one URL",
                        inputSchema={
                            "type": "object",
                            "properties": {"url": {"type": "string"}},
                            "required": ["url"],
                        },
                        outputSchema={"type": "object"},
                    )
                ],
                nextCursor="page-2",
            )
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name="crawl",
                    title="Crawl",
                    description="Crawl a site",
                    inputSchema={"type": "object", "properties": {}},
                    outputSchema=None,
                )
            ],
            nextCursor=None,
        )

    async def call_tool(self, name, arguments, read_timeout_seconds=None):
        self.calls.append(
            {
                "operation": "call_tool",
                "name": name,
                "arguments": arguments,
                "timeout": read_timeout_seconds,
            }
        )
        text = "Bearer api-secret was rejected" if self.call_error else "scraped"
        return FakeCallResult(is_error=self.call_error, text=text)


def _fake_session_factory(calls: list[dict], *, call_error: bool = False):
    @asynccontextmanager
    async def factory(_config):
        yield FakeSession(calls, call_error=call_error)

    return factory


def _codex_config(url: str = "https://mcp.example.test/rpc") -> dict:
    return {
        "mcpServers": {
            "firecrawl": {
                "type": "http",
                "url": url,
                "headers": {"Authorization": "Bearer api-secret"},
            }
        }
    }


def test_normalize_codex_http_and_validate_url():
    entry = normalize_mcp_payload(_codex_config())[0]
    assert entry["name"] == "firecrawl"
    assert entry["config"]["transport"] == "streamable_http"
    assert entry["config"]["headers"]["Authorization"] == "Bearer api-secret"

    env_entry = normalize_mcp_payload(
        {"mcpServers": {"env-server": {"type": "http", "url": "${MCP_SERVER_URL}"}}}
    )[0]
    assert env_entry["config"]["url"] == "${MCP_SERVER_URL}"

    host_entry = normalize_mcp_payload(
        {"mcpServers": {"env-host": {"type": "http", "url": "https://${MCP_HOST}/rpc"}}}
    )[0]
    assert host_entry["config"]["url"] == "https://${MCP_HOST}/rpc"

    with pytest.raises(ValueError, match="http"):
        normalize_mcp_payload(
            {"mcpServers": {"bad": {"type": "http", "url": "file:///etc/passwd"}}}
        )
    with pytest.raises(ValueError, match="credentials"):
        normalize_mcp_payload(
            {"mcpServers": {"bad": {"type": "http", "url": "https://u:p@example.test/rpc"}}}
        )

    malformed = public_mcp_configs(
        [
            {
                "name": "draft",
                "enabled": False,
                "config": {
                    "mcpServers": {
                        "draft": {"type": "unsupported", "headers": {"X-Api-Key": "secret"}}
                    }
                },
            }
        ]
    )
    assert malformed[0]["config"]["mcpServers"]["draft"]["headers"]["X-Api-Key"] == MASKED_SECRET


def test_unverified_mcp_server_is_not_agent_callable(isolated_settings):
    studio_settings.upsert_mcp_configs(normalize_mcp_payload(_codex_config()))
    record = BusinessRecord(
        id="business-unverified",
        name="Unverified MCP test",
        created_at=1,
        updated_at=1,
        context=BusinessContext(business_id="business-unverified", name="Unverified MCP test"),
    )
    assert all(item.kind != "mcp" for item in discover_capabilities(record))


def test_mcp_api_probe_save_redact_discover_and_mask_guard(
    isolated_settings,
    monkeypatch,
):
    calls: list[dict] = []
    monkeypatch.setattr(mcp_runtime, "_open_mcp_session", _fake_session_factory(calls))

    tested = client.post("/api/mcp-servers/test", json={"config": _codex_config()})
    assert tested.status_code == 200, tested.text
    test_result = tested.json()["servers"][0]
    assert test_result["status"] == "connected"
    assert test_result["tool_count"] == 2
    assert client.get("/api/settings").json()["mcp_configs"] == []

    saved = client.post("/api/mcp-servers", json={"config": _codex_config()})
    assert saved.status_code == 200, saved.text
    public_settings = saved.json()
    public_entry = public_settings["mcp_configs"][0]
    assert public_entry["config"]["headers"]["Authorization"] == MASKED_SECRET
    assert {tool["name"] for tool in public_entry["config"]["tools"]} == {"scrape", "crawl"}
    assert studio_settings.load().mcp_configs[0]["config"]["headers"]["Authorization"] == "Bearer api-secret"

    masked_codex = {"mcpServers": {"firecrawl": deepcopy(public_entry["config"])}}
    retested = client.post("/api/mcp-servers/test", json={"config": masked_codex})
    assert retested.status_code == 200, retested.text
    assert retested.json()["servers"][0]["status"] == "connected"
    resaved = client.post("/api/mcp-servers", json={"config": masked_codex})
    assert resaved.status_code == 200, resaved.text
    public_settings = resaved.json()

    record = BusinessRecord(
        id="business-test",
        name="MCP test",
        created_at=1,
        updated_at=1,
        context=BusinessContext(business_id="business-test", name="MCP test"),
    )
    capabilities = discover_capabilities(record)
    scrape = next(item for item in capabilities if item.function_name == "mcp__firecrawl__scrape")
    assert scrape.input_schema["required"] == ["url"]
    assert scrape.config["_remote_tool"] == "scrape"
    assert scrape.config["_server_name"] == "firecrawl"
    assert scrape.display_name == "firecrawl.scrape"
    _record_usage(record, scrape, {"url": "https://example.test"}, CapabilityResult({}, "scraped"))
    crawl = next(item for item in capabilities if item.function_name == "mcp__firecrawl__crawl")
    _record_usage(record, crawl, {}, CapabilityResult({}, "crawled"))
    assert len(record.context.mcp_references) == 1
    assert record.context.mcp_references[0]["name"] == "firecrawl"
    assert record.context.mcp_references[0]["last_tool"] == "crawl"

    same_destination = client.patch(
        "/api/settings",
        json={"mcp_configs": public_settings["mcp_configs"]},
    )
    assert same_destination.status_code == 200, same_destination.text
    assert studio_settings.load().mcp_configs[0]["config"]["headers"]["Authorization"] == "Bearer api-secret"

    attacked = deepcopy(public_settings["mcp_configs"])
    attacked[0]["config"]["url"] = "https://attacker.example.test/rpc"
    attacked_test = client.post(
        "/api/mcp-servers/test",
        json={
            "config": {
                "mcpServers": {"firecrawl": deepcopy(attacked[0]["config"])}
            }
        },
    )
    assert attacked_test.status_code == 422
    rejected = client.patch("/api/settings", json={"mcp_configs": attacked})
    assert rejected.status_code == 422
    current = studio_settings.load().mcp_configs[0]
    assert current["config"]["url"] == "https://mcp.example.test/rpc"
    assert current["config"]["headers"]["Authorization"] == "Bearer api-secret"

    disabled = client.patch("/api/mcp-servers/firecrawl", json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json()["mcp_configs"][0]["enabled"] is False
    deleted = client.delete("/api/mcp-servers/firecrawl")
    assert deleted.status_code == 200
    assert deleted.json()["mcp_configs"] == []

    cursors = [item["cursor"] for item in calls if item["operation"] == "list_tools"]
    assert cursors == [None, "page-2"] * 4


def test_call_tool_passes_arguments_and_treats_mcp_error_as_failure(monkeypatch):
    config = normalize_mcp_payload(_codex_config())[0]["config"]
    calls: list[dict] = []
    monkeypatch.setattr(mcp_runtime, "_open_mcp_session", _fake_session_factory(calls))
    result = asyncio.run(call_mcp_tool(config, "scrape", {"url": "https://example.test"}))
    assert result["content"][0]["text"] == "scraped"
    invocation = next(item for item in calls if item["operation"] == "call_tool")
    assert invocation["name"] == "scrape"
    assert invocation["arguments"] == {"url": "https://example.test"}
    assert invocation["timeout"].total_seconds() == 60

    monkeypatch.setattr(
        mcp_runtime,
        "_open_mcp_session",
        _fake_session_factory([], call_error=True),
    )
    with pytest.raises(RuntimeError) as raised:
        asyncio.run(call_mcp_tool(config, "scrape", {}))
    assert "api-secret" not in str(raised.value)
    assert MASKED_SECRET in str(raised.value)

    nested = ExceptionGroup("task group failed", [RuntimeError("Bearer api-secret rejected")])
    nested_message = sanitize_mcp_error(nested, config)
    assert "task group failed" not in nested_message
    assert "api-secret" not in nested_message
    assert MASKED_SECRET in nested_message
