"""glob の照合を 1 か所に置く。

ガードのフック（テストへの書き込みを止める）と完了チェック（テストのファイルが変わったか・
テストが要らないパスに収まるか）は、ここの照合を使う。別々に書くと、フックが通したものを
完了チェックが落とす（または逆の）ずれが出る。

`fnmatch` は `*` が `/` をまたぐので使わず、glob を正規表現に直す。

- `*`・`?` は `/` をまたがない。`**` だけがまたぐ。`**/` は 0 段にも当たる（`**/tests/**` が
  リポジトリ直下の `tests/` に当たる）
- `/` で始まらない glob は、末尾からの照合でも見る（`tests/*.py` が `a/b/tests/c.py` に当たる。
  `PurePath.match` と同じ）
- 末尾が `/` の指定はディレクトリの名前で当てる。名前が前方一致するだけのディレクトリには当てない
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache
from pathlib import PurePosixPath

from ...domain.value_objects.glob_pattern import GlobPattern


def normalize(path: str) -> str:
    """`\\` 区切りと先頭の `./` を寄せる。フックが受け取るパスは claude が渡すもので形が揃わない。"""
    return str(PurePosixPath(path.replace("\\", "/").removeprefix("./")))


@lru_cache(maxsize=256)
def _compile(glob: str) -> re.Pattern[str]:
    out: list[str] = []
    index = 0
    while index < len(glob):
        if glob.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
        elif glob.startswith("**", index):
            out.append(".*")
            index += 2
        elif glob[index] == "*":
            out.append("[^/]*")
            index += 1
        elif glob[index] == "?":
            out.append("[^/]")
            index += 1
        elif glob[index] == "[" and "]" in glob[index + 2 :]:
            end = glob.index("]", index + 2)
            body = glob[index + 1 : end]
            negated = body.startswith("!")
            body = body[1:] if negated else body
            out.append(f"[{'^' if negated else ''}{re.escape(body).replace(chr(92) + '-', '-')}]")
            index = end + 1
        else:
            out.append(re.escape(glob[index]))
            index += 1
    return re.compile("".join(out))


def matches(path: str, pattern: GlobPattern | str) -> bool:
    glob = str(pattern)
    target = normalize(path)
    if glob.endswith("/"):
        head = glob.rstrip("/")
        return target == head or target.startswith(f"{head}/") or f"/{head}/" in f"/{target}"
    compiled = _compile(glob.lstrip("/"))
    if compiled.fullmatch(target):
        return True
    if glob.startswith("/"):
        return False
    parts = target.split("/")
    return any(compiled.fullmatch("/".join(parts[i:])) for i in range(1, len(parts)))


def matches_any(path: str, patterns: Iterable[GlobPattern | str]) -> bool:
    return any(matches(path, pattern) for pattern in patterns)


def pick(paths: Iterable[str], patterns: Iterable[GlobPattern | str]) -> list[str]:
    globs = tuple(patterns)
    return [path for path in paths if matches_any(path, globs)]
