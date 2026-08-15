# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 satorux

"""項目がどこで終わるのかを、教えてもらわずに突き止める。

辞書の本文ファイルは 1 本の長い流れである。項目と項目のあいだにあるのは
エスケープだけ。それがどのエスケープかは出版社ごとの判断で、ディスクの
どこにも書かれていない。libeb は本ごとの答えを手書きの appendix ファイルから
知り、appendix がなければ推測していた。

しかしディスク自身は知っていて、遠回しにそれを教えてくれる。索引はすべての
項目の **開始** 位置を持っている。ということは、ある項目の 1 文字目の直前に
あるバイトは、その 1 つ前の項目を終わらせたバイトである。数百項目を抜き出して
先頭の並びを見ると、どの項目にも共通するエスケープが 2 つか 3 つ見つかる。
それが候補だ。あとは候補を順に使って項目を読み、毎回きちんと次の項目の
境界に着地するのはどれかを見ればいい。途中で止まらないものが stop code である。

たいてい候補は 2 つ残る。項目はエスケープ 2 つ——字下げと見出し語——で
始まるからだ。どちらを使っても本文は正しく区切れる。優先するのは前のほうで、
libeb の appendix が記録しているのもその選択である。厄介なのは、その開始用の
エスケープを記事の **途中** でも使う本で、しかもその意味が本ごとに違う。
日本大百科全書 は図版のキャプションを記事の書き出しと同じ字下げで組むので、
字下げを使うと 25 記事に 1 つが早く終わってしまう。一方 三省堂「大辞林」 が
字下げているのは 子見出し で、これは本当にそれ自体が項目であり、本当に
その前の項目を終わらせている。読んでみれば区別がつく。構造からは区別できない。
"""

from __future__ import annotations

from dataclasses import dataclass

from .search import NoSuchSearchError, Position, iter_index
from .text import DEFAULT_MAX_LENGTH, STOP_SOFT, Renderer, read_text
from .zio import PAGE_SIZE, EbError

#: 項目を終わらせうるエスケープ。字下げと、見出し語を開く keyword。
STOP_ESCAPES = (0x1F09, 0x1F41)

#: 項目の 1 バイト目から、開始用エスケープをどこまで探すか。項目は字下げと
#: keyword、あわせて 8 バイトで始まる。手前まで見るのは、索引が開始の字下げの
#: **先** を指している本のため。
_PROLOGUE_OFFSETS = (-4, 0, 4)

#: 抜き出した項目のほぼ全部を開いているエスケープでなければ、候補にしない。
_MIN_SHARE = 0.5

#: 2 つの候補の点数がどれだけ近ければ引き分けとみなすか。
_TIE = 0.03

#: 索引から何項目抜き出すか、そのうち何個から読みの連鎖を始めるか、
#: 連鎖を何段たどるか。
DEFAULT_SAMPLE = 1200
DEFAULT_TRIALS = 60
DEFAULT_DEPTH = 25


@dataclass(frozen=True)
class Candidate:
    """項目を終わらせているかもしれないエスケープと、その前置きの中での位置。"""

    code: int
    argument: int
    offset: int
    share: float = 0.0
    score: float = 0.0

    @property
    def stop_code(self) -> tuple[int, int]:
        return (self.code, self.argument)

    def __repr__(self) -> str:
        return (
            f"<Candidate 0x{self.code:04x} 0x{self.argument:04x} "
            f"offset={self.offset:+d} share={self.share:.2f} score={self.score:.2f}>"
        )


def _location(position: Position) -> int:
    return (position.page - 1) * PAGE_SIZE + position.offset


