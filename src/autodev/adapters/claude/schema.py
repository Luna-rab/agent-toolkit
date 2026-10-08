"""ステージが返した構造化出力を、`schemas/<ステージ>.json` の形と照らす。

`--json-schema` は生成時の制約ではなく事後の検証で、外れても `subtype: success` のまま空や崩れた形で
返ることがある。driver の側でもう一度確かめる。標準ライブラリだけで動かす
ため、`schemas/` が使うキーワード（`type`・`enum`・`required`・`properties`・`additionalProperties`・
`items`・`minLength`・`maxLength`・`minItems`・`uniqueItems`・`pattern`・`minimum`）だけを扱う。知らない
キーワードは、検査（`test_contracts.py`）が `schemas/` に置かせない。

モデルが報告の欄の「無い」を `null` ではなく文字列の `"null"`・`"none"` で返すことがある。`normalize_nulls` で、null を許す欄のそういう文字列を null に直してから照らす。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

_NULL_WORDS = frozenset({"null", "none"})

_TYPES: Mapping[str, Any] = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


@cache
def load_schema(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"スキーマが JSON の object でない: {path}")
    return loaded


@cache
def ecma_pattern(source: str) -> re.Pattern[str]:
    """JSON Schema の `pattern`（ECMA の正規表現）を Python で照らす形にする。

    Python の `$` は末尾の改行 1 つの手前でも合うが、ECMA の `$` は文字列の終わりにしか合わない。
    文字の組（`[...]`）の外の、エスケープしていない `$` を `\\Z` に置き換えて、末尾の改行を通さない。
    """
    out: list[str] = []
    escaped = in_class = False
    for char in source:
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif in_class:
            in_class = char != "]"
        elif char == "[":
            in_class = True
        elif char == "$":
            out.append(r"\Z")
            continue
        out.append(char)
    return re.compile("".join(out))


def _types(node: Mapping[str, Any]) -> tuple[str, ...]:
    kind = node.get("type")
    if kind is None:
        return ()
    return (kind,) if isinstance(kind, str) else tuple(kind)


def _allows_null(node: Mapping[str, Any]) -> bool:
    return "null" in _types(node) or None in node.get("enum", ())


def normalize_nulls(value: Any, node: Mapping[str, Any]) -> Any:
    """null を許す欄に入った `"null"`・`"none"` を null にする。ほかはそのまま返す。"""
    if (
        isinstance(value, str)
        and value.strip().lower() in _NULL_WORDS
        and _allows_null(node)
        and not (node.get("enum") and value in node["enum"])
    ):
        return None
    if isinstance(value, dict):
        properties = node.get("properties", {})
        return {
            key: normalize_nulls(item, properties[key]) if key in properties else item
            for key, item in value.items()
        }
    if isinstance(value, list) and isinstance(node.get("items"), dict):
        return [normalize_nulls(item, node["items"]) for item in value]
    return value


def _is(value: Any, kind: str) -> bool:
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES[kind])


def violations(value: Any, node: Mapping[str, Any], where: str = "$") -> list[str]:  # noqa: PLR0912  キーワードごとの分岐
    """形に合わない所。空なら合っている。"""
    found: list[str] = []
    types = _types(node)
    if types and not any(_is(value, kind) for kind in types):
        return [f"{where}: 型が {'・'.join(types)} でない"]
    if "enum" in node and value not in node["enum"]:
        found.append(f"{where}: {value!r} は列挙に無い")
    if isinstance(value, str):
        if len(value) < node.get("minLength", 0):
            found.append(f"{where}: 文字列が短い")
        if "maxLength" in node and len(value) > node["maxLength"]:
            found.append(f"{where}: 文字列が長い")
        if "pattern" in node and ecma_pattern(node["pattern"]).search(value) is None:
            found.append(f"{where}: {value!r} が形 {node['pattern']} に合わない")
    number = isinstance(value, (int, float)) and not isinstance(value, bool)
    if number and "minimum" in node and value < node["minimum"]:
        found.append(f"{where}: {value} が {node['minimum']} より小さい")
    if isinstance(value, list):
        if len(value) < node.get("minItems", 0):
            found.append(f"{where}: 要素が少ない")
        if node.get("uniqueItems"):
            seen = [json.dumps(item, sort_keys=True) for item in value]
            if len(seen) != len(set(seen)):
                found.append(f"{where}: 同じ要素が重なっている")
        items = node.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                found += violations(item, items, f"{where}[{index}]")
    if isinstance(value, dict):
        properties: Mapping[str, Any] = node.get("properties", {})
        for key in node.get("required", ()):
            if key not in value:
                found.append(f"{where}: {key} が無い")
        extra = node.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                found += violations(item, properties[key], f"{where}.{key}")
            elif extra is False:
                found.append(f"{where}: 知らない欄 {key}")
            elif isinstance(extra, dict):
                found += violations(item, extra, f"{where}.{key}")
    return found
