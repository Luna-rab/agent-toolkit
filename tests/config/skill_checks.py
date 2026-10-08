"""SKILL.md の検査。スキルのディレクトリを受け取り、落ちた件の一覧を返す。

次の 4 つを検査する。

1. frontmatter（`SKILL.md` の先頭にある `---` で挟まれた YAML）が YAML として読めること
2. `SKILL.md` が 500 行以下であること（基準は `editing-skills/SKILL.md`）
3. スキル内の `*.md` から張られた相対リンクの参照先が実在すること
4. `*.md` が参照する `scripts/` 配下のファイルが実在し、直接起動されるファイル
   （`.sh`、および 1 行目が `#!` で始まるファイル）に実行権限があること

入口は `check_skills_root(skills_root)`。スキルが 1 つも無いディレクトリも失敗として返す。
"""

from __future__ import annotations

import errno
import os
import re
import stat
from collections.abc import Iterator
from collections.abc import Set as AbstractSet
from pathlib import Path

import yaml

# --- 定数 ---

# `editing-skills/SKILL.md` が定める SKILL.md の行数上限。
MAX_SKILL_MD_LINES = 500

# 検査名。CI や grep から使うので ASCII の固定文字列にする（説明文は日本語で変わりうる）。
CHECK_FRONTMATTER = "frontmatter"
CHECK_LINE_LIMIT = "line-limit"
CHECK_MD_LINK = "md-link"
CHECK_SCRIPT_REF = "script-ref"
CHECK_READ = "read"

# `Path.stat()` が「そのパスは無い」を意味して投げる errno。`Path.exists()` が False を返す
# のと同じ集合にそろえてある。これ以外（EACCES = 権限が無い、など）は「確かめられない」として
# 1 件の失敗に記録し、走査は止めない。
MISSING_ERRNOS = (errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP)

# `[表示文字](リンク先)`。リンク先は 2 通りの書き方を受ける。
#   - 山括弧つき `[x](<a b.md>)` — CommonMark がリンク先に空白を許すときの書き方
#   - 素の形 `[x](a.md "title")` — 空白と `)` の手前まで（`"title"` は落ちる）
# 表示文字に `[]` が 1 段だけ入る形（`[see [x] here](a.md)`）も拾う。
# 拾わない書き方: 表示文字を 2 段以上入れ子にした形、リンク先に丸括弧を含む形
# （`[x](a(b).md)`）、参照方式の使用側（`[x][r1]`。定義側の `[r1]: a.md` は REF_DEF_RE で拾う）。
LINK_TEXT = r"\[(?:[^\[\]]|\[[^\[\]]*\])*\]"
LINK_RE = re.compile(LINK_TEXT + r"\(\s*(?:<(?P<angle>[^<>]*)>|(?P<target>[^)\s]+))")

# 参照方式のリンク定義 `[r1]: a.md`。行頭（インデントは CommonMark に合わせて 3 桁まで）。
# ラベルが `^` で始まる行は除く。GitHub Flavored Markdown の脚注定義 `[^1]: 説明文` が
# 同じ形をしているためで、除かないと説明文をリンク先として拾う。日本語は語の間に空白を
# 入れないので `\S+` が説明文を丸ごと飲み、末尾がたまたま `.md` になると
# 「リンク先が無い: 設定はsettings.md」と誤って落とす。CommonMark の参照方式で
# ラベルを `^` から始める慣習は無いので、除いても取りこぼしは増えない。
REF_DEF_RE = re.compile(
    r"^ {0,3}\[(?P<label>[^\^\]][^\]]*)\]:\s*(?:<(?P<angle>[^<>]*)>|(?P<target>\S+))"
)

# HTML で書いたリンク `<a href="a.md">`。markdown の中に直接書ける。
HTML_LINK_RE = re.compile(
    r"<a\s[^>]*?href\s*=\s*"
    r"(?:\"(?P<dquote>[^\"]*)\"|'(?P<squote>[^']*)'|(?P<bare>[^\s>]+))",
    re.IGNORECASE,
)

