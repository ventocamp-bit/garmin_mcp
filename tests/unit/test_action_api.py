import asyncio
import json
from pathlib import Path
import tomllib

import pytest
from starlette.testclient import TestClient

from garmin_mcp.action_api import (
    CatalogError,
    ConfigError,
    GatewayResult,
    Settings,
    ToolDescriptor,
    build_openapi,
    create_app,
    load_secret,
    validate_catalog,
)


API_KEY = "k" * 32
PUBLIC_URL = "https://garmin.example.test"
SAFE_TOOL = ToolDescriptor(
    name="get_stats",
    description="Get Garmin summary statistics.",
    input_schema={
        "type": "object",
        "properties": {"date": {"type": "string", "format": "date"}},
        "required": ["date"],
        "additionalProperties": False,
    },
    read_only=True,
    destructive=False,
)


class FakeGateway:
    def __init__(self, tools=None, result=None, delay=0):
        self.tools = list(tools or [SAFE_TOOL])
        self.result = result or GatewayResult(
            is_error=False,
            text_content=('{"steps": 1234}',),
            structured_content=None,
        )
        self.delay = delay
        self.calls = []

    async def list_tools(self):
        return self.tools

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.result


def make_app(gateway=None, **kwargs):
    return create_app(
        gateway=gateway or FakeGateway(),
        api_key=API_KEY,
        expected_tools={"get_stats"},
        public_base_url=PUBLIC_URL,
        **kwargs,
    )


@pytest.mark.parametrize(
    ("tool", "match"),
    [
        (
            ToolDescriptor("write_data", "unsafe", {"type": "object"}, False, False),
            "not explicitly read-only",
        ),
        (
            ToolDescriptor("get_stats", "unsafe", {"type": "object"}, True, True),
            "destructive",
        ),
        (
            ToolDescriptor("get_stats", "unsafe", {"type": "object"}, True, None),
            "not explicitly non-destructive",
        ),
        (
            ToolDescriptor("bad/name", "unsafe", {"type": "object"}, True, False),
            "invalid tool name",
        ),
        (
            ToolDescriptor("get_stats", "unsafe", {"type": "array"}, True, False),
            "object input schema",
        ),
    ],
)
def test_catalog_validation_fails_closed_for_unsafe_tools(tool, match):
    with pytest.raises(CatalogError, match=match):
        validate_catalog([tool], {tool.name})


def test_catalog_validation_requires_exact_expected_set():
    with pytest.raises(CatalogError, match="catalog mismatch"):
        validate_catalog([SAFE_TOOL], {"get_stats", "get_sleep_summary"})


def test_openapi_has_one_nonconsequential_bearer_secured_operation_per_tool():
    catalog = validate_catalog([SAFE_TOOL], {"get_stats"})

    schema = build_openapi(catalog, PUBLIC_URL)

    operation = schema["paths"]["/v1/tools/get_stats"]["post"]
    assert schema["openapi"] == "3.1.0"
    assert schema["servers"] == [{"url": PUBLIC_URL}]
    assert schema["components"]["securitySchemes"]["bearerAuth"] == {
        "type": "http",
        "scheme": "bearer",
    }
    assert schema["components"]["schemas"] == {}
    assert len(operation["description"]) <= 300
    assert operation["security"] == [{"bearerAuth": []}]
    assert operation["x-openai-isConsequential"] is False
    assert (
        operation["requestBody"]["content"]["application/json"]["schema"]
        == SAFE_TOOL.input_schema
    )
    assert API_KEY not in json.dumps(schema)


def test_requests_without_exact_bearer_key_are_rejected_before_gateway_call():
    gateway = FakeGateway()
    with TestClient(make_app(gateway)) as client:
        missing = client.post("/v1/tools/get_stats", json={"date": "2026-07-29"})
        wrong = client.post(
            "/v1/tools/get_stats",
            headers={"Authorization": "Bearer wrong"},
            json={"date": "2026-07-29"},
        )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert gateway.calls == []


def test_authenticated_request_proxies_exact_arguments_and_normalizes_json():
    gateway = FakeGateway()
    arguments = {"date": "2026-07-29"}
    with TestClient(make_app(gateway)) as client:
        response = client.post(
            "/v1/tools/get_stats",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json=arguments,
        )

    assert response.status_code == 200
    assert response.json() == {"steps": 1234}
    assert gateway.calls == [("get_stats", arguments)]


