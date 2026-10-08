"""値オブジェクト・コマンド・イベントと、JSON にできる値（dict・list・str・int・bool・None）の相互変換。

イベントストアの `data` と `requests` の `data` に使う。変換の規則は型注釈から決めるので、クラスを
足しても変換の処理を書き足さなくてよい。

- `Text`・`Number` は包みを外した素の値にする
- `Enum` は値にする
- dataclass は欄の名前をキーにした dict にする。読むときは、知らないキーと欠けたキー（既定値の
  無いもの）を拒む。黙って読み飛ばすと、形を変えたのにアップキャスタを書き忘れても気づけない
- `tuple[X, ...]` は list、`frozenset[X]` は並べた list にする（同じ集合が同じ JSON になる）。
  読むときは、同じ値が 2 つある list を集合として拒む
- 読んだ値が値オブジェクトの検査に落ちたら、どこで落ちたかを付けた `DecodeError` にする
- `X | None` は None か X。None 以外の型を 2 つ以上並べた Union は扱わない（どちらで読むか決まらない）
"""

from __future__ import annotations

import dataclasses
import json
import types
import typing
from enum import Enum
from functools import cache
from typing import Any, TypeVar, Union

from .value_objects.base import InvalidValue, Number, Text

T = TypeVar("T")

#: JSON にできる値
Json = Any

#: dataclass の欄の metadata の鍵。値は、その欄の古いキー名の tuple
RENAMED_FROM = "renamedFrom"


class DecodeError(ValueError):
    """JSON の形が、読もうとした型に合わない。"""


def to_json(value: object) -> Json:  # noqa: PLR0911  型の種類ごとの分岐
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (Text, Number)):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: to_json(getattr(value, field.name)) for field in _fields(type(value))}
    if isinstance(value, (tuple, list)):
        return [to_json(item) for item in value]
    if isinstance(value, frozenset):
        items = [to_json(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
    if isinstance(value, dict):
        return {str(key): to_json(item) for key, item in value.items()}
    raise TypeError(f"JSON にできない値: {type(value).__name__}")


def from_json(cls: type[T], data: Json) -> T:
    """`cls` の dataclass として読む。"""
    return _decode(cls, data, cls.__name__)


@cache
def _fields(cls: Any) -> tuple[dataclasses.Field[Any], ...]:
    return tuple(field for field in dataclasses.fields(cls) if field.init)


@cache
def _hints(cls: type) -> dict[str, Any]:
    return typing.get_type_hints(cls)


def _decode(tp: Any, data: Json, where: str) -> Any:  # noqa: PLR0911, PLR0912  型の種類ごとの分岐
    origin = typing.get_origin(tp)
    if origin is Union or origin is types.UnionType:
        args = [arg for arg in typing.get_args(tp) if arg is not type(None)]
        if len(args) != 1:
            raise TypeError(f"{where}: None 以外を 2 つ以上並べた Union は読めない: {tp}")
        return None if data is None else _decode(args[0], data, where)
    if origin is tuple:
        item = typing.get_args(tp)[0]
        return tuple(_decode(item, x, f"{where}[]") for x in _expect(list, data, where))
    if origin is frozenset:
        item = typing.get_args(tp)[0]
        items = [_decode(item, x, f"{where}[]") for x in _expect(list, data, where)]
        if len(set(items)) != len(items):
            raise DecodeError(f"{where}: 集合に同じ値が 2 つある: {data!r}")
        return frozenset(items)
    if origin is dict:
        return dict(_expect(dict, data, where))
    if tp is Any or tp is object:
        return data
    if isinstance(tp, type) and issubclass(tp, Enum):
        try:
            return tp(data)
        except ValueError as e:
            raise DecodeError(f"{where}: {tp.__name__} に無い値: {data!r}") from e
    if isinstance(tp, type) and issubclass(tp, Text):
        return _construct(tp, where, _expect(str, data, where))
    if isinstance(tp, type) and issubclass(tp, Number):
        if isinstance(data, bool):
            raise DecodeError(f"{where}: int が要るところに bool: {data!r}")
        return _construct(tp, where, _expect(int, data, where))
    if isinstance(tp, type) and dataclasses.is_dataclass(tp):
        return _decode_dataclass(tp, _expect(dict, data, where), where)
    if tp is bool:
        return _expect(bool, data, where)
    if tp is int:
        if isinstance(data, bool):
            raise DecodeError(f"{where}: int が要るところに bool: {data!r}")
        return _expect(int, data, where)
    if tp is str:
        return _expect(str, data, where)
    raise TypeError(f"{where}: 読めない型: {tp}")


def _decode_dataclass(cls: type, data: dict[str, Json], where: str) -> Any:
    fields = _fields(cls)
    names = {field.name for field in fields}
    data = _rename_keys(fields, data, where)
    if unknown := sorted(set(data) - names):
        raise DecodeError(f"{where}: 知らないキー: {', '.join(unknown)}")
    hints = _hints(cls)
    kwargs: dict[str, Any] = {}
    for field in fields:
        if field.name in data:
            kwargs[field.name] = _decode(
                hints[field.name], data[field.name], f"{where}.{field.name}"
            )
        elif field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING:
            raise DecodeError(f"{where}: {field.name} が無い")
    return _construct(cls, where, **kwargs)


def _rename_keys(fields: Any, data: dict[str, Json], where: str) -> dict[str, Json]:
    """古いキー名で書かれた欄を、新しい欄の名前に付け替える。両方あれば拒む。"""
    renamed = dict(data)
    for field in fields:
        for old in field.metadata.get(RENAMED_FROM, ()):
            if old not in renamed:
                continue
            if field.name in renamed:
                raise DecodeError(f"{where}: {old} と {field.name} が両方ある")
            renamed[field.name] = renamed.pop(old)
    return renamed


def _construct(cls: Any, where: str, *args: Any, **kwargs: Any) -> Any:
    """値の検査の落ち（InvalidValue）を、どこで落ちたかの付いた DecodeError に包み直す。"""
    try:
        return cls(*args, **kwargs)
    except InvalidValue as e:
        raise DecodeError(f"{where}: {e}") from e


def _expect(tp: type[T], data: Json, where: str) -> T:
    if not isinstance(data, tp):
        raise DecodeError(f"{where}: {tp.__name__} が要るところに {type(data).__name__}: {data!r}")
    return data
