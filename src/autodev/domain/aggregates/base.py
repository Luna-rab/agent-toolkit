"""集約の土台。

集約は、コマンドを受けて出すイベントを決める `handle` と、イベントを状態に当てる `apply` の 2 組の
振り分けでできている。振り分けは、メソッドに `@handles` と `@applies` を付けて書く::

    class Design(Aggregate):
        NAME = "Design"            # events/registry.py の EVENTS_BY_AGGREGATE・commands/registry.py の COMMANDS_BY_AGGREGATE のキー

        def __init__(self, stream: StreamId) -> None:
            super().__init__(stream)
            self.proposal: Proposal | None = None

        @handles(ProposeDesign)
        def _propose(self, command: ProposeDesign) -> list[Event]:
            if self.proposal is not None:
                raise Rejected("確定していない提案がすでにある")
            return [DesignProposed(command.proposal)]

        @applies(DesignProposed)
        def _proposed(self, event: DesignProposed) -> None:
            self.proposal = event.proposal

    design = Design.replay(StreamId.design(), history)   # 起動時。apply だけを通る
    events = design.handle(command)                     # 不変条件を確かめ、出すイベントを決める
    # イベントストアに追記して確定させてから、
    for event in events:
        design.apply(event, command.command_id)

守ること:

- `handle` は状態を変えない。状態を変えるのは `apply` だけ（再生のときに判断を走らせ直さない）
- `handle` は外の世界に触らない。HEAD・セッション id は、コマンドの中身として受け取る（BeginStage）。
  時刻は扱わない（イベントの時刻は、イベントストアが追記するときに付ける）
- 状態を変えるコマンドは、必ずイベントを出す（イベントにしない変化は、再生すると消える）
- `apply` に書くのは、その集約が出すイベント（`events/registry.py` の `EVENTS_BY_AGGREGATE[NAME]`）だけ。状態を
  変えないイベントも、何もしない `@applies` を書く。書いていないイベントは `UnknownEvent` になる。
  受けるコマンドと当てるイベントが表と一致することは、`test/autodev/test_aggregate.py` が
  具象の集約ごとに確かめる
- 同じコマンド・同じイベントに 2 つのメソッドを付けると、クラスを作った時点で TypeError になる
- `__init__` は `stream` だけを受けて、イベントが 1 つも無いときの状態を作る（`replay` が呼ぶ）
- これから出すイベントの id は `next_event_id`、`apply` の中で当てているイベントの id は
  `event_id` で引く（エスカレーションの id にする）

`handle` は振り分けの前に、宛先・出してよい者（`Command.ISSUERS`）・名乗りが宛先と合うか・
処理済みのコマンドかを確かめる。振り分けの後に、返したイベントをすべて当てられるかを確かめる。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, ClassVar, TypeVar

from ..commands.base import Command
from ..events.base import Event
from ..value_objects.command_id import CommandId
from ..value_objects.event_id import EventId
from ..value_objects.issuer_kind import IssuerKind
from ..value_objects.stream_id import StreamId


class Rejected(Exception):
    """コマンドを拒む。理由は、出した者に返す（統括なら同じセッションに差し戻す）。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class UnknownEvent(Exception):
    """集約が当て方を知らないイベント。黙って読み飛ばすと、再生のたびにその変化が消える。"""


_HANDLES = "_autodev_handles"
_APPLIES = "_autodev_applies"
#: preview の中で当てるイベントの、仮のコマンドの id
_PREVIEW = CommandId("preview")

F = TypeVar("F", bound=Callable[..., Any])
A = TypeVar("A", bound="Aggregate")


def handles(command_type: type[Command]) -> Callable[[F], F]:
    """このメソッドが `command_type` のコマンドを受ける。イベントの一覧を返すか、`Rejected` を投げる。"""

    def mark(method: F) -> F:
        setattr(method, _HANDLES, command_type)
        return method

    return mark


def applies(event_type: type[Event]) -> Callable[[F], F]:
    """このメソッドが `event_type` のイベントを状態に当てる。"""

    def mark(method: F) -> F:
        setattr(method, _APPLIES, event_type)
        return method

    return mark


def _collect(cls: type, marker: str) -> dict[Any, str]:
    """印の付いたメソッドを、型 → メソッドの名前で集める。子のクラスが同じ名前で上書きしてよい。"""
    found: dict[Any, str] = {}
    for klass in reversed(cls.__mro__):
        for name, member in vars(klass).items():
            if (key := getattr(member, marker, None)) is None:
                continue
            if found.get(key, name) != name:
                raise TypeError(
                    f"{cls.__name__}: {key.__name__} に {found[key]} と {name} の 2 つが付いている"
                )
            found[key] = name
    return found