# コードブロックの囲い（フェンス）。CommonMark に合わせて、記号は ``` と ~~~ の 2 種類、
# 3 つ以上並べる、前のインデントは 3 桁まで。開いた記号と同じ記号でしか閉じられない。
FENCE_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$")

# `https:` や `mailto:` のようなスキーム付き。相対リンクではないので検査しない。
SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")

# リンクを検査しなかった理由。集計行に内訳として出す。「対象外」を 1 つの数にまとめると、
# 拾い漏らしたリンクが外部 URL に紛れて見えなくなるので分けて数える。
LINK_SKIP_URL = "スキーム付き URL"
LINK_SKIP_ANCHOR = "ページ内リンク"
LINK_SKIP_ABSOLUTE = "ホーム基準や絶対のパス"
LINK_SKIP_NOT_MD = ".md ではない"
LINK_SKIP_REASONS = (
    LINK_SKIP_URL,
    LINK_SKIP_ANCHOR,
    LINK_SKIP_ABSOLUTE,
    LINK_SKIP_NOT_MD,
)

# パス名に使う文字。ここを「使ってよい文字」の側で書くのは、`scripts/x.sh; echo ...` のような
# シェルの例（hooks.md の動作確認）で `;` まで取り込んで参照先を見失わないようにするため。
PATH_CHARS = r"A-Za-z0-9._/\-"

SKILL_NAME_CHARS = r"A-Za-z0-9._\-"

# `scripts/` の直前 1 文字がこれなら参照として拾わない。目的は 2 つだけである。
#   - 別語の一部を弾く（`myscripts/x.sh` の `scripts`）
#   - スキルの外の `scripts/` を弾く（`dist/dot-claude/scripts/statusline.py`、`docs/scripts/x.sh`）
# ASCII に限るのが要点。`\w` は Python 3 では Unicode に当たるため、このリポジトリのスキルが
# 全文日本語であることと相まって `詳細はscripts/x.sh` を取りこぼしていた。
NOT_BEFORE = r"A-Za-z0-9_/~.\-"

# 参照の直前がこの形なら、git のコミットの中のパス（`56b72fa:scripts/x.py`）で、作業ツリーの
# ファイルではない。消したファイルを、消す前のコミットで指すときに書く。検査はせず、件数だけ
# `Counts.script_refs_at_revision` に数えて集計行に出す。ブランチ名（`main:`）は除外しない。
# ブランチは動くので、マージの後に指す先が変わる
AT_REVISION_RE = re.compile(r"(?<![A-Za-z0-9_])[0-9a-f]{7,40}:$")

# `scripts/` 配下への参照。4 つの書き方を 1 本の正規表現で拾う。並び順に意味がある
# （前の候補から順に試されるので、より長い書き方を先に置く）。
#   1. skills ルートから書いた形（`~/.claude/skills/autodev/scripts/autodev.py`）
#      → skills ルートから解決する
#   2. 隣のスキルを相対で指す形（`../autodev/scripts/autodev.py`）
#      → そのスキルのディレクトリの 1 つ上から解決する
#   3. 同じスキルの中を指す形（`scripts/x.sh`、`./scripts/x.sh`）
#      → そのファイルが属するスキルのディレクトリから解決する
#   4. スキル名から書いた形（`autodev/scripts/autodev.py`）
#      → 1 と同じ skills ルートから解決する。ただし名前が実在するスキルのときだけ参照として
#        扱う（`docs/scripts/x.sh` や `.claude/scripts/x.sh` を誤って拾わないため）
# 拾わない書き方: `../scripts/x.sh`（`..` はスキル名ではないので 4 の検証で落ちる）、
# 実在しないスキル名から書いた形（同上。名前を間違えた参照は検査されないが、捨てた件数は
# `Counts.script_refs_unknown_skill` に数えて集計行に出す）。
# 3 の `./scripts/` は、書かれた場所のスキルの `scripts/` とみなす。検査スクリプトはシェルの
# 作業ディレクトリを追えないので、直前の `cd` は見ない。スキルのディレクトリ以外へ移動してから
# 叩く例（`cd .claude && ./scripts/run.py`）を書くと、実在するファイルを
# 「参照先が無い」と誤って報告する。そういう例は `cd` 先からの形（`.claude/scripts/...`）で
# 書けば、1〜4 のどれにも当たらないので検査されない。
SCRIPT_REF_RE = re.compile(
    rf"\.claude/skills/(?P<root_skill>[{SKILL_NAME_CHARS}]+)/scripts/(?P<root_path>[{PATH_CHARS}]+)"
    rf"|(?<![{NOT_BEFORE}])\.\./(?P<up_skill>[{SKILL_NAME_CHARS}]+)/scripts/(?P<up_path>[{PATH_CHARS}]+)"
    rf"|(?<![{NOT_BEFORE}])(?:\./)?scripts/(?P<same_path>[{PATH_CHARS}]+)"
    rf"|(?<![{NOT_BEFORE}])(?P<named_skill>[{SKILL_NAME_CHARS}]+)/scripts/(?P<named_path>[{PATH_CHARS}]+)"
)

