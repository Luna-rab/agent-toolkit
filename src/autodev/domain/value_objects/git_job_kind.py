from __future__ import annotations

from enum import Enum


class GitJobKind(Enum):
    """git 管理タスクの仕事の種類。種類ごとの並びは `stages/catalog.py` の `GIT_JOB_STAGES`。"""

    #: ランの開始: 概要ブランチと trees/overview を切る
    CUT_OVERVIEW = "cut-overview"
    #: 実装タスクを始めた: スタックの一番上から、タスクのブランチと trees/<TaskId> を切る
    CUT_TASK = "cut-task"
    #: 再計画を頼まれた: スタックの一番上で HEAD を固定した trees/stack-top を切り直す
    CUT_STACK_TOP = "cut-stack-top"
    #: 計画を初めて反映した: 概要 PR のまとめを書き、draft で作る
    OPEN_OVERVIEW = "open-overview"
    #: 再計画を反映した: 概要 PR のまとめを書き直す
    REWRITE_OVERVIEW = "rewrite-overview"
    #: フローを終えたタスクを積む（破棄の後の積み直しも）
    STACK = "stack"
    #: 積んだタスクを破棄した: 破棄した所から上を閉じ、スタックを作り直す
    DISCARD = "discard"
    #: ランが終わった: 仕上げのまとめを書き、概要 PR を draft から外す（外すと決めたときだけ）
    FINISH = "finish"

    @property
    def needs_overview(self) -> bool:
        """概要 PR を作った後でないと取り出さないか。スタックの一番上・概要 PR を相手にする仕事。"""
        return self not in (GitJobKind.CUT_OVERVIEW, GitJobKind.OPEN_OVERVIEW)

    @property
    def builds_on_top(self) -> bool:
        """取り出したときのスタックの一番上を base にするか。"""
        return self in (GitJobKind.CUT_TASK, GitJobKind.CUT_STACK_TOP, GitJobKind.STACK)

    @property
    def must_finish(self) -> bool:
        """済ませないとランが終わらない仕事か。フローを捨てても消さずに、列の先頭へ戻す。

        概要 PR の書き直しだけは、次の書き直しか仕上げで本文が作り直されるので、捨てたら消してよい。
        ほかは、切らないとタスクも再計画も始まらず、積まない・閉じないと Run が終端にならない。
        """
        return self is not GitJobKind.REWRITE_OVERVIEW

    @property
    def can_drop(self) -> bool:
        """止めた仕事を、ラン統括の判断でやめてよいか。

        タスクを切る・積む・stack-top を切り直す仕事は、やめても相手のタスクを止めるか再計画を
        やり直せば、ランの終わりを待たせるものが残らない。概要ブランチ・概要 PR を作る仕事は、やめると
        概要 PR を待つ仕事が二度と取り出されない。破棄はやめると閉じ終えるのを待つ数が減らず、仕上げは
        やめると git 管理タスクが終わらない。どちらもランが終わらなくなる。
        """
        return self in (
            GitJobKind.CUT_TASK,
            GitJobKind.CUT_STACK_TOP,
            GitJobKind.REWRITE_OVERVIEW,
            GitJobKind.STACK,
        )