def test_json_text_wins_over_fastmcp_result_wrapper():
    gateway = FakeGateway(
        result=GatewayResult(
            is_error=False,
            text_content=('{"full_name": "Example User"}',),
            structured_content={"result": "{\"full_name\": \"Example User\"}"},
        )
    )
    with TestClient(make_app(gateway)) as client:
        response = client.post(
            "/v1/tools/get_stats",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"date": "2026-07-29"},
        )

    assert response.status_code == 200
    assert response.json() == {"full_name": "Example User"}


def test_unknown_tool_and_invalid_or_oversized_json_fail_closed():
    gateway = FakeGateway()
    headers = {"Authorization": f"Bearer {API_KEY}"}
    with TestClient(make_app(gateway, max_request_bytes=32)) as client:
        unknown = client.post("/v1/tools/not_allowed", headers=headers, json={})
        invalid_shape = client.post(
            "/v1/tools/get_stats", headers=headers, json=["not", "an", "object"]
        )
        oversized = client.post(
            "/v1/tools/get_stats",
            headers=headers,
            content=json.dumps({"value": "x" * 100}),
        )

    assert unknown.status_code == 404
    assert invalid_shape.status_code == 400
    assert oversized.status_code == 413
    assert gateway.calls == []


def test_backend_error_and_timeout_do_not_expose_backend_content():
    headers = {"Authorization": f"Bearer {API_KEY}"}
    backend_error = FakeGateway(
        result=GatewayResult(
            is_error=True,
            text_content=("secret backend detail",),
            structured_content=None,
        )
    )
    with TestClient(make_app(backend_error)) as client:
        failed = client.post("/v1/tools/get_stats", headers=headers, json={})

    slow = FakeGateway(delay=0.05)
    with TestClient(make_app(slow, call_timeout_seconds=0.001)) as client:
        timed_out = client.post("/v1/tools/get_stats", headers=headers, json={})

    assert failed.status_code == 502
    assert failed.json() == {"error": "backend_tool_error"}
    assert "secret backend detail" not in failed.text
    assert timed_out.status_code == 504
    assert timed_out.json() == {"error": "backend_timeout"}


def test_non_json_backend_result_fails_with_502():
    headers = {"Authorization": f"Bearer {API_KEY}"}
    gateway = FakeGateway(
        result=GatewayResult(
            is_error=False,
            text_content=(),
            structured_content={"not_json": object()},
        )
    )
    with TestClient(make_app(gateway)) as client:
        response = client.post("/v1/tools/get_stats", headers=headers, json={})

    assert response.status_code == 502
    assert response.json() == {"error": "invalid_backend_result"}


def test_public_metadata_endpoints_are_data_free_and_do_not_require_authentication():
    with TestClient(make_app()) as client:
        health = client.get("/healthz")
        schema = client.get("/openapi.json")
        privacy = client.get("/privacy")

    assert health.status_code == 200
    assert health.json() == {"status": "ok", "tool_count": 1}
    assert schema.status_code == 200
    assert "/v1/tools/get_stats" in schema.json()["paths"]
    assert privacy.status_code == 200
    assert privacy.headers["content-type"].startswith("text/plain")
    assert "private, personal-use integration" in privacy.text
    assert API_KEY not in privacy.text


def test_secret_loader_requires_one_strong_source(tmp_path):
    secret_file = tmp_path / "api-key"
    secret_file.write_text("f" * 32 + "\n", encoding="utf-8")

    assert (
        load_secret(
            {"GARMIN_ACTION_API_KEY_FILE": str(secret_file)},
            "GARMIN_ACTION_API_KEY",
            "GARMIN_ACTION_API_KEY_FILE",
        )
        == "f" * 32
    )

    with pytest.raises(ConfigError, match="exactly one"):
        load_secret(
            {
                "GARMIN_ACTION_API_KEY": API_KEY,
                "GARMIN_ACTION_API_KEY_FILE": str(secret_file),
            },
            "GARMIN_ACTION_API_KEY",
            "GARMIN_ACTION_API_KEY_FILE",
        )
    with pytest.raises(ConfigError, match="at least 32"):
        load_secret(
            {"GARMIN_ACTION_API_KEY": "short"},
            "GARMIN_ACTION_API_KEY",
            "GARMIN_ACTION_API_KEY_FILE",
        )