# 参照の直後に付く句読点。パス名の一部ではないので落とす。
TRAILING_PUNCT = "。、，．；：！？."

# `scripts/` 参照を解決するときの起点。どの書き方がどれを使うかは check_script_refs() の
# docstring に書いてある。
REF_BASE_SKILLS_ROOT = "skills-root"  # skills ルート（スキルを名前で指した形）
REF_BASE_MD_PARENT = "md-parent"  # その md が置かれたディレクトリ（`../` で書いた形）
REF_BASE_SKILL_DIR = "skill-dir"  # そのファイルが属するスキルのディレクトリ


class Failure:
    """検査に落ちた 1 件。パス・行番号・検査名・説明を持つ。"""

    def __init__(self, path: Path | str, line: int, check: str, message: str) -> None:
        self.path = str(path)
        self.line = line
        self.check = check
        self.message = message

    def sort_key(self) -> tuple[str, int, str]:
        return (self.path, self.line, self.check)

    def format(self) -> str:
        return f"{self.path}:{self.line}: {self.check}: {self.message}"


class Counts:
    """検査した件数。0 件しか見ていないのに「問題なし」と出る状態を見分けるために数える。"""

    def __init__(self) -> None:
        self.skills = 0
        self.files = 0
        self.links_checked = 0
        self.links_skipped = dict.fromkeys(LINK_SKIP_REASONS, 0)
        self.script_refs = 0
        # `<スキル名>/scripts/x.sh` の形で、スキル名が実在しないので検査しなかった件数。
        self.script_refs_unknown_skill = 0
        # `56b72fa:scripts/x.py` の形で、コミットの中のパスなので検査しなかった件数。
        self.script_refs_at_revision = 0

    def links_skipped_total(self) -> int:
        return sum(self.links_skipped.values())

    def links_skipped_detail(self) -> str:
        return "・".join(f"{reason} {self.links_skipped[reason]}" for reason in LINK_SKIP_REASONS)


# --- ファイルの読み込み ---


def read_text(path: Path) -> tuple[str | None, Failure | None]:
    """テキストを読む。読めなければ (None, Failure) を返す。

    UTF-8 以外や読み取り権限の無いファイルで例外を投げて止まると、残りのスキルが検査されない。
    1 件の失敗として記録して先へ進む。
    """
    try:
        return path.read_text(encoding="utf-8-sig"), None
    except (OSError, UnicodeDecodeError) as err:
        return None, Failure(path, 1, CHECK_READ, f"読めない: {err}")


