"""JSON Schemas for the configuration files (editor autocompletion, contract tests)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from cairnward.config.models import (
    BackendsConfig,
    ClientsConfig,
    EffortConfig,
    EnvironmentConfig,
    MainConfig,
    McpConfig,
)

FILES: dict[str, type[BaseModel]] = {
    "cairnward.schema.json": MainConfig,
    "backends.schema.json": BackendsConfig,
    "clients.schema.json": ClientsConfig,
    "effort.schema.json": EffortConfig,
    "environment.schema.json": EnvironmentConfig,
    "mcp.schema.json": McpConfig,
}


def schemas() -> dict[str, dict[str, Any]]:
    out = {}
    for filename, model in FILES.items():
        schema = model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://github.com/c23075/cairnward/schemas/{filename}"
        out[filename] = schema
    return out


def render(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def write_schemas(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for filename, schema in schemas().items():
        path = out_dir / filename
        path.write_text(render(schema), encoding="utf-8")
        written.append(path)
    return written
