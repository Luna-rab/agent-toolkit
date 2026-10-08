"""Codex の strict な出力スキーマへの変換（`strict_schema`）と戻し（`from_strict`）。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from autodev.adapters.claude.schema import violations
from autodev.adapters.codex.schema import from_strict, strict_schema
from conftest import AUTODEV_PACKAGE

SCHEMA_PATHS = sorted((AUTODEV_PACKAGE / "schemas").glob("*.json"))

#: スキーマが使う pattern ごとに、合う値を 1 つ置く。知らない pattern が増えたらここを足す
PATTERN_SAMPLES = {
    "^[a-z0-9][a-z0-9-]*$": "x1",
    "^[a-z]+(?:/[a-z0-9]+)?#[1-9][0-9]*$": "owner/repo#12",
    "^D[1-9][0-9]*$": "D1",
    "^R[1-9][0-9]*$": "R1",
    "^(?:R[1-9][0-9]*|G-(?:no-open-findings|verify))$": "R1",
    "^(?:[RD][1-9][0-9]*|G-(?:no-open-findings|verify))$": "D1",
    "^task[1-9][0-9]*$": "task1",
    "^[^\\r\\n]+$": "title",
    "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$": "0b6f3c1e-9a8d-4c2b-8e7f-1a2b3c4d5e6f",
}


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def walk(node: Any):
    """スキーマの全部の部分（入れ子を含む）を順にたどる。"""
    if isinstance(node, dict):
        yield node
        for child in node.values():
            yield from walk(child)
    elif isinstance(node, list):
        for child in node:
            yield from walk(child)


def required_value(node: dict[str, Any]) -> Any:  # noqa: PLR0911  型ごとの分岐
    """必須の欄に入れる値。pattern・minLength・enum・minItems を満たし、省けた欄は null にする。"""
    if "enum" in node:
        return next(item for item in node["enum"] if item is not None)
    kinds = node["type"] if isinstance(node["type"], list) else [node["type"]]
    kind = next(item for item in kinds if item != "null")
    if kind == "object":
        return object_value(node)
    if kind == "array":
        # uniqueItems の検査に通るよう、要素は 1 つだけにする（minItems は 1 以下）
        return [required_value(node["items"])]
    if kind == "string":
        if "pattern" in node:
            return PATTERN_SAMPLES[node["pattern"]]
        return "x" * max(node.get("minLength", 0), 1)
    if kind == "boolean":
        return True
    return node.get("minimum", 1)


def object_value(node: dict[str, Any]) -> dict[str, Any]:
    required = node.get("required", [])
    return {
        key: required_value(sub) if key in required else None
        for key, sub in node.get("properties", {}).items()
    }


def test_スキーマは18本ある():
    assert len(SCHEMA_PATHS) == 18


@pytest.mark.parametrize("path", SCHEMA_PATHS, ids=lambda p: p.stem)
def test_全スキーマの変換結果がstrictの規則を満たす(path):
    converted = strict_schema(load(path))
    for node in walk(converted):
        assert "uniqueItems" not in node
        if "properties" in node:
            assert node["required"] == list(node["properties"]) or sorted(
                node["required"]
            ) == sorted(node["properties"])
            assert node["additionalProperties"] is False


@pytest.mark.parametrize("path", SCHEMA_PATHS, ids=lambda p: p.stem)
def test_変換は渡したスキーマを書き換えない(path):
    schema = load(path)
    before = copy.deepcopy(schema)
    strict_schema(schema)
    assert schema == before


def test_省ける欄のtypeにnullが足され必須の欄はそのまま():
    schema = {
        "type": "object",
        "required": ["a"],
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
    }
    converted = strict_schema(schema)
    assert converted["properties"]["a"]["type"] == "string"
    assert converted["properties"]["b"]["type"] == ["string", "null"]


def test_省ける欄のenumにnullが入る():
    schema = {"type": "object", "properties": {"a": {"enum": ["x", "y"]}}}
    assert None in strict_schema(schema)["properties"]["a"]["enum"]


def test_省けた欄のnullは消え必須の欄のnullは残る():
    schema = {
        "type": "object",
        "required": ["a", "c"],
        "properties": {
            "a": {"type": "string"},
            "b": {"type": "string"},
            "c": {"type": ["string", "null"]},
        },
    }
    assert from_strict({"a": "v", "b": None, "c": None}, schema) == {"a": "v", "c": None}
    assert from_strict({"a": "v", "b": None, "c": "w"}, schema) == {"a": "v", "c": "w"}


def test_省けた欄の値があればそのまま残る():
    schema = {"type": "object", "properties": {"b": {"type": "string"}}}
    assert from_strict({"b": "v"}, schema) == {"b": "v"}


def test_入れ子のobjectと配列の要素の省けた欄のnullも消える():
    schema = {
        "type": "object",
        "required": ["inner", "list"],
        "properties": {
            "inner": {
                "type": "object",
                "required": ["x"],
                "properties": {"x": {"type": "string"}, "y": {"type": "string"}},
            },
            "list": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["p"],
                    "properties": {"p": {"type": "string"}, "q": {"type": "string"}},
                },
            },
        },
    }
    value = {
        "inner": {"x": "1", "y": None},
        "list": [{"p": "a", "q": None}, {"p": "b", "q": "c"}],
    }
    assert from_strict(value, schema) == {
        "inner": {"x": "1"},
        "list": [{"p": "a"}, {"p": "b", "q": "c"}],
    }


@pytest.mark.parametrize("path", SCHEMA_PATHS, ids=lambda p: p.stem)
def test_変換後の形の値を戻すと元のスキーマの検査に通る(path):
    schema = load(path)
    value = object_value(schema)
    assert violations(value, strict_schema(schema)) == []
    assert violations(from_strict(value, schema), schema) == []


def test_supervisor_taskの例():
    schema = load(AUTODEV_PACKAGE / "schemas" / "supervisor-task.json")
    restored = from_strict(object_value(schema) | {"decision": "escalate"}, schema)
    assert restored["decision"] == "escalate"
    assert "runFlow" not in restored
    assert violations(restored, schema) == []