def stat_path(path: Path) -> tuple[os.stat_result | None, str | None]:
    """`path` の stat を取る。戻り値は (stat 結果, 確かめられない理由)。

    - (stat 結果, None): 在る
    - (None, None): 無い
    - (None, 理由): 権限が無いなどで確かめられない（理由には対象のパスも含まれる）

    `Path.exists()` を直に呼ばないのは、EACCES（権限が無い）で例外が外へ出るからである。
    `scripts/` を `chmod 000` にしたスキルが 1 つあるだけで走査全体が traceback で止まり、
    後続のスキルが 1 つも検査されなくなる。read_text() と同じく、1 件の失敗として記録して
    先へ進めるようにここで受ける。

    `ValueError` も受ける。`Path.stat()` はパスに NUL バイト（`\\x00`）が入っていると
    `ValueError: embedded null byte` を投げ、これは `OSError` ではないので取りこぼすと
    走査が止まる。`ValueError` に `errno` は無いので、有無を `getattr` で見て
    「確かめられない」に振り分ける。
    """
    try:
        return path.stat(), None
    except (OSError, ValueError) as err:
        if getattr(err, "errno", None) in MISSING_ERRNOS:
            return None, None
        return None, str(err)


def starts_with_shebang(path: Path) -> tuple[bool | None, str | None]:
    """1 行目が `#!` で始まるかを返す。戻り値は (真偽, 読めない理由)。

    読めない理由には対象のパスも含まれる。read_text() と同じく、例外で走査を止めない。
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(8)
    except OSError as err:
        return None, str(err)
    # UTF-8 の BOM を付けて保存されたスクリプトでも 1 行目を見分けられるようにする。
    return head.lstrip(b"\xef\xbb\xbf").startswith(b"#!"), None


# --- 検査 1: frontmatter ---


def split_frontmatter(text: str) -> tuple[str | None, str | None]:
    """先頭の frontmatter を取り出す。(YAML 本文, エラー説明) を返す。

    frontmatter は 1 行目の `---` で始まり、次に現れる `---` または `...` で終わる。
    1 行目が `---` でなければ frontmatter が無い。閉じが無ければ、本文全体を YAML として
    読ませない（400 行の markdown が YAML として何かに解釈されて通ってしまうのを避ける）。
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, "frontmatter が無い（1 行目が `---` ではない）"
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() in ("---", "..."):
            return "\n".join(lines[1:i]), None
    return None, "frontmatter が閉じていない（2 行目以降に `---` が無い）"


def check_frontmatter(skill_md: Path, text: str) -> list[Failure]:
    """SKILL.md の frontmatter が YAML の mapping として読めることを確かめる。"""
    source, error = split_frontmatter(text)
    if error is not None:
        return [Failure(skill_md, 1, CHECK_FRONTMATTER, error)]
    try:
        data = yaml.safe_load(source)
    except yaml.YAMLError as err:
        detail = getattr(err, "problem", None) or str(err).replace("\n", " ")
        mark = getattr(err, "problem_mark", None)
        line = mark.line + 2 if mark is not None else 1  # +2: frontmatter は 2 行目から
        return [
            Failure(
                skill_md,
                line,
                CHECK_FRONTMATTER,
                f"YAML として読めない: {detail}",
            )
        ]
    if not isinstance(data, dict):
        return [
            Failure(
                skill_md,
                1,
                CHECK_FRONTMATTER,
                f"frontmatter が mapping ではない（{type(data).__name__}）",
            )
        ]
    return []


# --- 検査 2: 行数 ---


def count_lines(text: str) -> int:
    """エディタや `wc -l` と同じ数え方で行数を返す。

    `str.splitlines()` を使わないのは、改行のほかに `\\v` `\\f` `\\x1c` `\\x1d` `\\x1e`
    `\\x85` `U+2028` `U+2029` まで行の区切りに数えるからである。`editing-skills` スキル
    の「500 行以下」はエディタが数える行のことなので、基準がずれる（`\\f` を各行に入れた
    403 行の SKILL.md が 803 行と報告されていた）。

    末尾の改行は行を増やさない。`"a\\n" * 500` と `"a\\n" * 499 + "a"` はどちらも 500 行。
    """
    if not text:
        return 0
    count = text.count("\n")
    if not text.endswith("\n"):
        count += 1
    return count