def test_environment_settings_fail_closed_without_https_catalog_or_secret():
    valid = {
        "GARMIN_ACTION_API_KEY": API_KEY,
        "GARMIN_ACTION_EXPECTED_TOOLS": "get_stats",
        "GARMIN_ACTION_PUBLIC_BASE_URL": PUBLIC_URL,
    }
    settings = Settings.from_environment(valid)
    assert settings.expected_tools == frozenset({"get_stats"})
    assert settings.public_base_url == PUBLIC_URL

    for missing in valid:
        invalid = dict(valid)
        invalid.pop(missing)
        with pytest.raises(ConfigError):
            Settings.from_environment(invalid)

    insecure = dict(valid, GARMIN_ACTION_PUBLIC_BASE_URL="http://garmin.example.test")
    with pytest.raises(ConfigError, match="HTTPS URL"):
        Settings.from_environment(insecure)

    duplicates = dict(
        valid, GARMIN_ACTION_EXPECTED_TOOLS="get_stats,get_stats"
    )
    with pytest.raises(ConfigError, match="duplicates"):
        Settings.from_environment(duplicates)


def test_mobile_actions_compose_keeps_raw_mcp_private_and_uses_one_allowlist():
    repository = Path(__file__).resolve().parents[2]
    compose = (repository / "deploy" / "mobile-actions" / "compose.yaml").read_text(
        encoding="utf-8"
    )
    mcp_service = compose.split("  garmin-actions:", 1)[0]

    assert "ports:" not in mcp_service
    assert "GARMIN_ENABLED_TOOLS: *read-only-tools" in mcp_service
    assert "GARMIN_READ_ONLY_TOOLS: *read-only-tools" in mcp_service
    assert "GARMIN_ACTION_EXPECTED_TOOLS: *read-only-tools" in compose
    assert "GARMIN_ACTION_API_KEY_FILE: /run/secrets/garmin_action_api_key" in compose
    assert 'user: "65532:65532"' in compose
    assert "GARMIN_EMAIL" not in compose
    assert "GARMIN_PASSWORD" not in compose


def test_caddy_override_joins_only_the_action_service_to_existing_proxy_network():
    repository = Path(__file__).resolve().parents[2]
    override = (
        repository / "deploy" / "mobile-actions" / "compose.caddy.yaml"
    ).read_text(encoding="utf-8")

    assert "garmin-actions:" in override
    assert "garmin-mcp:" not in override
    assert "garmin-private" in override
    assert "caddy-proxy" in override
    assert "name: n8n_default" in override


def test_mcp_dependency_excludes_breaking_major_version_two():
    repository = Path(__file__).resolve().parents[2]
    project = tomllib.loads((repository / "pyproject.toml").read_text(encoding="utf-8"))
    mcp_requirement = next(
        dependency
        for dependency in project["project"]["dependencies"]
        if dependency.startswith("mcp")
    )

    assert "<2" in mcp_requirement


WRITE_TOOL = ToolDescriptor(
    "delete_workout", "Delete one workout", {"type": "object", "properties": {
        "workout_id": {"type": "integer"}}, "required": ["workout_id"]}, False, True,
)


def full_app(gateway, **kwargs):
    return create_app(
        gateway=gateway, api_key=API_KEY,
        expected_tools={tool.name for tool in gateway.tools},
        public_base_url=PUBLIC_URL, allow_writes=True,
        direct_tools={"get_stats"} if SAFE_TOOL in gateway.tools else set(), **kwargs,
    )


def test_write_opt_in_preserves_exact_catalog_and_consequential_annotations():
    with pytest.raises(CatalogError):
        validate_catalog([WRITE_TOOL], {"delete_workout"})
    with pytest.raises(CatalogError, match="catalog mismatch"):
        validate_catalog([WRITE_TOOL], {"get_stats"}, allow_writes=True)
    catalog = validate_catalog([SAFE_TOOL, WRITE_TOOL], {"get_stats", "delete_workout"}, allow_writes=True)
    schema = build_openapi(catalog, PUBLIC_URL, include_dispatch=True)
    assert schema["paths"]["/v1/tools/delete_workout"]["post"]["x-openai-isConsequential"] is True
    assert schema["paths"]["/v1/read"]["post"]["x-openai-isConsequential"] is False
    assert schema["paths"]["/v1/write"]["post"]["x-openai-isConsequential"] is True


