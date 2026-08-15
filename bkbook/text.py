# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 satorux
# Copyright (c) 1997-2006 Motoyuki Kasahara

"""項目本文の読み出しと整形。

項目の本文は、JIS X 0208 の 2 バイト文字とエスケープシーケンスが混ざった
バイト列である。エスケープはすべて 0x1F で始まり、次のバイトがその意味と
長さを示す。ほとんどは印付け——強調開始、改行、参照の始まり——で、
一部は字下げの深さや参照先の位置といったデータを伴う。

解釈器はこの流れをたどりながら **レンダラ** のメソッドを呼ぶ。
:class:`PlainTextRenderer` はその呼び出しを文字列にする。それ以外のものを
作りたければ :class:`Renderer` を継承する。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .jacode import CHARCODE_ISO8859_1
from .search import Position
from .zio import PAGE_SIZE, EbError

#: 終わりの来ない項目を諦めるまでに読む量。
DEFAULT_MAX_LENGTH = 64 * 1024

_READ_CHUNK = PAGE_SIZE
_REFILL_THRESHOLD = 64

ESCAPE = 0x1F

STOP_NONE = "none"
STOP_SOFT = "soft"
STOP_HARD = "hard"

#: 読み飛ばすだけでよいエスケープと、その全長。ここになく、個別に
#: 処理もされないものは 2 バイト。
_FIXED_LENGTH = {
    0x02: 2, 0x04: 2, 0x05: 2, 0x06: 2, 0x07: 2,
    0x09: 4, 0x0A: 2, 0x0B: 2, 0x0C: 2, 0x0E: 2, 0x0F: 2,
    0x10: 2, 0x11: 2, 0x12: 2, 0x13: 2, 0x14: 4,
    0x32: 2, 0x39: 46, 0x3C: 20,
    0x41: 4, 0x42: 4, 0x43: 2, 0x44: 12,
    0x4C: 4, 0x4D: 20, 0x4F: 34,
    0x52: 8, 0x53: 10, 0x59: 2, 0x5C: 2,
    0x61: 2, 0x62: 8, 0x63: 8, 0x64: 8,
    0x6A: 2, 0x6B: 2, 0x6C: 2, 0x6D: 2, 0x6F: 2,
    0xE1: 2,
}

#: 引数のバイトが、対応する「ここまで読み飛ばす」符号を選ぶエスケープ。
_SKIP_PLUS_20 = frozenset(
    {0x35, 0x36, 0x37, 0x38, 0x3A, 0x3B, 0x3D, 0x3E, 0x3F, 0x49, 0x4E}
    | set(range(0x70, 0x90))
)
_SKIP_PLUS_01 = frozenset(range(0xE4, 0x100, 2))

#: EPWING では 4 バイトの引数を伴うが、EB では伴わないことのあるエスケープ。
_OPTIONAL_ARGUMENT = frozenset({0x1A, 0x1B, 0x1C, 0x1D, 0x1E, 0x1F, 0xE0})

#: 長さが後続のバイトで決まるエスケープ。
_VARIABLE_LENGTH = frozenset({0x42, 0x4B})


class TextError(EbError):
    """本文の流れが壊れているか、途中で終わっている。"""


def bcd2(data: bytes, offset: int) -> int:
    """2 バイトのパック 10 進数を読む。"""
    return (
        ((data[offset] >> 4) & 0x0F) * 1000
        + (data[offset] & 0x0F) * 100
        + ((data[offset + 1] >> 4) & 0x0F) * 10
        + (data[offset + 1] & 0x0F)
    )


def bcd4(data: bytes, offset: int) -> int:
    """4 バイトのパック 10 進数を読む。"""
    value = 0
    for i in range(4):
        byte = data[offset + i]
        value = value * 100 + ((byte >> 4) & 0x0F) * 10 + (byte & 0x0F)
    return value


# -- レンダラ -------------------------------------------------------------


class Renderer:
    """本文イベントを捨てる受け皿。継承して必要なものだけ上書きする。"""

    def character(self, text: str) -> None: ...
    def gaiji(self, code: int, narrow: bool) -> None: ...
    def newline(self) -> None: ...
    def indent(self, level: int) -> None: ...

    def begin_narrow(self) -> None: ...
    def end_narrow(self) -> None: ...
    def begin_subscript(self) -> None: ...
    def end_subscript(self) -> None: ...
    def begin_superscript(self) -> None: ...
    def end_superscript(self) -> None: ...
    def begin_emphasis(self) -> None: ...
    def end_emphasis(self) -> None: ...
    def begin_no_newline(self) -> None: ...
    def end_no_newline(self) -> None: ...
    def begin_decoration(self, kind: int) -> None: ...
    def end_decoration(self) -> None: ...

    def begin_keyword(self, code: int) -> None: ...
    def end_keyword(self) -> None: ...
    def begin_reference(self) -> None: ...
    def end_reference(self, position: Position) -> None: ...
    def begin_candidate(self) -> None: ...
    def end_candidate(self, position: Position | None) -> None: ...


@dataclass
class Reference:
    """参照。それを担っているテキストと、指している先。"""

    start: int
    end: int
    position: Position

    def text_of(self, text: str) -> str:
        return text[self.start : self.end]


@dataclass
class Candidate:
    """メニューの 1 行、または multi 検索のある欄に入れられる値 1 つ。

    position が ``None`` の候補は、行き先ではなく、後続の候補にかかる見出しで
    ある。その行がグループの label であるとき、本はページ番号に 0 を書く。
    """

    start: int
    end: int
    position: Position | None

    def text_of(self, text: str) -> str:
        return text[self.start : self.end]


class PlainTextRenderer(Renderer):
    """イベントを平文の文字列に集める。

    本が自前のビットマップフォントで描く文字（**外字**）には、対応する
    Unicode の符号位置がない。``gaiji`` に ``(narrow, code)`` から置き換え
    文字列への対応表を渡す。対応のないものは eblook 式の ``<gaiji=z1234>``
    と書き出すので、少なくとも目に見えるし grep もできる。
    """

    #: 数字には本物の下付き文字がある。それ以外は括弧でくくる。
    SUBSCRIPT_DIGITS = str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉")

    def __init__(
        self,
        gaiji: dict[tuple[bool, int], str] | None = None,
        subscript: bool = True,
    ):
        self._parts: list[str] = []
        self._length = 0
        self.gaiji_map = gaiji or {}
        self.references: list[Reference] = []
        self.candidates: list[Candidate] = []
        self.unknown_gaiji: list[tuple[bool, int]] = []
        self._reference_start: int | None = None
        self._candidate_start: int | None = None
        self._subscript = subscript
        self._subscript_starts: list[int] = []

    def _emit(self, text: str) -> None:
        if text:
            self._parts.append(text)
            self._length += len(text)

    @property
    def text(self) -> str:
        return "".join(self._parts)

    def character(self, text: str) -> None:
        self._emit(text)

    def gaiji(self, code: int, narrow: bool) -> None:
        replacement = self.gaiji_map.get((narrow, code))
        if replacement is None:
            self.unknown_gaiji.append((narrow, code))
            replacement = f"<gaiji={'h' if narrow else 'z'}{code:04x}>"
        self._emit(replacement)

    def newline(self) -> None:
        self._emit("\n")

    def begin_subscript(self) -> None:
        if self._subscript:
            self._subscript_starts.append(len(self._parts))

    def end_subscript(self) -> None:
        """下付きのテキストを区切る。これらの本はここに振り仮名を入れている。

        広辞苑 は読みを下付きのエスケープで示す——粟 のあとに あわ——ので、
        そのままつなげると語とその読みの区別が消えてしまう。同書の第四版は
        読みを括弧に入れて書いており、ここでもそれに倣う。
        """
        if not self._subscript_starts:
            return
        start = self._subscript_starts.pop()
        inner = "".join(self._parts[start:])
        if not inner:
            return
        del self._parts[start:]
        self._length -= len(inner)

        if inner.isdigit():
            self._emit(inner.translate(self.SUBSCRIPT_DIGITS))
        else:
            self._emit(f"({inner})")

    def begin_reference(self) -> None:
        self._reference_start = self._length

    def end_reference(self, position: Position) -> None:
        if self._reference_start is not None:
            self.references.append(
                Reference(self._reference_start, self._length, position)
            )
            self._reference_start = None

    def begin_candidate(self) -> None:
        self._candidate_start = self._length

    def end_candidate(self, position: Position | None) -> None:
        if self._candidate_start is not None:
            self.candidates.append(
                Candidate(self._candidate_start, self._length, position)
            )
            self._candidate_start = None


# -- 解釈器 ---------------------------------------------------------------


@dataclass
class TextResult:
    """:func:`read_text` の呼び出し 1 回の結果。"""

    text: str
    stop: str
    next_position: Position
    references: list[Reference] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    unknown_gaiji: list[tuple[bool, int]] = field(default_factory=list)


class _Stream:
    """サブブックの本文ファイルを 1 バイトずつ読む道具。"""

    def __init__(self, subbook, location: int):
        self._zio = subbook.zio
        self.location = location
        self._buffer = b""
        self._offset = 0
        self._at_end = False

    def peek(self, count: int) -> bytes:
        if len(self._buffer) - self._offset < count:
            self._refill()
        return self._buffer[self._offset : self._offset + count]

    def _refill(self) -> None:
        if self._at_end:
            return
        rest = self._buffer[self._offset :]
        chunk = self._zio.read(self.location + len(rest), _READ_CHUNK)
        if len(chunk) < _READ_CHUNK:
            self._at_end = True
        self._buffer = rest + chunk
        self._offset = 0

    def advance(self, count: int) -> None:
        self._offset += count
        self.location += count


def _position_to_location(position: Position) -> int:
    return (position.page - 1) * PAGE_SIZE + position.offset


def _location_to_position(location: int) -> Position:
    return Position(location // PAGE_SIZE + 1, location % PAGE_SIZE)


def read_text(
    subbook,
    position: Position,
    *,
    heading: bool = False,
    renderer: Renderer | None = None,
    max_length: int = DEFAULT_MAX_LENGTH,
    stop_code: tuple[int, int] | None = None,
) -> TextResult:
    """``position`` から項目（または見出し）を 1 つ読む。

    読み出しは、本文終了のエスケープ、その項目の stop code、または——
    見出しの場合は——最初の改行で止まる。

    ``stop_code`` はこの本で項目を終わらせるエスケープシーケンス。省略すると
    本に自前のものを尋ね、本は初回に索引からそれを割り出す
    （:mod:`bkbook.stopcode` を参照）。見出しはどちらにせよ改行で終わるので、
    見出し語を引くだけならこの費用はかからない。
    """
    subbook.load()
    if stop_code is None and not heading:
        stop_code = subbook.stop_code
    sink = renderer if renderer is not None else PlainTextRenderer()
    interpreter = _Interpreter(subbook, sink, heading=heading, stop_code=stop_code)
    stop = interpreter.run(_position_to_location(position), max_length)

    result = TextResult(
        text=sink.text if isinstance(sink, PlainTextRenderer) else "",
        stop=stop,
        next_position=_location_to_position(interpreter.location),
    )
    if isinstance(sink, PlainTextRenderer):
        result.references = sink.references
        result.candidates = sink.candidates
        result.unknown_gaiji = sink.unknown_gaiji
    return result


def read_heading(subbook, position: Position, **kwargs) -> TextResult:
    """``position`` の見出し語だけを読む。"""
    return read_text(subbook, position, heading=True, **kwargs)


class _Interpreter:
    def __init__(
        self,
        subbook,
        renderer: Renderer,
        heading: bool,
        stop_code: tuple[int, int] | None = None,
    ):
        self.subbook = subbook
        self.renderer = renderer
        self.heading = heading
        self.stop_code = stop_code
        self.location = 0

        self.narrow = False
        self.skip_code: int | None = None
        self.ebxac_gaiji = False
        self.printable_count = 0
        self.auto_stop_code: int | None = None
        self._is_epwing = subbook.book.disc_code != "eb"
        self._latin = subbook.book.character_code == CHARCODE_ISO8859_1

    def run(self, location: int, max_length: int) -> str:
        stream = _Stream(self.subbook, location)
        self.location = location
        consumed = 0

        while consumed < max_length:
            head = stream.peek(2)
            if not head:
                self.location = stream.location
                return STOP_HARD

            if head[0] == ESCAPE:
                if len(head) < 2:
                    raise TextError(f"{stream.location}: truncated escape")
                step, stop = self._escape(stream, head[1])
                if stop == STOP_HARD:
                    self.location = stream.location
                    return STOP_HARD
                stream.advance(step)
                consumed += step
                self.location = stream.location
                if stop == STOP_SOFT:
                    return STOP_SOFT
                continue

            step = self._character(stream, head)
            stream.advance(step)
            consumed += step
            self.location = stream.location

        return STOP_NONE

    # -- 文字 --------------------------------------------------------------

    def _character(self, stream: _Stream, head: bytes) -> int:
        self.printable_count += 1

        if self._latin:
            c1 = head[0]
            if 0x20 <= c1 < 0x7F or 0xA0 <= c1 <= 0xFF:
                if self.skip_code is None:
                    self.renderer.character(bytes((c1,)).decode("iso8859-1"))
                return 1
            if len(head) < 2:
                raise TextError(f"{stream.location}: truncated local character")
            if self.skip_code is None:
                self.renderer.gaiji(int.from_bytes(head[:2], "big"), True)
            return 2

        if len(head) < 2:
            raise TextError(f"{stream.location}: truncated character")
        c1, c2 = head[0], head[1]

        if c1 < 0x20:
            # 文字の先頭にはなりえないバイト。ロングマン は banger、beam、
            # get の途中に 0x00 が紛れており、これに 2 バイト使うと以降の
            # 2 バイト対が 1 つずつずれる。すると項目は自分の終わりを
            # 読み抜けて次の項目まで文字化けしたまま進み、0x1f がたまたま
            # 偶数バイト目に来るまで止まらない。1 バイトだけ進めれば
            # 位相が戻る。
            return 1

        if self.skip_code is not None:
            return 2

        if 0x20 < c1 < 0x7F and 0x20 < c2 < 0x7F:
            # JIS X 0208 の文字。半角フラグが変えるのは描き方だけで、
            # 文字そのものではないので、テキストとしては同じ。
            self.renderer.character(
                bytes((c1 | 0x80, c2 | 0x80)).decode("euc_jp", errors="replace")
            )
        elif 0x20 < c1 < 0x7F and 0xA0 < c2 < 0xFF:
            # GB 2312。最上位ビットを戻せば EUC-CN。
            self.renderer.character(
                bytes((c1 | 0x80, c2)).decode("gb2312", errors="replace")
            )
        elif 0xA0 < c1 < 0xFF and 0x20 < c2 < 0x7F:
            self.renderer.gaiji(int.from_bytes(head[:2], "big"), self.narrow)
        return 2

    # -- エスケープシーケンス ----------------------------------------------

    def _escape(self, stream: _Stream, code: int) -> tuple[int, str]:
        """エスケープを 1 つ処理し、(消費バイト数, 停止状態) を返す。"""
        if code == 0x03:
            return 0, STOP_HARD

        step = _FIXED_LENGTH.get(code, 2)
        if code in _OPTIONAL_ARGUMENT:
            step = self._optional_argument_length(stream, code)
        elif code in _VARIABLE_LENGTH:
            step = self._variable_length(stream, code)

        data = stream.peek(step)
        if len(data) < step:
            raise TextError(f"{stream.location}: truncated escape 0x1f{code:02x}")

        if self.skip_code is not None:
            # 読み飛ばし中は、対応する終了符号だけが意味を持つ。
            if code == self.skip_code:
                self.skip_code = None
            return step, STOP_NONE

        if code in _SKIP_PLUS_20:
            self.skip_code = code + 0x20
            return 2, STOP_NONE
        if code in _SKIP_PLUS_01:
            self.skip_code = code + 0x01
            return 2, STOP_NONE

        return self._dispatch(code, data, step)

    def _variable_length(self, stream: _Stream, code: int) -> int:
        """後続のバイトで見分ける 2 つのエスケープの長さ。"""
        if code == 0x42:
            # 参照は 2 バイトの引数を伴うか、次のバイトが 0 でなければ
            # まったく伴わないかのどちらか。後者ではそのバイトはもう本文。
            data = stream.peek(4)
            if len(data) < 4:
                raise TextError(f"{stream.location}: truncated reference")
            return 2 if data[2] != 0x00 else 4

        # 0x4b: 直後に 0x1f6b で閉じられるページ参照は、全体で 1 つの単位。
        data = stream.peek(10)
        if len(data) < 10:
            raise TextError(f"{stream.location}: truncated paged reference")
        return 10 if data[8:10] == b"\x1f\x6b" else 8

    def _optional_argument_length(self, stream: _Stream, code: int) -> int:
        """EPWING 以前の本には、これらのエスケープの引数を省くものがある。"""
        if code in (0x1C, 0x1D) and self.subbook.book.character_code == 3:
            return 2
        data = stream.peek(4)
        if len(data) < 4:
            raise TextError(f"{stream.location}: truncated escape 0x1f{code:02x}")
        if not self._is_epwing and data[2] >= 0x1F:
            return 2
        return 4

    def _dispatch(self, code: int, data: bytes, step: int) -> tuple[int, str]:
        renderer = self.renderer

        if code == 0x04:
            self.narrow = True
            renderer.begin_narrow()
        elif code == 0x05:
            self.narrow = False
            renderer.end_narrow()
        elif code == 0x06:
            renderer.begin_subscript()
        elif code == 0x07:
            renderer.end_subscript()
        elif code == 0x09:
            argument = int.from_bytes(data[2:4], "big")
            if self._is_stop_code(0x1F09, argument):
                return step, STOP_SOFT
            renderer.indent(argument)
        elif code == 0x0A:
            if self.heading:
                return step, STOP_SOFT
            renderer.newline()
        elif code == 0x0E:
            renderer.begin_superscript()
        elif code == 0x0F:
            renderer.end_superscript()
        elif code == 0x10:
            renderer.begin_no_newline()
        elif code == 0x11:
            renderer.end_no_newline()
        elif code == 0x12:
            renderer.begin_emphasis()
        elif code == 0x13:
            renderer.end_emphasis()
        elif code == 0x14:
            self.skip_code = 0x15
        elif code == 0x1C and self.subbook.book.character_code == 3:
            self.ebxac_gaiji = True
        elif code == 0x1D and self.subbook.book.character_code == 3:
            self.ebxac_gaiji = False
        elif code == 0x41:
            argument = int.from_bytes(data[2:4], "big")
            if self._is_stop_code(0x1F41, argument):
                return step, STOP_SOFT
            if self.auto_stop_code is None:
                self.auto_stop_code = argument
            renderer.begin_keyword(argument)
        elif code == 0x42:
            renderer.begin_reference()
        elif code == 0x4B:
            if step == 10:
                return step, STOP_SOFT
        elif code == 0x43:
            renderer.begin_candidate()
        elif code == 0x61:
            renderer.end_keyword()
        elif code == 0x62:
            renderer.end_reference(Position(bcd4(data, 2), bcd2(data, 6)))
        elif code == 0x63:
            page, offset = bcd4(data, 2), bcd2(data, 6)
            renderer.end_candidate(
                None if page == 0 and offset == 0 else Position(page, offset)
            )
        elif code == 0x6C:
            return step, STOP_SOFT
        elif code == 0xE0:
            if step == 4:
                renderer.begin_decoration(int.from_bytes(data[2:4], "big"))
        elif code == 0xE1:
            renderer.end_decoration()

        return step, STOP_NONE

    def _is_stop_code(self, code0: int, code1: int) -> bool:
        """このエスケープは項目を終わらせるか。

        appendix から得た stop code があれば答えは正確。なければ libeb の
        推測に落ちる——項目は、それを開いた keyword エスケープが再び現れた
        ところで終わる。
        """
        if self.heading or self.printable_count == 0:
            return False
        if self.stop_code is not None:
            return (code0, code1) == self.stop_code
        return code0 == 0x1F41 and code1 == self.auto_stop_code