def check_line_limit(skill_md: Path, text: str) -> list[Failure]:
    """SKILL.md が 500 行以下であることを確かめる。"""
    count = count_lines(text)
    if count > MAX_SKILL_MD_LINES:
        return [
            Failure(
                skill_md,
                MAX_SKILL_MD_LINES + 1,
                CHECK_LINE_LIMIT,
                f"{count} 行（上限 {MAX_SKILL_MD_LINES} 行）",
            )
        ]
    return []


# --- 検査 3: 相対リンク ---


def iter_md_lines(text: str) -> Iterator[tuple[int, str, bool]]:
    """md の各行を (行番号, 行, コードブロックの内側か) で返す。

    コードブロックはフェンス（``` または ~~~ を 3 つ以上並べた行）で囲まれた範囲である。
    開いた記号と同じ記号で、開いたときと同じ数以上並べた行でしか閉じない。閉じる側の行に
    情報文字列（```markdown の `markdown` の部分）は書けないので、付いていたら閉じとみなさない。
    フェンスの行そのものは内側として返す（囲いの記号であって本文ではない）。
    """
    fence_char = ""
    fence_len = 0
    for lineno, line in enumerate(text.split("\n"), start=1):
        match = FENCE_RE.match(line)
        marker = match.group("fence") if match is not None else ""
        info = match.group("info").strip() if match is not None else ""
        if fence_char:
            if marker[:1] == fence_char and len(marker) >= fence_len and not info:
                fence_char, fence_len = "", 0
            yield lineno, line, True
            continue
        # ``` で開くときは情報文字列にバッククォートを書けない（CommonMark の規定）。
        if marker and (marker[0] == "~" or "`" not in info):
            fence_char, fence_len = marker[0], len(marker)
            yield lineno, line, True
            continue
        yield lineno, line, False


def iter_link_targets(line: str, in_fence: bool = False) -> Iterator[str]:
    """1 行に書かれたリンク先を順に返す。

    3 通りの書き方を拾う。`[x](a.md)`（山括弧つきの `[x](<a.md>)` を含む）、
    参照方式の定義 `[r1]: a.md`、HTML の `<a href="a.md">`。

    コードブロックの内側（`in_fence` が真）では、後ろの 2 つ（参照方式の定義と HTML の
    リンク）を拾わない。この 2 つは「markdown の書き方を説明する文書」でコードブロックの中に
    例として置かれるのが典型で、そこを検査すると中身が正しい文書を誤って落とす。
    `[x](a.md)` の形はコードブロックの中も検査する。スキルの文書はコマンド例や設定例を
    コードブロックに入れて書くので、そこを外すと検査の主戦場が抜け落ちるからである
    （`scripts/` 参照 23 件のうち 17 件がコードブロックの中にある）。
    """
    for match in LINK_RE.finditer(line):
        angle = match.group("angle")
        yield angle.strip() if angle is not None else match.group("target")
    if in_fence:
        return
    ref_def = REF_DEF_RE.match(line)
    if ref_def is not None:
        angle = ref_def.group("angle")
        yield angle.strip() if angle is not None else ref_def.group("target")
    for match in HTML_LINK_RE.finditer(line):
        for name in ("dquote", "squote", "bare"):
            value = match.group(name)
            if value is not None:
                yield value.strip()
                break


def link_skip_reason(target: str) -> str | None:
    """リンクの実在を確かめないなら理由を、確かめるなら None を返す。

    確かめないのは次の 4 つ。いずれもスキル内のファイルを指していない。
    - `https://...` のようなスキーム付き
    - `#見出し` だけのページ内リンク
    - `/` で始まる絶対パスと `~/` で始まるホーム基準のパス（どちらもそのファイルからの
      相対ではない。`~` はシェルが展開する記号であってパスの一部ではないので、
      `<スキルのディレクトリ>/~/...` を探しにいくと実在するファイルを誤って落とす）
    - `.md` で終わらない参照先（DoD が検査対象を `*.md` に限っている）
    """
    if SCHEME_RE.match(target):
        return LINK_SKIP_URL
    if target.startswith("#"):
        return LINK_SKIP_ANCHOR
    path_part = target.split("#", 1)[0]
    if not path_part:
        return LINK_SKIP_ANCHOR
    if path_part.startswith(("/", "~")):
        return LINK_SKIP_ABSOLUTE
    if not path_part.lower().endswith(".md"):
        return LINK_SKIP_NOT_MD
    return None