def _position(location: int) -> Position:
    return Position(location // PAGE_SIZE + 1, location % PAGE_SIZE)


def _sample_starts(subbook, limit: int) -> list[int]:
    """項目の開始位置を、その本が持つ見出し語索引すべてから集める。

    索引 1 つで足りることが多いが、いつもではない。日本大百科全書 は
    ``word_asis`` には欧文見出し 699 語しか入れておらず、残りの十数万語は
    ``word_kana`` にある。全部読めば、標本が本文の一角に固まらず全体に散る。
    """
    names = [
        name
        for name in ("word_asis", "word_kana", "word_alphabet")
        if name in subbook.searches and subbook.searches[name].start_page
    ]
    starts: set[int] = set()
    for name in names:
        per_index = max(1, limit // len(names))
        try:
            entries = iter_index(subbook, name)
        except NoSuchSearchError:
            continue
        for count, hit in enumerate(entries):
            if count >= per_index:
                break
            starts.add(_location(hit.text))
    return sorted(starts)


def _escapes_at(subbook, start: int) -> dict[int, tuple[int, int]]:
    """項目の 1 バイト目の前後にある字下げ／keyword のエスケープを読む。"""
    base = start + _PROLOGUE_OFFSETS[0]
    if base < 0:
        return {}
    data = subbook.zio.read(base, 4 + _PROLOGUE_OFFSETS[-1] - _PROLOGUE_OFFSETS[0])
    found = {}
    for offset in _PROLOGUE_OFFSETS:
        at = offset - _PROLOGUE_OFFSETS[0]
        if len(data) < at + 4:
            continue
        code = int.from_bytes(data[at : at + 2], "big")
        if code in STOP_ESCAPES:
            found[offset] = (code, int.from_bytes(data[at + 2 : at + 4], "big"))
    return found


def _candidates(subbook, starts: list[int]) -> list[Candidate]:
    """抜き出した項目の（ほぼ）すべてを開いているエスケープを探す。"""
    counts: dict[tuple[int, int, int], int] = {}
    for start in starts:
        for offset, (code, argument) in _escapes_at(subbook, start).items():
            key = (code, argument, offset)
            counts[key] = counts.get(key, 0) + 1

    total = len(starts) or 1
    found = [
        Candidate(code, argument, offset, share=count / total)
        for (code, argument, offset), count in counts.items()
        if count / total >= _MIN_SHARE
    ]
    # 前にあるものから順に。どちらでも正しく区切れる 2 つのエスケープなら、
    # 境界に近いほうが appendix の挙げているものである。
    found.sort(key=lambda c: (c.offset, -c.share))
    return found


def _signatures(subbook, starts: list[int], candidates: list[Candidate]) -> set[bytes]:
    """項目の始まりを示すバイト列。

    本によっては項目の開き方が 1 通りではない。ジーニアス英和大辞典 は数千項目を
    字下げ 1 で、百項目ほどを字下げ 2 で開く。だから、よくある形だけでなく、
    まれでも繰り返し現れる形もパターンとして残す。ただし 4 バイトごとに必ず
    エスケープであることは要求する。これがあるおかげで、たまたま標本に共通して
    いた見出し語が境界の目印と取り違えられずに済む。
    """
    offsets = sorted({c.offset for c in candidates if c.offset >= 0})
    if not offsets:
        return set()
    width = offsets[-1] + 4

    shapes: dict[bytes, int] = {}
    for start in starts:
        data = subbook.zio.read(start, width)
        if len(data) == width and all(data[at] == 0x1F for at in range(0, width, 4)):
            shapes[data] = shapes.get(data, 0) + 1

    floor = max(2, len(starts) // 200)
    return {shape for shape, count in shapes.items() if count >= floor}


class _Boundaries:
    """ある位置が項目の始まりかどうかを判定するのに要るもの一式。"""

    def __init__(self, subbook, starts: list[int], candidates: list[Candidate]):
        self.subbook = subbook
        self.known = set(starts)
        self.signatures = _signatures(subbook, starts, candidates)
        self.width = len(next(iter(self.signatures))) if self.signatures else 0

    def __contains__(self, location: int) -> bool:
        if location in self.known:
            return True
        if not self.width or location < 0:
            return False
        return self.subbook.zio.read(location, self.width) in self.signatures


def _score(
    subbook,
    candidate: Candidate,
    seeds: list[int],
    boundaries: _Boundaries,
    depth: int,
) -> float:
    """この stop code で項目から次の項目へどれだけ確実に渡っていけるか。

    間違ったエスケープで読んでも、たいていは動いてしまう。1 冊の項目はどれも
    同じ形で始まるので、その書き出しに含まれるエスケープならどれでも大半を
    正しく区切るからだ。正解と「もっともらしい候補」を分けるのは、本文の
    **途中** にもそれを使っている少数の項目である——日本大百科全書 で
    項目を終わらせていないと判明したエスケープの場合、25 項目に 1 つ——
    そしてそれを見つけるには膨大な数の項目を読まねばならない。

    そこで、標本を大量に取る代わりに、読みを次々に連鎖させる。ある項目が
    終わったところが次の項目の始まりなので、索引から取った起点 1 つで
    確認すべき境界がいくつも得られる。項目の途中に踏み込んでしまった連鎖は
    そこから先は信用できないので、そこで打ち切る。
    """
    good = bad = 0
    read: set[int] = set()
    for seed in seeds:
        location = seed
        for _step in range(depth):
            if location in read:
                break  # 先行する連鎖に追いついた
            read.add(location)
            try:
                result = read_text(
                    subbook,
                    _position(location),
                    renderer=Renderer(),
                    stop_code=candidate.stop_code,
                    max_length=DEFAULT_MAX_LENGTH,
                )
            except EbError:
                break
            if result.stop != STOP_SOFT:
                break
            # 解釈器は認識したエスケープの直後で止まる。そのエスケープは、
            # 区切っている項目から既知の距離のところにある。
            location = _location(result.next_position) - 4 - candidate.offset
            if location not in boundaries:
                bad += 1
                break
            good += 1
    return good / (good + bad) if good or bad else 0.0


def _weigh(
    subbook, sample: int, trials: int, depth: int, all_of_them: bool = True
) -> list[Candidate]:
    """候補に点をつける。前置きの中で前にあるものから順に。

    ``all_of_them`` は報告用。判断するだけならここまで要らない。候補は
    すでに引き分けを解く順に並んでいるので、どれかが全項目を正しく読めた
    時点で、後ろの候補が勝てる余地はない。
    """
    subbook.load()
    starts = _sample_starts(subbook, sample)
    if len(starts) < 8:
        return []
    candidates = _candidates(subbook, starts)
    if not candidates:
        return []

    boundaries = _Boundaries(subbook, starts, candidates)
    stride = max(1, len(starts) // trials)
    seeds = starts[::stride][:trials]

    scored = []
    for candidate in candidates:
        score = _score(subbook, candidate, seeds, boundaries, depth)
        scored.append(
            Candidate(
                candidate.code, candidate.argument, candidate.offset,
                candidate.share, score,
            )
        )
        if score >= 1.0 and not all_of_them:
            break
    return scored


def infer(
    subbook,
    *,
    sample: int = DEFAULT_SAMPLE,
    trials: int = DEFAULT_TRIALS,
    depth: int = DEFAULT_DEPTH,
) -> tuple[int, int] | None:
    """このサブブックで項目を終わらせているエスケープシーケンスを求める。

    appendix ファイルと同じ形の ``(escape, argument)`` の組——たいていの辞書では
    ``(0x1f09, 0x0001)``——を返す。索引から得られる手がかりが少なすぎるときは
    ``None``。
    """
    scored = _weigh(subbook, sample, trials, depth, all_of_them=False)
    if not scored:
        return None
    top = max(c.score for c in scored)
    if top < _MIN_SHARE:
        return None
    # 項目は字下げと keyword で始まり、どちらでも本文は正しく区切れるので、
    # 2 つはたいてい同じ点数になる。appendix が挙げているのは前のほう。
    # 点数の比較に少し遊びを持たせてあるのは、50 万項目もある本には壊れた項目が
    # いくつかあり、読めない項目 1 つでどのエスケープを使うかが決まってしまっては
    # まずいからである。
    return min(
        (c for c in scored if c.score >= top - _TIE), key=lambda c: c.offset
    ).stop_code


def report(
    subbook,
    *,
    sample: int = DEFAULT_SAMPLE,
    trials: int = DEFAULT_TRIALS,
    depth: int = DEFAULT_DEPTH,
) -> list[Candidate]:
    """全候補とその点数を、良い順に。推定の根拠を説明するためのもの。"""
    scored = _weigh(subbook, sample, trials, depth)
    return sorted(scored, key=lambda c: (-c.score, c.offset))


__all__ = [
    "Candidate",
    "infer",
    "report",
    "DEFAULT_SAMPLE",
    "DEFAULT_TRIALS",
    "DEFAULT_DEPTH",
]
