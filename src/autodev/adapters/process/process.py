"""ProcessRunner: 検証コマンドを流して、終了コードと出力の末尾を返す。"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from dataclasses import dataclass

from ...domain.value_objects.verify_command import VerifyCommand
from ...domain.value_objects.verify_result import VerifyResult
from .command import NOT_FOUND, TIMED_OUT, check_stopped, merged_env, tracked

#: 出力の末尾として残す文字数。テストの失敗の要約は末尾に出る
DEFAULT_TAIL_CHARS = 4000


@dataclass(frozen=True)
class ProcessRunner:
    """検証コマンドだけは `bash -lc` で流す。パイプやリダイレクトを含みうるからで、
    計画ステージが返したコマンドも driver の権限で流れる（受け入れている）。"""

    timeout: float | None = 1800
    tail_chars: int = DEFAULT_TAIL_CHARS
    shell: str = "bash"

    def run(self, command: VerifyCommand, cwd: str | os.PathLike[str]) -> VerifyResult:
        """`stoppable` の中なら、止めたとき Stopped を投げる（止めた検証の結果を返さない）。"""
        check_stopped()
        try:
            child = subprocess.Popen(
                [self.shell, "-lc", str(command)],
                cwd=cwd,
                env=merged_env(None),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                # 標準エラーを標準出力に混ぜ、出た順のまま末尾を取る
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                # 時間切れのとき、bash の子（pytest など）までまとめて止める。bash だけを止めると、
                # 孫が出力の管を握ったまま残り、読み終わらない
                start_new_session=True,
            )
        except FileNotFoundError:
            return VerifyResult(command, NOT_FOUND, f"{self.shell} が見つからない")
        with tracked(child):
            try:
                out, _ = child.communicate(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                out, _ = child.communicate()
                check_stopped()
                note = f"\n制限時間を超えた（{self.timeout} 秒）"
                return VerifyResult(command, TIMED_OUT, self._tail((out or "") + note))
        check_stopped()
        return VerifyResult(command, child.returncode, self._tail(out or ""))

    def _tail(self, text: str) -> str:
        return text[-self.tail_chars :] if self.tail_chars > 0 else ""