def check_md_links(md_path: Path, text: str, counts: Counts) -> list[Failure]:
    """md から張られた相対リンクの参照先が実在することを確かめる。"""
    failures = []
    for lineno, line, in_fence in iter_md_lines(text):
        for target in iter_link_targets(line, in_fence):
            reason = link_skip_reason(target)
            if reason is not None:
                counts.links_skipped[reason] += 1
                continue
            rel = target.split("#", 1)[0]
            counts.links_checked += 1
            result, error = stat_path(md_path.parent / rel)
            if error is not None:
                failures.append(
                    Failure(
                        md_path,
                        lineno,
                        CHECK_READ,
                        f"リンク先を確かめられない: {error}",
                    )
                )
            elif result is None:
                failures.append(
                    Failure(
                        md_path,
                        lineno,
                        CHECK_MD_LINK,
                        f"リンク先が無い: {rel}",
                    )
                )
    return failures


# --- 検査 4: scripts/ 配下への参照 ---


def iter_script_refs(text: str) -> Iterator[tuple[int, str | None, str, str, bool]]:
    """`scripts/` 配下への参照を (行番号, スキル名 or None, 相対パス, 起点, 名前検証の要否) で返す。

    「起点」は解決の基準にするディレクトリの種類で、次の 3 つ。値は check_script_refs() が
    実際のパスに変える。
    - REF_BASE_SKILLS_ROOT: skills ルート（`<スキル名>` を名前として書いた形）
    - REF_BASE_MD_PARENT: その md が置かれたディレクトリ（`../` で書いた形）
    - REF_BASE_SKILL_DIR: そのファイルが属するスキルのディレクトリ（`scripts/x.sh` の形）

    5 番目の値が True の書き方（`autodev/scripts/x.sh`）は、スキル名の位置に
    何が書かれていても形が同じなので、実在するスキル名のときだけ参照として扱う。
    そうしないと `docs/scripts/x.sh` を「スキル docs の参照」と誤って拾ってしまう。

    コミットの中のパス（`56b72fa:scripts/x.py`）は返さない。件数は count_revision_refs() が数える。
    """
    for lineno, line in enumerate(text.split("\n"), start=1):
        for match in SCRIPT_REF_RE.finditer(line):
            if AT_REVISION_RE.search(line, 0, match.start()):
                continue
            if match.group("root_skill"):
                skill, rel, base, verify_name = (
                    match.group("root_skill"),
                    match.group("root_path"),
                    REF_BASE_SKILLS_ROOT,
                    False,
                )
            elif match.group("up_skill"):
                skill, rel, base, verify_name = (
                    match.group("up_skill"),
                    match.group("up_path"),
                    REF_BASE_MD_PARENT,
                    False,
                )
            elif match.group("same_path"):
                skill, rel, base, verify_name = (
                    None,
                    match.group("same_path"),
                    REF_BASE_SKILL_DIR,
                    False,
                )
            else:
                skill, rel, base, verify_name = (
                    match.group("named_skill"),
                    match.group("named_path"),
                    REF_BASE_SKILLS_ROOT,
                    True,
                )
            rel = rel.rstrip(TRAILING_PUNCT)
            if not rel:
                continue
            yield lineno, skill, rel, base, verify_name


def count_revision_refs(text: str) -> int:
    """コミットの中のパスとして書いた `scripts/` 参照（検査しないもの）の件数。"""
    return sum(
        1
        for line in text.split("\n")
        for match in SCRIPT_REF_RE.finditer(line)
        if AT_REVISION_RE.search(line, 0, match.start())
    )


