"""Codex の `--output-schema`（strict モード）に渡すスキーマの変換と、受けた値の戻し。

strict モードは、`properties` を持つ object の全部の欄を `required` に入れ、`additionalProperties: false`
を求め、`uniqueItems` を受けない。元のスキーマで省けた欄は、null を許す形にして「省く」代わりに null で
返させる。`from_strict` は、その null を消して元の形に戻す。
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any


def _allow_null(node: dict[str, Any]) -> None:
    kind = node.get("type")
    if isinstance(kind, str):
        if kind != "null":
            node["type"] = [kind, "null"]
    elif isinstance(kind, list) and "null" not in kind:
        node["type"] = [*kind, "null"]
    enum = node.get("enum")
    if isinstance(enum, list) and None not in enum:
        node["enum"] = [*enum, None]


def _convert(node: Any) -> None:
    if isinstance(node, list):
        for child in node:
            _convert(child)
        return
    if not isinstance(node, dict):
        return
    node.pop("uniqueItems", None)
    properties = node.get("properties")
    if isinstance(properties, dict):
        required = set(node.get("required", ()))
        for key, sub in properties.items():
            if key not in required and isinstance(sub, dict):
                _allow_null(sub)
        node["required"] = list(properties)
        node["additionalProperties"] = False
    for key, child in node.items():
        if key == "properties" and isinstance(child, dict):
            # 欄の名前から部分スキーマへの対応表。欄の名前がキーワードと重なっても、表自身は節として扱わない
            for sub in child.values():
                _convert(sub)
        else:
            _convert(child)


def strict_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """元を書き換えずに、strict モードが受ける形の写しを返す。"""
    converted = copy.deepcopy(dict(schema))
    _convert(converted)
    return converted


def from_strict(value: Any, schema: Mapping[str, Any]) -> Any:
    """元のスキーマで省けた欄のうち null のものを消す（入れ子の object・配列の要素も）。

    `schema` は変換前のもの。必須の欄の null と、ほかの値はそのまま返す。
    """
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", ())
        restored: dict[str, Any] = {}
        for key, item in value.items():
            if item is None and key in properties and key not in required:
                continue
            sub = properties.get(key)
            restored[key] = from_strict(item, sub) if isinstance(sub, Mapping) else item
        return restored
    items = schema.get("items")
    if isinstance(value, list) and isinstance(items, Mapping):
        return [from_strict(item, items) for item in value]
    return value