def test_full_access_discovery_and_dispatch_keep_effects_separate():
    gateway = FakeGateway(tools=[SAFE_TOOL, WRITE_TOOL])
    headers = {"Authorization": f"Bearer {API_KEY}"}
    payload = {"tool_name": "delete_workout", "arguments_json": '{"workout_id": 123}'}
    with TestClient(full_app(gateway)) as client:
        assert client.get("/v1/catalog").status_code == 401
        assert client.post("/v1/write", json=payload).status_code == 401
        assert client.post("/v1/read", headers=headers, json=payload).status_code == 403
        assert gateway.calls == []
        listing = client.get("/v1/catalog?limit=1", headers=headers).json()
        assert listing["total"] == 2 and listing["next_offset"] == 1
        descriptor = client.get("/v1/catalog?name=delete_workout", headers=headers).json()
        assert descriptor["input_schema"] == WRITE_TOOL.input_schema
        assert descriptor["read_only"] is False
        assert client.post("/v1/write", headers=headers, json=payload).status_code == 200
        read = {"tool_name": "get_stats", "arguments_json": '{"date": "2026-10-20"}'}
        assert client.post("/v1/read", headers=headers, json=read).status_code == 200
        assert client.post("/v1/write", headers=headers, json=read).status_code == 403
        schema = client.get("/openapi.json").json()
        assert len(schema["paths"]) == 4
    assert gateway.calls == [("delete_workout", {"workout_id": 123}), ("get_stats", {"date": "2026-10-20"})]


@pytest.mark.parametrize("encoded", ["[1]", "null", "broken"])
def test_invalid_dispatch_arguments_never_reach_garmin(encoded):
    gateway = FakeGateway(tools=[WRITE_TOOL])
    with TestClient(full_app(gateway)) as client:
        response = client.post("/v1/write", headers={"Authorization": f"Bearer {API_KEY}"},
                               json={"tool_name": "delete_workout", "arguments_json": encoded})
    assert response.status_code == 400 and not gateway.calls


def test_unannotated_operations_are_consequential_when_writes_are_enabled():
    unknown = ToolDescriptor("new_operation", None, {"type": "object"}, False, None)
    catalog = validate_catalog([unknown], {"new_operation"}, allow_writes=True)
    assert catalog["new_operation"].destructive is True
    assert not catalog["new_operation"].read_only


@pytest.mark.parametrize("endpoint", ["/v1/tools/upload_course", "/v1/write"])
def test_full_access_cannot_upload_server_credentials(endpoint, tmp_path):
    tool = ToolDescriptor("upload_course", "Upload a GPX course", {"type": "object"}, False, None)
    gateway = FakeGateway(tools=[tool])
    root = tmp_path / "files"
    root.mkdir()
    arguments = {"gpx_path": str(tmp_path / "tokens" / "garmin_tokens.json"), "course_name": "Example"}
    payload = arguments if endpoint.endswith("upload_course") else {
        "tool_name": "upload_course", "arguments_json": json.dumps(arguments)}
    with TestClient(full_app(gateway, file_root=root)) as client:
        response = client.post(endpoint, headers={"Authorization": f"Bearer {API_KEY}"}, json=payload)
    assert response.status_code == 403 and not gateway.calls


def test_file_arguments_resolve_relative_paths_inside_the_shared_workspace(tmp_path):
    tool = ToolDescriptor("upload_course", None, {"type": "object"}, False, None)
    gateway = FakeGateway(tools=[tool])
    with TestClient(full_app(gateway, file_root=tmp_path)) as client:
        response = client.post("/v1/write", headers={"Authorization": f"Bearer {API_KEY}"},
            json={"tool_name": "upload_course", "arguments_json": '{"gpx_path": "route.gpx"}'})
    assert response.status_code == 200
    assert gateway.calls == [("upload_course", {"gpx_path": str(tmp_path / "route.gpx")})]
