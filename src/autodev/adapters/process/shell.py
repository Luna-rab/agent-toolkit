"""Bash のコマンド行から、書き込みの宛先を取り出す。止めるかは決めない。

正規表現でなめず、shlex でトークンに割る。引用の中の `>`・`->`・`rm` はシェルに
届かない。引用が閉じていなければ空白と記号で割り、止める方へ倒す。リダイレクトの宛先とコマンド行の
パスは混ぜない。書き換えるコマンドは、コマンドの位置と `xargs`・`find -exec` の直後に
あるときだけ見る。

取り出しきれない形は残る（スクリプトの中から開いて書く・設定ファイルの git の別名など）。
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

_PUNCTUATION = "();<>|&\n"
_OPERATOR = re.compile(r">>|>\||&>>|&>|>&|<<<|<<-|<<|<&|<>|\|\||&&|\|&|;;|[;|&()<>\n]")
_WRITE_REDIRECTS = frozenset({">", ">>", ">|", "&>", "&>>", "<>", ">&"})
_READ_REDIRECTS = frozenset({"<", "<<", "<<-", "<<<", "<&"})
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
_DURATION = re.compile(r"\d+(?:\.\d+)?[smhd]?")
_KEYWORDS = frozenset(
    {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "!", "{", "}"}
)
#: 後ろのコマンドを走らせるだけのコマンドと、値を取るオプション（値をコマンドの名前と読まない）
_WRAPPERS: Mapping[str, frozenset[str]] = {
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "command": frozenset(),
    "builtin": frozenset(),
    "exec": frozenset({"-a"}),
    "nohup": frozenset(),
    "time": frozenset({"-f", "--format", "-o", "--output"}),
    "sudo": frozenset(
        {"-u", "--user", "-g", "--group", "-C", "-D", "-h", "--host", "-p", "-r", "-t", "-U", "-T"}
    ),
    "doas": frozenset({"-u", "-C"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({"-c", "-n", "-p"}),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "xargs": frozenset(
        {"-I", "-i", "-n", "-P", "-L", "-l", "-d", "-E", "-e", "-s", "-a", "--arg-file"}
    ),
}
_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
#: 書き換えるコマンドと、値を取るオプション
_VALUE_OPTIONS: Mapping[str, frozenset[str]] = {
    "rm": frozenset(),
    "rmdir": frozenset(),
    "tee": frozenset(),
    "touch": frozenset({"-d", "--date", "-t", "-r", "--reference"}),
    "truncate": frozenset({"-s", "--size", "-r", "--reference"}),
    "mv": frozenset({"-t", "--target-directory", "-S", "--suffix"}),
    "cp": frozenset({"-t", "--target-directory", "-S", "--suffix"}),
    "ln": frozenset({"-t", "--target-directory", "-S", "--suffix"}),
    "install": frozenset({"-t", "--target-directory", "-m", "--mode", "-o", "--owner", "-g"}),
    "rsync": frozenset({"-e", "--rsh", "--exclude", "--include", "--filter", "-f"}),
    "dd": frozenset(),
    "patch": frozenset({"-p", "-o", "--output", "-i", "--input", "-d", "--directory"}),
    "sed": frozenset({"-e", "--expression", "-f", "--file", "-l", "--line-length"}),
    "perl": frozenset({"-e", "-E", "-I", "-M", "-m"}),
    "sqlite3": frozenset({"-cmd", "-init", "-separator", "-newline", "-nullvalue"}),
}
#: 宛先が最後の引数（か `-t` の値）だけのコマンド
_COPIERS = frozenset({"cp", "ln", "install", "rsync"})
#: スクリプトを引数に取るコマンド（`-e` が無ければ最初の引数がスクリプト）
_SCRIPTED = frozenset({"sed", "perl"})
_SCRIPT_OPTIONS = frozenset({"-e", "--expression", "-f", "--file", "-E"})
#: git のサブコマンドの前に置けて、値を 1 つ取るオプション
_GIT_VALUE_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
#: worktree を書き換える git のサブコマンド。引数のパスを宛先にする
_GIT_PATH_WRITERS = frozenset({"checkout", "restore", "rm", "mv"})
_GIT_PATH_VALUE_OPTIONS = frozenset({"-b", "-B", "--source", "-s"})
#: worktree 全体を書き換えうる git のサブコマンド
_GIT_TREE_WRITERS = frozenset({"clean", "apply", "am"})
_FIND_EXEC = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
#: 入れ子（`bash -c`・置換）をたどる深さ。超えたら名前の決まらないコマンドとして止める側に倒す
_MAX_NESTING = 4


@dataclass(frozen=True)
class SimpleCommand:
    """パイプや `;` で区切られた 1 つのコマンド。リダイレクトは外して `redirects` に分ける。"""

    words: tuple[str, ...]
    #: `>`・`>>` などの書き込みの宛先。`/dev/…` と fd の複製（`2>&1`）は外してある
    redirects: tuple[str, ...] = ()


# --- 割る ---

_HEREDOC = re.compile(r"<<(-?)[ \t]*(?:(['\"])(.+?)\2|\\(\w+)|(\w+))")


def strip_heredocs(command: str) -> tuple[str, list[str]]:
    """ヒアドキュメントの本文を外す。本文はコマンドではない（`a > b` を書き込みと読まない）。

    始まりとみなすのは、引用の外の `<<`（`<<<` ではない）だけ。区切りを引用していない本文は
    `$(…)` が展開されるので、置換を探すために返す。
    """
    out: list[str] = []
    expanding: list[str] = []
    pending: list[tuple[bool, str, bool]] = []
    quote: str | None = None
    index, size = 0, len(command)
    while index < size:
        char = command[index]
        if char == "\\" and quote != "'" and index + 1 < size:
            out.append(command[index : index + 2])
            index += 2
            continue
        if quote is not None:
            quote = None if char == quote else quote
            out.append(char)
            index += 1
            continue
        if char in "'\"":
            quote = char
        elif command.startswith("<<", index) and not command.startswith("<<<", index):
            found = _HEREDOC.match(command, index)
            if found:
                delimiter = found.group(3) or found.group(4) or found.group(5)
                pending.append((found.group(1) == "-", delimiter, found.group(5) is not None))
                out.append(command[index : found.end()])
                index = found.end()
                continue
        elif char == "\n" and pending:
            out.append("\n")
            index = _skip_bodies(command, index + 1, pending, expanding)
            pending = []
            continue
        out.append(char)
        index += 1
    return "".join(out), expanding


def _skip_bodies(
    command: str, index: int, pending: list[tuple[bool, str, bool]], expanding: list[str]
) -> int:
    for strip_tabs, delimiter, expands in pending:
        body: list[str] = []
        while index < len(command):
            end = command.find("\n", index)
            line = command[index : end if end != -1 else len(command)]
            index = end + 1 if end != -1 else len(command)
            if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                break
            body.append(line)
        if expands:
            expanding.append("\n".join(body))
    return index


def _closing(text: str, start: int) -> int:
    """`text[start]` の `(` に対応する `)` の位置。閉じていなければ末尾。"""
    depth, quote, index = 0, None, start
    while index < len(text):
        char = text[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if quote is not None:
            quote = None if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return len(text)


def substitutions(command: str) -> list[str]:
    """コマンド置換（`$(…)`・バッククォート）とプロセス置換（`<(…)`・`>(…)`）の中身。

    二重引用の中も展開されるので見る。単一引用の中は見ない。
    """
    found: list[str] = []
    in_double = False
    index, size = 0, len(command)
    while index < size:
        char = command[index]
        if char == "\\":
            index += 2
            continue
        if char == "'" and not in_double:
            end = command.find("'", index + 1)
            index = size if end == -1 else end + 1
            continue
        if char == '"':
            in_double = not in_double
        elif char == "`":
            end = index + 1
            while end < size and command[end] != "`":
                end += 2 if command[end] == "\\" else 1
            found.append(command[index + 1 : end])
            index = end + 1
            continue
        elif char in "$<>" and command.startswith("(", index + 1):
            end = _closing(command, index + 1)
            found.append(command[index + 2 : end])
            index = end + 1
            continue
        index += 1
    return found


def _tokens(text: str) -> list[str]:
    # バッククォートは語の一部のまま残す。`` `pwd`/a `` を 1 つのパスとして読み（場所が決まらない）、
    # コマンドの位置なら名前が決まらないコマンドになる。中身は `substitutions` が見る
    lexer = shlex.shlex(text, posix=True, punctuation_chars=_PUNCTUATION)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    # `#` は語の頭でだけ注釈になるが、引用を外した後では見分けられない。注釈として飲ませると、
    # 改行まで（次のコマンドの切れ目まで）消える。注釈として扱わないほうが止める側に倒れる
    lexer.commenters = ""
    try:
        raw = list(lexer)
    except ValueError:
        raw = re.findall(r"[();<>|&\n]+|[^\s();<>|&]+", text)
    tokens: list[str] = []
    for token in raw:
        if _is_operator(token):
            tokens += _OPERATOR.findall(token)
        else:
            tokens.append(token)
    return tokens


def _is_operator(token: str) -> bool:
    return bool(token) and all(ch in _PUNCTUATION for ch in token)


def simple_commands(text: str) -> list[SimpleCommand]:
    """ヒアドキュメントの本文を外したコマンド行を、単純コマンドに割る。"""
    tokens = _tokens(text)
    found: list[SimpleCommand] = []
    words: list[str] = []
    redirects: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        word_follows = following is not None and not _is_operator(following)
        if not _is_operator(token):
            # `2>&1` の `2` は fd の番号で、引数ではない
            if not (token.isdigit() and following in _WRITE_REDIRECTS | _READ_REDIRECTS):
                words.append(token)
            index += 1
        elif token in _WRITE_REDIRECTS:
            if word_follows and following is not None:
                duplicated = token == ">&" and (following.isdigit() or following == "-")
                if not duplicated and not following.startswith("/dev/"):
                    redirects.append(following)
                index += 2
            else:
                # `>(…)` はプロセス置換（中身は `substitutions` が見る）
                index += 1
        elif token in _READ_REDIRECTS:
            index += 2 if word_follows else 1
        else:
            if words or redirects:
                found.append(SimpleCommand(tuple(words), tuple(redirects)))
            words, redirects = [], []
            index += 1
    if words or redirects:
        found.append(SimpleCommand(tuple(words), tuple(redirects)))
    return found


def name_of(word: str) -> str:
    return os.path.basename(word)


def expand(path: str) -> str:
    """`~` と、フックの環境にある変数を展開する。コマンド行の中で決めた変数は展開されずに残る。"""
    return os.path.expandvars(os.path.expanduser(path))


def is_literal(path: str) -> bool:
    return not any(ch in path for ch in "$`*?[")


# --- 起点 ---


@dataclass(frozen=True)
class Where:
    """相対パスを数える起点の候補。cd で動いても、前の起点も候補に残す（サブシェルの cd は戻る）。

    `known` が偽なら、cd の引数が決まらず、相対パスの場所は分からない。
    """

    cwds: tuple[str, ...]
    known: bool = True

    def moved(self, argument: str | None, home: str) -> Where:
        """`cd <argument>` の後。"""
        if argument is None:
            return Where((*self.cwds, home), self.known)
        into = self.into(argument)
        return Where(tuple(dict.fromkeys((*self.cwds, *into.cwds))), into.known)

    def into(self, argument: str) -> Where:
        """`git -C <argument>` のように、そこで走ることが決まっている起点。"""
        expanded = expand(argument)
        if argument == "-" or not is_literal(expanded):
            return Where(self.cwds, known=False)
        return Where(tuple(os.path.join(c, expanded) for c in self.cwds), self.known)


@dataclass
class BashFacts:
    """コマンド行から取り出した、書き込みの宛先。"""

    #: （コマンド行のパス, 起点の候補）
    writes: list[tuple[str, Where]] = field(default_factory=list)
    #: 入れ子が深すぎて読みきれなかった。書き込みの宛先が決まらない
    unreadable: bool = False


def bash_facts(command: str, cwd: str, home: str) -> BashFacts:
    scanner = _Scanner(home)
    scanner.collect(command, Where((cwd,)), 0)
    return scanner.facts


# --- 本体のコマンド ---


@dataclass
class _Body:
    """前に置いた代入・予約語・ラッパーを外した本体のコマンド。"""

    words: list[str]
    #: `xargs` の後ろ（引数は標準入力から足される）
    via_xargs: bool = False
    #: `env -S '<文字列>'` の中のコマンド行
    scripts: list[str] = field(default_factory=list)
    #: ラッパーが作業ディレクトリを変える指定（`env -C <dir>`・`sudo -D <dir>`）。指定された順
    chdirs: list[str] = field(default_factory=list)


#: 作業ディレクトリを変えるラッパーと、そのオプション
_CHDIR_OPTIONS: Mapping[str, tuple[str, ...]] = {
    "env": ("-C", "--chdir"),
    "sudo": ("-D", "--chdir"),
}


def _attached_chdir(wrapper: str, word: str) -> str | None:
    """`--chdir=<dir>`・`-C<dir>` のように、値がオプションに続く形の dir。"""
    for option in _CHDIR_OPTIONS.get(wrapper, ()):
        if option.startswith("--"):
            if word.startswith(option + "="):
                return word[len(option) + 1 :]
        elif word.startswith(option) and len(word) > len(option):
            return word[len(option) :]
    return None


def _skippable(wrapper: str, word: str) -> bool:
    return (
        word.startswith("-")
        or bool(_ASSIGNMENT.fullmatch(word))
        or (wrapper == "timeout" and bool(_DURATION.fullmatch(word)))
    )


def _body(words: Sequence[str]) -> _Body:
    rest = list(words)
    body = _Body(rest)
    while rest:
        head = rest[0]
        if _ASSIGNMENT.fullmatch(head) or head in _KEYWORDS:
            rest.pop(0)
            continue
        wrapper = name_of(head)
        options = _WRAPPERS.get(wrapper)
        if options is None:
            break
        rest.pop(0)
        body.via_xargs = body.via_xargs or wrapper == "xargs"
        while rest:
            word = rest[0]
            if word == "--":
                rest.pop(0)
                break
            attached = _attached_chdir(wrapper, word)
            if attached is not None:
                body.chdirs.append(attached)
                rest.pop(0)
            elif word in options and len(rest) > 1:
                if wrapper == "env" and word in {"-S", "--split-string"}:
                    body.scripts.append(rest[1])
                if word in _CHDIR_OPTIONS.get(wrapper, ()):
                    body.chdirs.append(rest[1])
                del rest[:2]
            elif _skippable(wrapper, word):
                rest.pop(0)
            else:
                break
    body.words = rest
    return body


def command_words(words: Sequence[str]) -> list[str]:
    """先頭の代入・予約語・ラッパー（`env`・`timeout`・`nohup` など）を外した、本体のコマンドの語。"""
    return _body(words).words


def _operands(words: Iterable[str], value_options: frozenset[str] = frozenset()) -> list[str]:
    """オプションと（値を取るオプションの）値を除いた引数。`--` の後ろはすべて引数。"""
    found: list[str] = []
    rest_are_operands = False
    skip = False
    for word in words:
        if skip:
            skip = False
        elif rest_are_operands:
            found.append(word)
        elif word == "--":
            rest_are_operands = True
        elif word in value_options:
            skip = True
        elif not word.startswith("-"):
            found.append(word)
    return found


def _option_values(words: Sequence[str], names: Iterable[str]) -> list[str]:
    values: list[str] = []
    for index, word in enumerate(words):
        for option in names:
            if word == option and index + 1 < len(words):
                values.append(words[index + 1])
            elif option.startswith("--") and word.startswith(f"{option}="):
                values.append(word[len(option) + 1 :])
    return values


def _in_place(command: str, rest: Sequence[str]) -> bool:
    if command == "sed":
        return any(
            w == "--in-place" or w.startswith("--in-place=") or re.fullmatch(r"-[A-Za-z]*i.*", w)
            for w in rest
        )
    return any(re.fullmatch(r"-[A-Za-z0-9]*i.*", w) for w in rest)


def _mutated(command: str, rest: Sequence[str], via_xargs: bool) -> list[str]:
    """書き換えるコマンド（コマンドの位置にあるもの）の、書き換えうる宛先。"""
    options = _VALUE_OPTIONS.get(command)
    if options is None:
        return []
    operands = _operands(rest, options)
    found: list[str]
    if command == "dd":
        found = [w[len("of=") :] for w in rest if w.startswith("of=")]
    elif command in _SCRIPTED:
        scripted = any(w in _SCRIPT_OPTIONS for w in rest)
        found = (operands if scripted else operands[1:]) if _in_place(command, rest) else []
    elif command == "sqlite3":
        found = operands[:1]
    elif command in _COPIERS:
        found = _option_values(rest, ["-t", "--target-directory"]) or operands[-1:]
    else:
        found = operands
    if not found and (via_xargs or command == "patch"):
        # 宛先を標準入力（`xargs rm`）や差分（patch）から読むので、cwd を宛先とみなす
        return ["."]
    return found


class _Scanner:
    def __init__(self, home: str) -> None:
        self.home = home
        self.facts = BashFacts()

    def collect(self, command: str, where: Where, depth: int) -> None:
        if depth > _MAX_NESTING:
            self.facts.unreadable = True
            return
        text, expanding = strip_heredocs(command)
        inner = [*substitutions(text), *(s for body in expanding for s in substitutions(body))]
        for script in inner:
            self.collect(script, where, depth + 1)
        for simple in simple_commands(text):
            self.facts.writes += [(path, where) for path in simple.redirects]
            body = _body(simple.words)
            for script in body.scripts:
                self.collect(script, where, depth + 1)
            where = self.run(body.words, where, depth, via_xargs=body.via_xargs)

    def run(self, words: Sequence[str], where: Where, depth: int, *, via_xargs: bool) -> Where:
        """本体のコマンド 1 つ。cd なら動いた後の起点を返す。"""
        if not words:
            return where
        command = expand(words[0])
        if "$" in command or "`" in command:
            # 名前が決まらないので、何を書くかも分からない。止めない
            return where
        name, rest = name_of(command), list(words[1:])
        if name in {"cd", "pushd"}:
            # `cd -` の `-` は前の場所で、オプションではない
            argument = next((w for w in rest if w == "-" or not w.startswith("-")), None)
            return where.moved(argument, self.home)
        if name == "git":
            self._git(rest, where, depth)
        elif name in _SHELLS:
            for index, word in enumerate(rest[:-1]):
                if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", word):
                    self.collect(rest[index + 1], where, depth + 1)
                    break
        elif name == "eval":
            self.collect(" ".join(rest), where, depth + 1)
        elif name == "find":
            self._find(rest, where, depth)
        else:
            self.facts.writes += [(path, where) for path in _mutated(name, rest, via_xargs)]
        return where

    def _git(self, rest: Sequence[str], where: Where, depth: int) -> None:
        index = 0
        while index < len(rest):
            word = rest[index]
            if word in _GIT_VALUE_OPTIONS and index + 1 < len(rest):
                value = rest[index + 1]
                if word == "-C":
                    where = where.into(value)
                elif word == "-c":
                    self._git_alias(value, where, depth)
                index += 2
            elif word.startswith("-"):
                index += 1
            else:
                break
        if index >= len(rest):
            return
        subcommand, after = rest[index], rest[index + 1 :]
        if subcommand in _GIT_PATH_WRITERS:
            paths = _operands(after, _GIT_PATH_VALUE_OPTIONS)
            self.facts.writes += [(path, where) for path in paths]
        elif subcommand in _GIT_TREE_WRITERS or (subcommand == "reset" and "--hard" in after):
            self.facts.writes.append((".", where))

    def _git_alias(self, setting: str, where: Where, depth: int) -> None:
        """`git -c alias.p=push p` を見る。`!` で始まる別名はシェルのコマンド行として見る。"""
        key, _, value = setting.partition("=")
        if not key.lower().startswith("alias."):
            return
        if value.startswith("!"):
            self.collect(value[1:], where, depth + 1)

    def _find(self, rest: Sequence[str], where: Where, depth: int) -> None:
        starts: list[str] = []
        index = 0
        while index < len(rest) and rest[index] in {"-H", "-L", "-P"}:
            index += 1
        while index < len(rest) and not rest[index].startswith(("-", "(", "!", ")")):
            starts.append(rest[index])
            index += 1
        starts = starts or ["."]
        if "-delete" in rest:
            self.facts.writes += [(path, where) for path in starts]
        for position, word in enumerate(rest):
            if word not in _FIND_EXEC:
                continue
            inner: list[str] = []
            for part in rest[position + 1 :]:
                if part in {";", "+"}:
                    break
                # `{}` は見つかったファイル。探し始めた場所を宛先とみなす
                inner += starts if part == "{}" else [part]
            nested = _body(inner)
            self.run(nested.words, where, depth + 1, via_xargs=False)