def resolve_ref_base(
    ref_base: str, skill: str | None, md_path: Path, skill_dir: Path
) -> Path | None:
    """`scripts/` 参照の起点を、実際のディレクトリに変える。

    起点の種類（`REF_BASE_*`）は iter_script_refs() が決める。ここでは種類ごとに
    どのディレクトリを基準にするかだけを持つ。

    - REF_BASE_SKILL_DIR:   そのファイルが属するスキルのディレクトリ（スキル名は要らない）
    - REF_BASE_MD_PARENT:   その md の親の親（`../<スキル名>/scripts/x` と書いた形）
    - REF_BASE_SKILLS_ROOT: skills ルート（`<スキル名>/scripts/x` と書いた形）

    スキル名を要る起点なのにスキル名が無いときは None を返す。iter_script_refs() は
    両者を必ず組で返すので実際には起きないが、起きたときに解決先を取り違えるより、
    その参照を検査せずに飛ばす。
    """
    if ref_base == REF_BASE_SKILL_DIR:
        return skill_dir
    if skill is None:
        return None
    if ref_base == REF_BASE_MD_PARENT:
        return md_path.parent.parent / skill
    return skill_dir.parent / skill


def check_script_refs(
    md_path: Path,
    skill_dir: Path,
    known_skills: AbstractSet[str],
    text: str,
    counts: Counts,
) -> list[Failure]:
    """参照された `scripts/` 配下のファイルが実在し、実行権限があることを確かめる。

    実行権限を求めるのは、インタプリタを前に置かずそのまま起動されるファイルである。
    拡張子 `.sh` に加えて、1 行目が `#!` で始まるファイル（`gh-review.py` など）も見る。
    拡張子を並べる代わりに shebang を見るのは、`.py` `.pl` と増えるたびに漏れるのを避け、
    「直接起動される」という事実に近い指標で判定するためである。

    解決の起点は書き方ごとに違う。理由は書き方ごとに読み手への意味が違うからである。
    - `../<スキル名>/scripts/x.sh`（REF_BASE_MD_PARENT）: `..` は「1 つ上」と読める
      相対パスの記法なので、その md が置かれたディレクトリ（`md_path.parent`）を起点にする。
    - `.claude/skills/<スキル名>/scripts/x.sh` と `<スキル名>/scripts/x.sh`
      （REF_BASE_SKILLS_ROOT）: スキルを名前で指しているので、skills ルート
      （`skill_dir.parent`）を起点にする。
    - `scripts/x.sh`、`./scripts/x.sh`（REF_BASE_SKILL_DIR）: 「このスキルの `scripts/`」と
      いう意味で書かれるので、md がスキルの何段目にあってもスキルのディレクトリを起点にする。
      md をサブディレクトリに置いても指す先が変わらないようにするための決めごとである。
    """
    failures = []
    counts.script_refs_at_revision += count_revision_refs(text)
    for lineno, skill, rel, ref_base, verify_name in iter_script_refs(text):
        if verify_name and skill not in known_skills:
            # スキル名が実在しない。`docs/scripts/x.sh` のようにスキルの参照ではない行と、
            # スキル名を打ち間違えた行が、字面が同じなのでここに混ざる。検査はできないが、
            # 捨てた件数は集計行に出す（「参照が無い」と「拾えなかった」を読み分けるため）。
            counts.script_refs_unknown_skill += 1
            continue
        counts.script_refs += 1
        base = resolve_ref_base(ref_base, skill, md_path, skill_dir)
        if base is None:
            continue
        target = base / "scripts" / rel
        result, error = stat_path(target)
        if error is not None:
            failures.append(
                Failure(
                    md_path,
                    lineno,
                    CHECK_READ,
                    f"参照先を確かめられない: {error}",
                )
            )
            continue
        if result is None:
            failures.append(
                Failure(
                    md_path,
                    lineno,
                    CHECK_SCRIPT_REF,
                    f"参照先が無い: {target}",
                )
            )
            continue
        if not stat.S_ISREG(result.st_mode):
            continue  # ディレクトリなどは実行権限を見ない
        needs_exec = target.suffix == ".sh"
        if not needs_exec:
            shebang, read_error = starts_with_shebang(target)
            if read_error is not None:
                failures.append(
                    Failure(
                        md_path,
                        lineno,
                        CHECK_READ,
                        f"参照先の 1 行目を読めない: {read_error}",
                    )
                )
                continue
            needs_exec = shebang
        if not needs_exec:
            continue
        mode = stat.S_IMODE(result.st_mode)
        # os.access(X_OK) は root だと常に真になるので、モードのビットを直接見る。
        if not mode & 0o111:
            failures.append(
                Failure(
                    md_path,
                    lineno,
                    CHECK_SCRIPT_REF,
                    f"実行権限が無い（mode {mode:03o}）: {target}",
                )
            )
    return failures