class Aggregate:
    #: 集約の名前。具象の集約は必ず書く
    NAME: ClassVar[str] = ""

    _handlers: ClassVar[dict[type[Command], str]] = {}
    _appliers: ClassVar[dict[type[Event], str]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls._handlers = _collect(cls, _HANDLES)
        cls._appliers = _collect(cls, _APPLIES)

    def __init__(self, stream: StreamId) -> None:
        self.stream = stream
        #: ストリームの中の版。当てたイベントの数
        self.version = 0
        #: イベントを出したコマンドの id。受け直しで同じコマンドが 2 回来ても、2 回目は何もしない
        self.processed: set[CommandId] = set()

    @classmethod
    def replay(cls: type[A], stream: StreamId, history: Iterable[tuple[Event, CommandId]]) -> A:
        """イベントの列（と、それを出したコマンドの id）から状態を作る。判断は走らせない。"""
        aggregate = cls(stream)
        for event, command_id in history:
            aggregate.apply(event, command_id)
        return aggregate

    @classmethod
    def handled_commands(cls) -> frozenset[type[Command]]:
        return frozenset(cls._handlers)

    @classmethod
    def applied_events(cls) -> frozenset[type[Event]]:
        return frozenset(cls._appliers)

    def handle(self, command: Command) -> list[Event]:
        """不変条件を確かめ、出すイベントを決める。状態は変えない。"""
        name = type(command).__name__
        if command.target != self.stream:
            raise Rejected(f"{name} の宛先は {command.target} で、{self.stream} ではない")
        if command.issuer.kind not in type(command).ISSUERS:
            raise Rejected(f"{name} を {command.issuer.kind.value} は出せない")
        _check_claim(command)
        if command.command_id in self.processed:
            return []
        handler = self._handlers.get(type(command))
        if handler is None:
            raise Rejected(f"{name} は {type(self).__name__} が受けるコマンドではない")
        events = list(getattr(self, handler)(command))
        # 当てられないイベントを追記すると、確定した後に apply で落ち、再生もできなくなる
        if unknown := sorted({type(e).__name__ for e in events if type(e) not in self._appliers}):
            raise UnknownEvent(
                f"{type(self).__name__} が {name} から当てられないイベントを返した: "
                + ", ".join(unknown)
            )
        return events

    def apply(self, event: Event, command_id: CommandId) -> None:
        """イベントを状態に当てる。再生のときもここだけを通る。

        ここで出た例外は致命的で、集約の状態は途中まで変わっているかもしれない。戻さずに、
        その集約を捨ててイベントストアから再生し直す。
        """
        applier = self._appliers.get(type(event))
        if applier is None:
            raise UnknownEvent(f"{type(self).__name__} は {type(event).__name__} を当てられない")
        self.version += 1
        getattr(self, applier)(event)
        self.processed.add(command_id)

    def preview(self: A, events: Iterable[Event]) -> A:
        """`events` を当てた後の状態を、自分を変えずに作る。

        `handle` が「このイベントを出したら、その後どうなるか」から続きのイベントを決めるときに使う
        （例: 最後の実装タスクが終端になるか）。状態の入れ物を 1 段だけ写すので、入れ物の中身は
        不変の値にしておく（入れ物の中を書き換える apply は、写した元まで変えてしまう）。
        """
        twin = object.__new__(type(self))
        for name, value in vars(self).items():
            copied = type(value)(value) if isinstance(value, (dict, list, set)) else value
            setattr(twin, name, copied)
        for event in events:
            twin.apply(event, _PREVIEW)
        return twin

    def next_event_id(self, offset: int = 0) -> EventId:
        """`handle` が返す一覧の `offset` 番目のイベントが、追記されたときに持つ id。"""
        return EventId.of(self.stream, self.version + 1 + offset)

    @property
    def event_id(self) -> EventId:
        """`apply` の中では、当てているイベントの id。外では、最後に当てたイベントの id。"""
        return EventId.of(self.stream, self.version)


def _check_claim(command: Command) -> None:
    """名乗りは宛先と合う。

    出してよい者の種類（ISSUERS）が合っていても、別のタスクの統括や別の実行の結果を名乗った
    コマンドは拒む。タスクの統括は `supervised_task` のタスクの統括だけが出せ（Stack のコマンドは
    git 管理タスクの統括だけ）、実行器は `reported_execution` の実行の結果だけを出せる。
    """
    issuer = command.issuer
    name = type(command).__name__
    if issuer.kind is IssuerKind.TASK_SUPERVISOR:
        task = command.supervised_task
        if task is None or issuer.task != task:
            raise Rejected(
                f"{name} を出せるのは {task} の統括だけ（{issuer.task} の統括が名乗った）"
            )
    if issuer.kind is IssuerKind.EXECUTOR:
        execution = command.reported_execution
        if execution is None or issuer.execution != execution:
            raise Rejected(
                f"{name} を出せるのは {execution} の実行器だけ（{issuer.execution} が名乗った）"
            )
