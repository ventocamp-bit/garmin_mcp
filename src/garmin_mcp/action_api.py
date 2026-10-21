"""Authenticated REST/OpenAPI facade for ChatGPT GPT Actions.

The facade deliberately owns no Garmin behavior. It discovers the exact tool
catalog from a private MCP server. Read-only access is the default; a deployment
can explicitly opt into writes without weakening authentication or its catalog.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlparse

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
import uvicorn


_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]+$")

_PRIVACY_POLICY = """Private Garmin Integration Privacy Policy

This is a private, personal-use integration. It sends only the action inputs
requested in ChatGPT to the owner's private Garmin API and returns the resulting
Garmin data to that ChatGPT conversation.

When the owner enables write access, requested actions can also create, change,
schedule, or delete Garmin records. The Action schema marks those operations as
consequential so ChatGPT can obtain confirmation before execution.

The application does not intentionally persist Garmin request or response
bodies, sell personal data, or expose Garmin data through its public health,
OpenAPI, or privacy endpoints. The API key and Garmin session tokens remain on
the private server. Infrastructure providers may process limited operational
metadata needed to deliver and secure the service.

The owner can disable access at any time by removing the GPT Action or rotating
the API key. This integration is not affiliated with Garmin or OpenAI.
"""


class ConfigError(ValueError):
    """Raised when the Action service cannot start securely."""


class CatalogError(ValueError):
    """Raised when the MCP tool catalog violates the configured contract."""


@dataclass(frozen=True)
class ToolDescriptor:
    """Security-relevant subset of an MCP tool definition."""

    name: str
    description: str | None
    input_schema: dict[str, Any]
    read_only: bool
    destructive: bool | None


@dataclass(frozen=True)
class GatewayResult:
    """Transport-neutral subset of an MCP tool result."""

    is_error: bool
    text_content: tuple[str, ...]
    structured_content: Any | None


class ToolGateway(Protocol):
    """The only interface the public facade needs from the private MCP."""

    async def list_tools(self) -> Sequence[ToolDescriptor]: ...

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> GatewayResult: ...


@dataclass(frozen=True)
class Settings:
    mcp_url: str
    api_key: str
    expected_tools: frozenset[str]
    public_base_url: str
    host: str
    port: int
    max_request_bytes: int
    call_timeout_seconds: float
    max_concurrency: int
    access_log: bool
    allow_writes: bool
    direct_tools: frozenset[str] | None
    file_root: Path | None

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> "Settings":
        expected_tools = _parse_expected_tools(
            environ.get("GARMIN_ACTION_EXPECTED_TOOLS", "")
        )
        public_base_url = _validate_public_base_url(
            environ.get("GARMIN_ACTION_PUBLIC_BASE_URL", "")
        )
        mcp_url = _validate_mcp_url(
            environ.get("GARMIN_MCP_URL", "http://garmin-mcp:8000/mcp")
        )

        return cls(
            mcp_url=mcp_url,
            api_key=load_secret(
                environ,
                "GARMIN_ACTION_API_KEY",
                "GARMIN_ACTION_API_KEY_FILE",
            ),
            expected_tools=expected_tools,
            public_base_url=public_base_url,
            host=environ.get("GARMIN_ACTION_HOST", "127.0.0.1"),
            port=_positive_int(environ, "GARMIN_ACTION_PORT", 8080, maximum=65535),
            max_request_bytes=_positive_int(
                environ, "GARMIN_ACTION_MAX_REQUEST_BYTES", 65_536, maximum=1_048_576
            ),
            call_timeout_seconds=_positive_float(
                environ, "GARMIN_ACTION_CALL_TIMEOUT_SECONDS", 60.0, maximum=300.0
            ),
            max_concurrency=_positive_int(
                environ, "GARMIN_ACTION_MAX_CONCURRENCY", 4, maximum=32
            ),
            access_log=_truthy(environ.get("GARMIN_ACTION_ACCESS_LOG", "false")),
            allow_writes=_truthy(environ.get("GARMIN_ACTION_ALLOW_WRITES", "false")),
            direct_tools=(
                _parse_expected_tools(environ["GARMIN_ACTION_DIRECT_TOOLS"])
                if environ.get("GARMIN_ACTION_DIRECT_TOOLS") else None
            ),
            file_root=(
                Path(environ["GARMIN_ACTION_FILE_ROOT"]).resolve()
                if environ.get("GARMIN_ACTION_FILE_ROOT") else None
            ),
        )


class StreamableMcpGateway:
    """Small per-operation client for a private streamable-HTTP MCP server."""

    def __init__(self, url: str):
        self._url = _validate_mcp_url(url)

    @asynccontextmanager
    async def _session(self):
        async with streamable_http_client(self._url) as (
            read_stream,
            write_stream,
            _get_session_id,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session

    async def list_tools(self) -> Sequence[ToolDescriptor]:
        descriptors: list[ToolDescriptor] = []
        async with self._session() as session:
            cursor: str | None = None
            while True:
                result = await session.list_tools(cursor=cursor)
                for tool in result.tools:
                    annotations = tool.annotations
                    descriptors.append(
                        ToolDescriptor(
                            name=tool.name,
                            description=tool.description,
                            input_schema=tool.inputSchema,
                            read_only=bool(
                                annotations
                                and annotations.readOnlyHint is True
                            ),
                            destructive=(
                                annotations.destructiveHint
                                if annotations is not None
                                else None
                            ),
                        )
                    )
                cursor = getattr(result, "nextCursor", None)
                if not cursor:
                    break
        return descriptors

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> GatewayResult:
        async with self._session() as session:
            result = await session.call_tool(name, arguments)

        text_content = tuple(
            item.text
            for item in result.content
            if getattr(item, "type", None) == "text"
        )
        return GatewayResult(
            is_error=bool(result.isError),
            text_content=text_content,
            structured_content=getattr(result, "structuredContent", None),
        )


def load_secret(
    environ: Mapping[str, str],
    value_name: str,
    file_name: str,
    *,
    minimum_length: int = 32,
) -> str:
    """Load exactly one nonempty secret value without ever logging it."""

    direct = environ.get(value_name)
    secret_path = environ.get(file_name)
    if bool(direct) == bool(secret_path):
        raise ConfigError(f"configure exactly one of {value_name} and {file_name}")

    if secret_path:
        try:
            value = Path(secret_path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"cannot read secret file configured by {file_name}") from exc
    else:
        value = direct or ""

    if len(value) < minimum_length:
        raise ConfigError(f"{value_name} must contain at least {minimum_length} characters")
    return value


def validate_catalog(
    tools: Sequence[ToolDescriptor], expected_tools: set[str] | frozenset[str],
    *, allow_writes: bool = False,
) -> dict[str, ToolDescriptor]:
    """Return an immutable-by-convention catalog or fail before serving traffic."""

    catalog: dict[str, ToolDescriptor] = {}
    for tool in tools:
        if not _TOOL_NAME.fullmatch(tool.name):
            raise CatalogError(f"invalid tool name: {tool.name!r}")
        if tool.name in catalog:
            raise CatalogError(f"duplicate tool name: {tool.name}")
        if not allow_writes and not tool.read_only:
            raise CatalogError(f"tool {tool.name!r} is not explicitly read-only")
        if not allow_writes and tool.destructive is True:
            raise CatalogError(f"tool {tool.name!r} is destructive")
        if not allow_writes and tool.destructive is not False:
            raise CatalogError(
                f"tool {tool.name!r} is not explicitly non-destructive"
            )
        if not isinstance(tool.input_schema, dict) or tool.input_schema.get("type") != "object":
            raise CatalogError(f"tool {tool.name!r} must have an object input schema")

        try:
            schema_copy = json.loads(json.dumps(tool.input_schema))
        except (TypeError, ValueError) as exc:
            raise CatalogError(f"tool {tool.name!r} has a non-JSON input schema") from exc

        catalog[tool.name] = ToolDescriptor(
            name=tool.name,
            description=tool.description,
            input_schema=schema_copy,
            read_only=tool.read_only and tool.destructive is False,
            destructive=tool.destructive is not False,
        )

    expected = set(expected_tools)
    actual = set(catalog)
    if not expected or actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise CatalogError(
            f"tool catalog mismatch: missing={missing!r}, extra={extra!r}"
        )
    return catalog


def build_openapi(
    catalog: Mapping[str, ToolDescriptor], public_base_url: str,
    *, direct_tools: set[str] | frozenset[str] | None = None,
    include_dispatch: bool = False,
) -> dict[str, Any]:
    """Build a GPT-Actions-compatible OpenAPI document from MCP-owned schemas."""

    public_base_url = _validate_public_base_url(public_base_url)
    paths: dict[str, Any] = {}
    selected = set(catalog) if direct_tools is None else set(direct_tools)
    if not selected.issubset(catalog):
        raise CatalogError("direct Action tools must be in the validated catalog")
    for name in sorted(selected):
        tool = catalog[name]
        description = (tool.description or f"Call the {name} tool.").strip()
        paths[f"/v1/tools/{name}"] = {
            "post": {
                "operationId": name,
                "summary": f"Run {name}",
                # ChatGPT's Action editor accepts operation descriptions of at
                # most 300 characters. Keep the MCP description as the source
                # while making the exported contract directly importable.
                "description": description[:300],
                "x-openai-isConsequential": not tool.read_only,
                "security": [{"bearerAuth": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {"schema": tool.input_schema}
                    },
                },
                "responses": {
                    "200": {
                        "description": "Garmin operation result",
                        "content": {
                            "application/json": {"schema": {}}
                        },
                    },
                    "400": {"description": "Invalid request"},
                    "401": {"description": "Missing or invalid bearer key"},
                    "404": {"description": "Tool is not exposed"},
                    "413": {"description": "Request body is too large"},
                    "502": {"description": "Private Garmin backend failed"},
                    "504": {"description": "Private Garmin backend timed out"},
                },
            }
        }

    if include_dispatch:
        paths.update(_dispatch_openapi())

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Private Garmin API" if include_dispatch else "Private Garmin Read API",
            "description": "Authenticated Garmin tools for a private GPT.",
            "version": "1.1.0",
        },
        "servers": [{"url": public_base_url}],
        "paths": paths,
        "components": {
            # The Action editor expects this subsection to be an object when a
            # components section is present, even when no reusable schemas exist.
            "schemas": {},
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer"}
            }
        },
    }


def _dispatch_openapi() -> dict[str, Any]:
    """Keep a large MCP catalog discoverable through a small Action schema."""
    paths: dict[str, Any] = {
        "/v1/catalog": {
            "get": {
                "operationId": "list_garmin_tools",
                "description": "Find Garmin operations by query. Pass an exact name to retrieve its input schema before calling read_garmin_data or change_garmin_data. Results are paginated.",
                "x-openai-isConsequential": False,
                "security": [{"bearerAuth": []}],
                "parameters": [
                    {"name": name, "in": "query", "required": False, "schema": schema}
                    for name, schema in (
                        ("query", {"type": "string"}),
                        ("name", {"type": "string"}),
                        ("offset", {"type": "integer", "minimum": 0, "default": 0}),
                        ("limit", {"type": "integer", "minimum": 1, "maximum": 50, "default": 20}),
                    )
                ],
                "responses": {"200": {"description": "Operation descriptions and optional input schema"}},
            }
        }
    }
    for effect, operation in (("read", "read_garmin_data"), ("write", "change_garmin_data")):
        paths[f"/v1/{effect}"] = {
            "post": {
                "operationId": operation,
                "description": (
                    "Run a read-only Garmin operation discovered with list_garmin_tools."
                    if effect == "read" else
                    "Create, update, schedule or delete Garmin data using an operation discovered with list_garmin_tools. Confirm the exact operation and arguments with the user."
                ),
                "x-openai-isConsequential": effect == "write",
                "security": [{"bearerAuth": []}],
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": {
                        "type": "object",
                        "required": ["tool_name", "arguments_json"],
                        "additionalProperties": False,
                        "properties": {
                            "tool_name": {"type": "string", "description": "Exact name from list_garmin_tools."},
                            "arguments_json": {"type": "string", "description": "JSON object encoded as a string, matching the discovered input schema. Use {} when no arguments are required."},
                        },
                    }}},
                },
                "responses": {
                    "200": {"description": "Garmin operation result"},
                    "400": {"description": "Invalid arguments"},
                    "401": {"description": "Missing or invalid bearer key"},
                    "403": {"description": "Wrong operation effect or file path outside the configured workspace"},
                    "502": {"description": "Garmin backend failed"},
                },
            }
        }
    return paths


def _confine_file_arguments(name: str, arguments: dict[str, Any], root: Path | None) -> dict[str, Any]:
    """Garmin write access must not grant access to server files or credentials."""
    if root is None:
        return arguments
    parameter = {
        "upload_course": "gpx_path",
        "download_activity_file": "output_dir",
        "set_fit_download_dir": "path",
    }.get(name)
    if parameter is None or arguments.get(parameter) is None:
        return arguments
    value = arguments[parameter]
    if not isinstance(value, str) or not value:
        raise ValueError("invalid file path")
    requested = Path(value)
    resolved = (requested if requested.is_absolute() else root / requested).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("path outside workspace")
    return {**arguments, parameter: str(resolved)}


def create_app(
    *,
    gateway: ToolGateway,
    api_key: str,
    expected_tools: set[str] | frozenset[str],
    public_base_url: str,
    max_request_bytes: int = 65_536,
    call_timeout_seconds: float = 60.0,
    max_concurrency: int = 4,
    allow_writes: bool = False,
    direct_tools: set[str] | frozenset[str] | None = None,
    file_root: Path | None = None,
) -> Starlette:
    """Create the public app, validating its private backend during startup."""

    if len(api_key) < 32:
        raise ConfigError("API key must contain at least 32 characters")
    if max_request_bytes <= 0:
        raise ConfigError("max_request_bytes must be positive")
    if call_timeout_seconds <= 0:
        raise ConfigError("call_timeout_seconds must be positive")
    if max_concurrency <= 0:
        raise ConfigError("max_concurrency must be positive")

    expected = frozenset(expected_tools)
    public_url = _validate_public_base_url(public_base_url)
    concurrency = asyncio.Semaphore(max_concurrency)
    if file_root is not None:
        file_root = file_root.resolve()

    @asynccontextmanager
    async def lifespan(app: Starlette):
        catalog = validate_catalog(await gateway.list_tools(), expected, allow_writes=allow_writes)
        app.state.catalog = catalog
        app.state.openapi = build_openapi(
            catalog, public_url,
            direct_tools=(direct_tools or set()) if allow_writes else None,
            include_dispatch=allow_writes,
        )
        yield

    async def healthz(request: Request) -> Response:
        return JSONResponse(
            {"status": "ok", "tool_count": len(request.app.state.catalog)}
        )

    async def openapi(request: Request) -> Response:
        return JSONResponse(request.app.state.openapi)

    async def privacy(request: Request) -> Response:
        return Response(
            _PRIVACY_POLICY,
            media_type="text/plain",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    async def list_catalog(request: Request) -> Response:
        if not _authorized(request.headers.get("authorization"), api_key):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        catalog = request.app.state.catalog
        name = request.query_params.get("name")
        if name:
            tool = catalog.get(name)
            if tool is None:
                return JSONResponse({"error": "tool_not_found"}, status_code=404)
            return JSONResponse({
                "name": tool.name, "description": tool.description,
                "read_only": tool.read_only, "destructive": tool.destructive,
                "input_schema": tool.input_schema,
            })
        try:
            offset = int(request.query_params.get("offset", "0"))
            limit = int(request.query_params.get("limit", "20"))
            if offset < 0 or not 1 <= limit <= 50:
                raise ValueError
        except ValueError:
            return JSONResponse({"error": "invalid_pagination"}, status_code=400)
        query = request.query_params.get("query", "").casefold()
        matches = [tool for _, tool in sorted(catalog.items())
                   if query in f"{tool.name} {tool.description or ''}".casefold()]
        end = offset + limit
        return JSONResponse({
            "total": len(matches), "offset": offset,
            "next_offset": end if end < len(matches) else None,
            "tools": [{"name": tool.name, "description": (tool.description or "")[:300],
                       "read_only": tool.read_only, "destructive": tool.destructive}
                      for tool in matches[offset:end]],
        })

    async def call_tool(request: Request) -> Response:
        if not _authorized(request.headers.get("authorization"), api_key):
            return JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        tool_name = request.path_params.get("tool_name")
        if tool_name is not None and tool_name not in request.app.state.catalog:
            return JSONResponse({"error": "tool_not_found"}, status_code=404)

        length_header = request.headers.get("content-length")
        if length_header:
            try:
                declared_length = int(length_header)
                if declared_length < 0:
                    return JSONResponse({"error": "invalid_request"}, status_code=400)
                if declared_length > max_request_bytes:
                    return JSONResponse(
                        {"error": "request_too_large"}, status_code=413
                    )
            except ValueError:
                return JSONResponse({"error": "invalid_request"}, status_code=400)

        body_buffer = bytearray()
        async for chunk in request.stream():
            if len(body_buffer) + len(chunk) > max_request_bytes:
                return JSONResponse({"error": "request_too_large"}, status_code=413)
            body_buffer.extend(chunk)
        body = bytes(body_buffer)
        try:
            arguments = json.loads(body) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return JSONResponse({"error": "invalid_json"}, status_code=400)
        if not isinstance(arguments, dict):
            return JSONResponse({"error": "json_object_required"}, status_code=400)

        if tool_name is None:
            tool_name = arguments.get("tool_name")
            encoded = arguments.get("arguments_json")
            if not isinstance(tool_name, str) or not isinstance(encoded, str):
                return JSONResponse({"error": "invalid_dispatch_request"}, status_code=400)
            tool = request.app.state.catalog.get(tool_name)
            if tool is None:
                return JSONResponse({"error": "tool_not_found"}, status_code=404)
            requested_read = request.url.path.endswith("/read")
            if requested_read != tool.read_only:
                return JSONResponse({"error": "wrong_operation_effect"}, status_code=403)
            try:
                arguments = json.loads(encoded)
            except (ValueError, TypeError):
                return JSONResponse({"error": "invalid_arguments_json"}, status_code=400)
            if not isinstance(arguments, dict):
                return JSONResponse({"error": "json_object_required"}, status_code=400)

        try:
            arguments = _confine_file_arguments(tool_name, arguments, file_root)
        except ValueError:
            return JSONResponse({"error": "file_path_not_allowed"}, status_code=403)

        async def invoke() -> GatewayResult:
            async with concurrency:
                return await gateway.call_tool(tool_name, arguments)

        try:
            result = await asyncio.wait_for(invoke(), timeout=call_timeout_seconds)
        except TimeoutError:
            return JSONResponse({"error": "backend_timeout"}, status_code=504)
        except Exception:
            # Backend exceptions can include URLs, account data, or transport
            # internals. Keep the public error deliberately generic.
            return JSONResponse({"error": "backend_unavailable"}, status_code=502)

        if result.is_error:
            return JSONResponse({"error": "backend_tool_error"}, status_code=502)
        try:
            normalised = _normalise_result(result)
        except ValueError:
            return JSONResponse(
                {"error": "invalid_backend_result"}, status_code=502
            )
        return JSONResponse(normalised)

    return Starlette(
        debug=False,
        lifespan=lifespan,
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route("/openapi.json", openapi, methods=["GET"]),
            Route("/privacy", privacy, methods=["GET"]),
            Route("/v1/tools/{tool_name}", call_tool, methods=["POST"]),
        ] + ([
            Route("/v1/catalog", list_catalog, methods=["GET"]),
            Route("/v1/read", call_tool, methods=["POST"]),
            Route("/v1/write", call_tool, methods=["POST"]),
        ] if allow_writes else []),
    )


def _normalise_result(result: GatewayResult) -> Any:
    if len(result.text_content) == 1:
        text = result.text_content[0]
        try:
            return json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            if result.structured_content is None:
                return {"result": text}
    if result.structured_content is not None:
        try:
            json.dumps(result.structured_content)
        except (TypeError, ValueError) as exc:
            raise ValueError("backend result is not JSON serializable") from exc
        return result.structured_content
    if result.text_content:
        return {"results": list(result.text_content)}
    return {"result": None}


def _authorized(header: str | None, api_key: str) -> bool:
    if not header or not header.startswith("Bearer "):
        return False
    candidate = header[len("Bearer ") :]
    return bool(candidate) and hmac.compare_digest(
        candidate.encode("utf-8"), api_key.encode("utf-8")
    )


def _parse_expected_tools(value: str) -> frozenset[str]:
    names = [item.strip() for item in value.split(",") if item.strip()]
    if not names:
        raise ConfigError("GARMIN_ACTION_EXPECTED_TOOLS must not be empty")
    if len(names) != len(set(names)):
        raise ConfigError("GARMIN_ACTION_EXPECTED_TOOLS contains duplicates")
    invalid = sorted(name for name in names if not _TOOL_NAME.fullmatch(name))
    if invalid:
        raise ConfigError(f"GARMIN_ACTION_EXPECTED_TOOLS has invalid names: {invalid!r}")
    return frozenset(names)


def _validate_public_base_url(value: str) -> str:
    value = value.rstrip("/")
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError(
            "GARMIN_ACTION_PUBLIC_BASE_URL must be an HTTPS URL without credentials, query, or fragment"
        )
    return value


def _validate_mcp_url(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError("GARMIN_MCP_URL must be an HTTP(S) URL without credentials")
    return value


def _positive_int(
    environ: Mapping[str, str], name: str, default: int, *, maximum: int
) -> int:
    try:
        value = int(environ.get(name, str(default)))
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value <= 0 or value > maximum:
        raise ConfigError(f"{name} must be between 1 and {maximum}")
    return value


def _positive_float(
    environ: Mapping[str, str], name: str, default: float, *, maximum: float
) -> float:
    try:
        value = float(environ.get(name, str(default)))
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if value <= 0 or value > maximum:
        raise ConfigError(f"{name} must be greater than 0 and at most {maximum}")
    return value


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def main() -> None:
    try:
        settings = Settings.from_environment(os.environ)
    except ConfigError as exc:
        print(f"Garmin Action API configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    app = create_app(
        gateway=StreamableMcpGateway(settings.mcp_url),
        api_key=settings.api_key,
        expected_tools=settings.expected_tools,
        public_base_url=settings.public_base_url,
        max_request_bytes=settings.max_request_bytes,
        call_timeout_seconds=settings.call_timeout_seconds,
        max_concurrency=settings.max_concurrency,
        allow_writes=settings.allow_writes,
        direct_tools=settings.direct_tools,
        file_root=settings.file_root,
    )
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        access_log=settings.access_log,
        log_level="info",
    )


if __name__ == "__main__":
    main()