# --- 走査 ---


def iter_skill_dirs(skills_root: Path) -> list[Path]:
    """skills ルート直下のスキルディレクトリを返す。

    `.gitkeep` のようなファイルと、`.` で始まるディレクトリは対象外にする。
    """
    return sorted(p for p in skills_root.iterdir() if p.is_dir() and not p.name.startswith("."))


def check_skill(skill_dir: Path, known_skills: AbstractSet[str], counts: Counts) -> list[Failure]:
    """1 スキルを検査して、落ちた件を返す。"""
    failures = []
    skill_md = skill_dir / "SKILL.md"
    # `skill_md.is_file()` を直に呼ばないのは stat_path() と同じ理由である。スキルの
    # ディレクトリを `chmod 000` にすると EACCES（権限が無い）で PermissionError が外へ出て、
    # 後続のスキルが 1 つも検査されなくなる。
    skill_md_stat, stat_error = stat_path(skill_md)
    if stat_error is not None:
        failures.append(
            Failure(
                skill_md,
                1,
                CHECK_READ,
                f"SKILL.md を確かめられない: {stat_error}",
            )
        )
    elif skill_md_stat is None or not stat.S_ISREG(skill_md_stat.st_mode):
        # SKILL.md を消せば検査が静かになる、という抜け道を作らない。
        failures.append(Failure(skill_md, 1, CHECK_FRONTMATTER, "SKILL.md が無い"))

    for md_path in sorted(skill_dir.rglob("*.md")):
        text, read_failure = read_text(md_path)
        if read_failure is not None:
            failures.append(read_failure)
        if text is None:
            continue
        counts.files += 1
        if md_path == skill_md:
            failures.extend(check_frontmatter(md_path, text))
            failures.extend(check_line_limit(md_path, text))
        failures.extend(check_md_links(md_path, text, counts))
        failures.extend(check_script_refs(md_path, skill_dir, known_skills, text, counts))
    return failures


def check_skills_root(skills_root: Path) -> list[Failure]:
    """`skills_root` 直下の全スキルを検査して、落ちた件を（パス・行番号順に）返す。

    ディレクトリが無い・スキルが 1 つも無いときも 1 件の失敗にする。0 件を「問題なし」で通すと、
    スキルが全部消えても検査が緑のままになる。
    """
    if not skills_root.is_dir():
        return [Failure(skills_root, 1, CHECK_READ, "検査対象のディレクトリが無い")]
    skill_dirs = iter_skill_dirs(skills_root)
    if not skill_dirs:
        return [Failure(skills_root, 1, CHECK_READ, "検査対象のスキルが 1 つも無い")]
    known_skills = frozenset(p.name for p in skill_dirs)
    counts = Counts()
    failures: list[Failure] = []
    for skill_dir in skill_dirs:
        counts.skills += 1
        failures.extend(check_skill(skill_dir, known_skills, counts))
    return sorted(failures, key=Failure.sort_key)
