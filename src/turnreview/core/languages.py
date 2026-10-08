"""コメントを見るファイルを、どの tree-sitter の文法で読むか。

**ここに無い拡張子は見ない。** Markdown は対象外——文書は成果物であって、コードに付けた
注釈ではない。`.conf` も対象外——nginx とも INI とも取れる拡張子で、文法を決められない。
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from turnreview.core.turn import is_generated


class Language(NamedTuple):
    grammar: str  # tree-sitter の文法名
    config: bool = False
    python: bool = False


# キーは `get_parser` が受ける文法名でなければならない
CODE_GRAMMARS: dict[str, tuple[str, ...]] = {
    "python": (".py", ".pyi"),
    "bash": (".sh", ".bash"),
    "zsh": (".zsh",),
    "fish": (".fish",),
    "javascript": (".js", ".mjs", ".cjs", ".jsx"),
    "typescript": (".ts", ".mts", ".cts"),
    "tsx": (".tsx",),
    "vue": (".vue",),
    "svelte": (".svelte",),
    "astro": (".astro",),
    "html": (".html", ".htm"),
    "xml": (".xml", ".xhtml", ".svg", ".xsl", ".plist", ".xaml", ".axaml"),
    "css": (".css",),
    "scss": (".scss",),
    "less": (".less",),
    "rust": (".rs",),
    "go": (".go",),
    "c": (".c", ".h"),
    "cpp": (".cpp", ".cc", ".cxx", ".hpp", ".hh"),
    "objc": (".m", ".mm"),
    "java": (".java",),
    "kotlin": (".kt", ".kts"),
    "scala": (".scala",),
    "groovy": (".groovy", ".gradle"),
    "swift": (".swift",),
    "csharp": (".cs",),
    "fsharp": (".fs",),
    "ruby": (".rb",),
    "php": (".php",),
    "lua": (".lua",),
    "perl": (".pl", ".pm"),
    "r": (".r",),
    "julia": (".jl",),
    "dart": (".dart",),
    "zig": (".zig",),
    "nix": (".nix",),
    "nim": (".nim",),
    "tcl": (".tcl",),
    "awk": (".awk",),
    "solidity": (".sol",),
    "elixir": (".ex", ".exs"),
    "erlang": (".erl",),
    "haskell": (".hs",),
    "elm": (".elm",),
    "ocaml": (".ml", ".mli"),
    "clojure": (".clj", ".cljs"),
    "commonlisp": (".lisp",),
    "scheme": (".scm",),
    "elisp": (".el",),
    "vim": (".vim",),
    "powershell": (".ps1",),
    "asm": (".asm",),
    "vhdl": (".vhd", ".vhdl"),
    "sql": (".sql",),
    "proto": (".proto",),
    "graphql": (".graphql",),
    "latex": (".tex",),
    "typst": (".typ",),
}

CONFIG_GRAMMARS: dict[str, tuple[str, ...]] = {
    "toml": (".toml",),
    "yaml": (".yaml", ".yml"),
    "ini": (".ini", ".cfg", ".service"),
    "properties": (".properties",),
    "json5": (".json5", ".jsonc"),
    "terraform": (".tf", ".tfvars"),
    "hcl": (".hcl",),
    "cmake": (".cmake",),
    "make": (".mk",),
    "just": (".just",),
    "bash": (".env",),
}

# `.env` や `.gitignore` は `Path.suffix` が空になるので、拡張子ではなく名前で引く
CONFIG_FILENAMES: dict[str, tuple[str, ...]] = {
    "dockerfile": ("Dockerfile", "Containerfile"),
    "make": ("Makefile", "GNUmakefile"),
    "just": ("Justfile", "justfile"),
    "cmake": ("CMakeLists.txt",),
    "gitignore": (".gitignore", ".gitattributes", ".dockerignore", "CODEOWNERS"),
    "editorconfig": (".editorconfig",),
    "ini": (".npmrc",),
    "bash": (".env", ".envrc"),
}


def _table(
    code: dict[str, tuple[str, ...]], config: dict[str, tuple[str, ...]]
) -> dict[str, Language]:
    built: dict[str, Language] = {}
    for is_config, grammars in ((False, code), (True, config)):
        for grammar, keys in grammars.items():
            for key in keys:
                built[key] = Language(grammar, config=is_config, python=grammar == "python")
    return built


LANGUAGES = _table(CODE_GRAMMARS, CONFIG_GRAMMARS)
LANGUAGES_BY_NAME = _table({}, CONFIG_FILENAMES)

# 拡張子の無いスクリプトを shebang から見分ける
SHEBANG_GRAMMARS: tuple[tuple[str, Language], ...] = (
    ("python", Language("python", python=True)),
    ("zsh", Language("zsh")),
    ("bash", Language("bash")),
    ("sh", Language("bash")),
    ("ruby", Language("ruby")),
    ("perl", Language("perl")),
    ("node", Language("javascript")),
)


def is_skipped(path: Path) -> bool:
    return is_generated(path) or ".min." in path.name or ".generated." in path.name


def wants_shebang(path: Path) -> bool:
    """拡張子と名前で決まらず、1 行目の shebang を読まないと文法が分からないか。"""
    return not is_skipped(path) and not path.suffix and path.name not in LANGUAGES_BY_NAME


def language_for(path: Path, first_line: str | None = None) -> Language | None:
    """拡張子・名前・shebang から文法を決める。`first_line` は `wants_shebang` が真のときだけ要る。"""
    if is_skipped(path):
        return None
    if path.suffix:
        found = LANGUAGES.get(path.suffix.lower())
        if found:
            return found
    found = LANGUAGES_BY_NAME.get(path.name)
    if found:
        return found
    if path.suffix or not first_line or not first_line.startswith("#!"):
        return None
    for name, language in SHEBANG_GRAMMARS:
        if name in first_line:
            return language
    return None
