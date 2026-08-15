# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 satorux
# Copyright (c) 1997-2006 Motoyuki Kasahara

"""サブブックの語索引を引く。

索引は 2048 バイトのページからなる B 木である。どのページも 4 バイトの
ヘッダで始まる。ID バイト、項目の長さ（可変長なら 0）、16 ビットの項目数。
中間ページは鍵を子ページの番号に対応させ、葉ページは鍵をその項目の本文と
見出しの位置に対応させる。

葉ページの形は 3 通りあり、ページ ID のビットで区別される。固定長の項目、
可変長の項目、そして **グループ** 項目——正規化すると同じ鍵になる見出し語が
複数あるとき、それらは整列した 1 つの連なりを共有する。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterator, NamedTuple

from . import match as _match
from .jacode import CHARCODE_ISO8859_1
from .setword import (
    WORD_ALPHABET,
    WORD_KANA,
    Query,
    WordError,
    convert_to_jisx0208,
    convert_to_latin,
    fix_word,
)
from .subbook import SubBook
from .zio import EbError

MAX_INDEX_DEPTH = 6

PAGE_ID_LEAF = 0x80
PAGE_ID_LAYER_START = 0x40
PAGE_ID_LAYER_END = 0x20
PAGE_ID_HAS_GROUP = 0x10

GROUP_SINGLE = 0x00
GROUP_START = 0x80
GROUP_MEMBER = 0xC0

#: 比較関数がかなを畳む索引。つまり、索引が並んでいる順と検索が比較する順が
#: 一致しない。見出し語はひらがなとカタカナで 1 回ずつ、計 2 回登録されており、
#: 2 つの写しは索引の中で遠く離れているのに比較関数はそれらを等しいと言う。
#: 下の 2 つの近道をここで使ってはいけないのはそのためである。
KANA_INDEX_IDS = (0x70, 0x90)

Comparator = Callable[[bytes, bytes], int]


class Layout(NamedTuple):
    """グループ化された葉項目の、各欄の幅。

    2 つの形は細かいところで違うのに、ページ中のどのバイトもそれを告げない。
    語索引はグループの構成要素数を 2 バイトで書き、構成要素ごとに見出し語を
    繰り返す。multi 検索の索引は個数を 4 バイトで書き、構成要素には何も
    付けない——どれも同じ語だからである。
    """

    #: 0x80 のグループヘッダにある、構成要素数のバイト数。
    group_count: int = 2
    #: 0xc0 の構成要素がグループの鍵を繰り返すかどうか。
    member_key: bool = True


WORD_LAYOUT = Layout()
MULTI_LAYOUT = Layout(group_count=4, member_key=False)


class SearchError(EbError):
    """索引ページが壊れている。"""


class NoSuchSearchError(SearchError):
    """この種の問い合わせに答えられる索引を、このサブブックは持たない。

    索引が壊れている場合とは別物である。英語だけの辞書で日本語の語を引けない
    のは当たり前であって、複数の本を同時に検索する側はこれを失敗ではなく
    「この本には結果なし」として扱うべきである。
    """


@dataclass(frozen=True)
class Position:
    """本文ファイル内の位置。1 起点のページ番号とバイト位置。"""

    page: int
    offset: int

    def __repr__(self) -> str:
        return f"{self.page}:{self.offset}"


@dataclass(frozen=True)
class Hit:
    """一致した索引項目 1 つ。その本文と見出しの在り処。"""

    text: Position
    heading: Position


def _uint(data: bytes, offset: int, width: int) -> int:
    return int.from_bytes(data[offset : offset + width], "big")


# -- 索引と比較関数を選ぶ ------------------------------------------------


def _select_index(subbook: SubBook, word_code: str, backward: bool = False):
    """引く索引を選ぶ。libeb と同じ優先順位で代替に落ちていく。"""
    subbook.load()
    kind = "endword" if backward else "word"
    preferred = {
        WORD_ALPHABET: (f"{kind}_alphabet", f"{kind}_asis"),
        WORD_KANA: (f"{kind}_kana", f"{kind}_asis"),
    }.get(word_code, (f"{kind}_asis",))

    for name in preferred:
        search = subbook.searches.get(name)
        if search is not None and search.start_page != 0:
            return name, search
    raise NoSuchSearchError(
        f"{subbook.title!r}: no {kind} index for a {word_code} search word"
    )


def _comparators(
    subbook: SubBook, index_name: str, exact: bool
) -> tuple[Comparator, Comparator, Comparator]:
    """(中間ノード用, 葉用, グループ用) の比較関数を返す。"""
    if subbook.book.character_code == CHARCODE_ISO8859_1:
        if exact:
            return (
                _match.exact_pre_match_word_latin,
                _match.exact_match_word_latin,
                _match.exact_match_word_latin,
            )
        return (_match.pre_match_word, _match.match_word, _match.match_word)

    if index_name.endswith("_kana"):
        if exact:
            return (
                _match.exact_pre_match_word_jis,
                _match.exact_match_word_kana_single,
                _match.exact_match_word_kana_group,
            )
        return (
            _match.pre_match_word,
            _match.match_word_kana_single,
            _match.match_word_kana_group,
        )

    if exact:
        return (
            _match.exact_pre_match_word_jis,
            _match.exact_match_word_jis,
            _match.exact_match_word_kana_group,
        )
    return (_match.pre_match_word, _match.match_word, _match.match_word_kana_group)


def prepare(
    subbook: SubBook, text: str, exact: bool = False, backward: bool = False
):
    """``text`` を検索するための問い合わせと比較関数を用意する。"""
    if subbook.book.character_code == CHARCODE_ISO8859_1:
        raw, word_code = convert_to_latin(text)
    else:
        raw, word_code = convert_to_jisx0208(text)

    index_name, search = _select_index(subbook, word_code, backward)
    query = fix_word(
        search, raw, subbook.book.character_code, word_code, backward=backward
    )
    return query, index_name, _comparators(subbook, index_name, exact)


# -- B 木の走査 ----------------------------------------------------------


def _descend(subbook: SubBook, page: int, query: Query, compare_pre: Comparator) -> int | None:
    """中間ページをたどり、語が載っているかもしれない葉ページまで降りる。

    葉ページの番号を返す。語が索引の全項目より後ろに並ぶ場合は ``None``。
    """
    for _ in range(MAX_INDEX_DEPTH):
        data = subbook.zio.read_page(page)
        if len(data) < 4:
            raise SearchError(f"page {page}: truncated index page")

        page_id = data[0]
        entry_length = data[1]
        entry_count = _uint(data, 2, 2)

        if page_id & PAGE_ID_LEAF:
            return page

        next_page = page
        offset = 4
        for _entry in range(entry_count):
            if len(data) < offset + entry_length + 4:
                raise SearchError(f"page {page}: index entry overruns the page")
            key = data[offset : offset + entry_length]
            if compare_pre(query.canonicalized, key) <= 0:
                next_page = _uint(data, offset + entry_length, 4)
                break
            offset += entry_length + 4
        else:
            return None

        if next_page == page:
            return None
        page = next_page

    raise SearchError(f"index is deeper than {MAX_INDEX_DEPTH} levels")


def _iter_leaf_hits(
    subbook: SubBook,
    page: int,
    query: Query,
    compare_single: Comparator,
    compare_group: Comparator,
    layout: Layout = WORD_LAYOUT,
) -> Iterator[Hit]:
    """``page`` から葉ページを前へ走査し、一致したものを順に返す。

    グループはページ境界で割れることがある。0x80 のヘッダが 1 ページの
    最後になり、その 0xc0 の構成要素が次のページの頭から始まる。構成要素は
    照合すべき鍵を自分で持たず、ヘッダの鍵しかない。だからヘッダが何を
    決めたかを示す 2 つのフラグはページをまたいで生き延びなければならない
    ——ページごとに初期化すると、境界にまたがったグループの見出し語が
    黙って全部消える。広辞苑 の 羅生門河岸 がそれだった。収録されていて、
    読めるのに、見つからない。
    """
    comparison = 1
    in_group = False

    while True:
        data = subbook.zio.read_page(page)
        if len(data) < 4:
            raise SearchError(f"page {page}: truncated leaf page")

        page_id = data[0]
        entry_length = data[1]
        entry_count = _uint(data, 2, 2)
        if not page_id & PAGE_ID_LEAF:
            raise SearchError(f"page {page}: expected a leaf page")

        has_group = bool(page_id & PAGE_ID_HAS_GROUP)
        variable = entry_length == 0
        offset = 4

        for _entry in range(entry_count):
            if has_group:
                offset, comparison, in_group, hit = _read_group_entry(
                    data, offset, query, compare_single, compare_group,
                    comparison, in_group, page, layout,
                )
            elif variable:
                offset, comparison, hit = _read_variable_entry(
                    data, offset, query, compare_single, page
                )
            else:
                offset, comparison, hit = _read_fixed_entry(
                    data, offset, entry_length, query, compare_single, page
                )
            if hit is not None:
                yield hit
            if comparison < 0:
                return

        if page_id & PAGE_ID_LAYER_END:
            return
        page += 1


def _hit_at(data: bytes, base: int) -> Hit:
    """項目の鍵に続く、本文と見出しの位置を読む。"""
    return Hit(
        text=Position(_uint(data, base, 4), _uint(data, base + 4, 2)),
        heading=Position(_uint(data, base + 6, 4), _uint(data, base + 10, 2)),
    )


def _read_fixed_entry(data, offset, entry_length, query, compare, page):
    if len(data) < offset + entry_length + 12:
        raise SearchError(f"page {page}: fixed entry overruns the page")
    comparison = compare(query.word, data[offset : offset + entry_length])
    hit = _hit_at(data, offset + entry_length) if comparison == 0 else None
    return offset + entry_length + 12, comparison, hit


def _read_variable_entry(data, offset, query, compare, page):
    if len(data) < offset + 1:
        raise SearchError(f"page {page}: variable entry overruns the page")
    entry_length = data[offset]
    if len(data) < offset + entry_length + 13:
        raise SearchError(f"page {page}: variable entry overruns the page")
    comparison = compare(query.word, data[offset + 1 : offset + 1 + entry_length])
    hit = _hit_at(data, offset + entry_length + 1) if comparison == 0 else None
    return offset + entry_length + 13, comparison, hit


def _read_group_entry(
    data,
    offset,
    query,
    compare_single,
    compare_group,
    comparison,
    in_group,
    page,
    layout: Layout = WORD_LAYOUT,
):
    """グループ化された葉ページから項目を 1 つ読む。

    グループは、共有する正規化済みの鍵を持つ 0x80 のヘッダで始まる。続く
    0xc0 の構成要素は正規化前の語と比較されるが、それはグループ自身の鍵が
    まだ一致している間だけである。構成要素が自分の鍵を持たない場合——
    multi 検索の索引——は、ヘッダの判定をそのまま受け継ぐ。
    """
    if len(data) < offset + 2:
        raise SearchError(f"page {page}: group entry overruns the page")
    group_id = data[offset]
    entry_length = data[offset + 1]

    if group_id == GROUP_SINGLE:
        if len(data) < offset + entry_length + 14:
            raise SearchError(f"page {page}: group entry overruns the page")
        comparison = compare_single(
            query.canonicalized, data[offset + 2 : offset + 2 + entry_length]
        )
        hit = _hit_at(data, offset + entry_length + 2) if comparison == 0 else None
        return offset + entry_length + 14, comparison, False, hit

    if group_id == GROUP_START:
        key = 2 + layout.group_count
        if len(data) < offset + entry_length + key:
            raise SearchError(f"page {page}: group header overruns the page")
        comparison = compare_single(
            query.canonicalized, data[offset + key : offset + key + entry_length]
        )
        return offset + entry_length + key, comparison, True, None

    if group_id == GROUP_MEMBER:
        if not layout.member_key:
            if len(data) < offset + 13:
                raise SearchError(f"page {page}: group member overruns the page")
            hit = _hit_at(data, offset + 1) if comparison == 0 and in_group else None
            return offset + 13, comparison, in_group, hit
        if len(data) < offset + 14:
            raise SearchError(f"page {page}: group member overruns the page")
        hit = None
        if comparison == 0 and in_group:
            member = data[offset + 2 : offset + 2 + entry_length]
            if compare_group(query.word, member) == 0:
                hit = _hit_at(data, offset + entry_length + 2)
        return offset + entry_length + 14, comparison, in_group, hit

    raise SearchError(f"page {page}: unknown group id 0x{group_id:02x}")


# -- 公開する入口 ---------------------------------------------------------


def _last_key(subbook: SubBook, search_index) -> bytes | None:
    """索引に収められた最大の鍵。読めなければ None。

    索引のページは根が先、葉が最後という並びなので、最終ページに最後の項目が
    ある。この 1 ページを見るだけで全ページを見ずに済む。
    :func:`_beyond_the_index` を参照。

    語索引の配置専用である——keyword 索引の構成要素は 7 バイトで鍵を持たず、
    ここの寸法で読めば項目の外へ出てしまう。呼び出し元はどちらも語検索。
    """
    try:
        data = subbook.zio.read_page(search_index.end_page)
    except EbError:
        return None
    if len(data) < 4 or not data[0] & PAGE_ID_LEAF:
        return None

    entry_length = data[1]
    count = _uint(data, 2, 2)
    has_group = bool(data[0] & PAGE_ID_HAS_GROUP)
    offset = 4
    key = None
    for _entry in range(count):
        if offset + 2 > len(data):
            return None
        if has_group:
            group_id = data[offset]
            length = data[offset + 1]
            if group_id == GROUP_SINGLE:
                key = data[offset + 2 : offset + 2 + length]
                offset += length + 14
            elif group_id == GROUP_START:
                key = data[offset + 4 : offset + 4 + length]
                offset += length + 4
            elif group_id == GROUP_MEMBER:
                key = data[offset + 2 : offset + 2 + length]
                offset += length + 14
            else:
                return None
        elif entry_length == 0:
            length = data[offset]
            key = data[offset + 1 : offset + 1 + length]
            offset += length + 13
        else:
            key = data[offset : offset + entry_length]
            offset += entry_length + 12
        if offset > len(data):
            return None
    return key


def _first_key(subbook: SubBook, page: int) -> bytes | None:
    """葉ページの先頭の鍵。葉ページでなければ None。

    :func:`_last_key` と同じく、語索引の配置専用。
    """
    try:
        data = subbook.zio.read_page(page)
    except EbError:
        return None
    if len(data) < 6 or not data[0] & PAGE_ID_LEAF or _uint(data, 2, 2) == 0:
        return None

    entry_length = data[1]
    if data[0] & PAGE_ID_HAS_GROUP:
        group_id = data[4]
        length = data[5]
        start = 8 if group_id == GROUP_START else 6
        if group_id not in (GROUP_SINGLE, GROUP_START, GROUP_MEMBER):
            return None
        return data[start : start + length]
    if entry_length == 0:
        return data[5 : 5 + data[4]]
    return data[4 : 4 + entry_length]


def _skip_to_page(
    subbook: SubBook, query: Query, compare: Comparator, page: int
) -> int:
    """一致がありえない葉を飛ばして、走査を先へ進める。

    木はいつも語を正しい葉に置いてくれるわけではない。索引ページの最後の
    項目は 0xff の並びで、「最後の実在の鍵より後ろのすべて」を意味するが、
    それが 1 つ前の項目と同じ子を指していることがある。そうなると降下は
    早めに止まり、あとは葉を前へたどっていくことが前提になる——
    ランダムハウス の ｚｙｍｕｒｇｙ でそれは 1,741 ページ分になる。

    葉は連続していて整列しているので、代わりに各葉の先頭の鍵を 2 分探索
    すれば、どこから始めればよいかが分かる。ページを飛ばして安全なのは、
    一致するものが語と同じか語より後ろに並ぶからである。**次の** ページも
    まだ語より前で始まるなら、このページのものはすべて語より前にある。
    """
    if query.search.index_id in KANA_INDEX_IDS:
        return page
    low, high = page, query.search.end_page
    if high <= low:
        return page
    best = page
    while low <= high:
        middle = (low + high) // 2
        key = _first_key(subbook, middle)
        if key is None:
            return best
        if compare(query.word, key) > 0:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    return best


def _beyond_the_index(
    subbook: SubBook, query: Query, compare: Comparator
) -> bool:
    """その語は、索引の全項目より後ろに並ぶか。

    ページの配置には部分木がどこで終わるかを示すものが何もない。だから
    部分木の末尾を越えた検索は、そのまま葉を前へ読み続ける——全見出し語より
    後ろに並ぶ語であれば、索引を丸ごと読むことになる。辞書を混ぜて持っている
    と、これは日常的に起きる。かなの問い合わせはどれも ランダムハウス にも
    投げられ、その最後の見出し語は ＺＹＭＹ なので、以前は 1,741 ページを
    たどって何も見つけずに終わっていた。1 ページで答えが出るようにした。
    """
    if query.search.index_id in KANA_INDEX_IDS:
        return False
    key = _last_key(subbook, query.search)
    return key is not None and compare(query.word, key) > 0


def search(
    subbook: SubBook,
    text: str,
    exact: bool = False,
    limit: int | None = None,
    backward: bool = False,
) -> list[Hit]:
    """``subbook`` から ``text`` を検索する。

    ``exact=False``（既定）なら ``text`` で始まる見出し語がすべて一致する。
    eblook が word 検索と呼ぶものである。``exact=True`` なら見出し語全体が
    一致するものだけ。

    ``backward`` は代わりに endword 索引を引く。そこでは見出し語がすべて
    逆順に収められているので、前方一致が見出し語の **末尾** の一致になる。
    """
    query, _index_name, (compare_pre, compare_single, compare_group) = prepare(
        subbook, text, exact, backward
    )

    if _beyond_the_index(subbook, query, compare_single):
        return []

    leaf_page = _descend(subbook, query.search.start_page, query, compare_pre)
    if leaf_page is None:
        return []
    leaf_page = _skip_to_page(subbook, query, compare_single, leaf_page)

    # かなの索引は見出し語をひらがなとカタカナで 1 回ずつ、計 2 回収める。
    # そしてかな用の比較関数は か と カ を同じ文字として扱う——だから
    # かなで引くと、どの項目も 2 つずつ返ってくる。広辞苑 はこの対に
    # 見出しレコードを 1 つだけ与え、大辞林 は同じ内容のものを 2 つ与える。
    # 写しどうしが必ず共有しているのは項目自身の位置なので、ここではそれで
    # 見分ける。
    #
    # 他の索引では、見出しと本文の両方が一致したときにだけ重複を落とす。
    # 1 つの項目を共有する 2 つの見出し語は、どちらも見せる価値があるからだ。
    if query.search.index_id in (0x70, 0x90):
        identity = lambda hit: hit.text  # noqa: E731
    else:
        identity = lambda hit: hit  # noqa: E731

    hits = []
    seen = set()
    for hit in _iter_leaf_hits(
        subbook, leaf_page, query, compare_single, compare_group
    ):
        key = identity(hit)
        if key in seen:
            continue
        seen.add(key)
        hits.append(hit)
        if limit is not None and len(hits) >= limit:
            break
    return hits


def _first_leaf(subbook: SubBook, page: int) -> int:
    """根のページから、その下の一番左の葉まで降りる。"""
    for _ in range(MAX_INDEX_DEPTH):
        data = subbook.zio.read_page(page)
        if len(data) < 4:
            raise SearchError(f"page {page}: truncated index page")
        if data[0] & PAGE_ID_LEAF:
            return page
        entry_length = data[1]
        if len(data) < 4 + entry_length + 4:
            raise SearchError(f"page {page}: index entry overruns the page")
        child = _uint(data, 4 + entry_length, 4)
        if child == page:
            raise SearchError(f"page {page}: index points at itself")
        page = child
    raise SearchError(f"index is deeper than {MAX_INDEX_DEPTH} levels")


def iter_index(subbook: SubBook, name: str = "word_asis") -> Iterator[Hit]:
    """サブブックのある索引に収められた項目を、索引順にすべて返す。

    これは問いへの答えではなく本 1 冊分そのものなので、辞書に何が入って
    いるかを知るための手段になる——項目が何件あるか、それぞれの本文が
    どこから始まるか。比較関数が何に対しても「等しい」と答えるようにして、
    検索が使うのと同じ葉の走査をそのまま全件列挙に変えている。
    """
    subbook.load()
    search_index = subbook.searches.get(name)
    if search_index is None or search_index.start_page == 0:
        raise NoSuchSearchError(f"{subbook.title!r}: no {name} index")

    everything: Comparator = lambda _a, _b: 0  # noqa: E731
    query = Query(word=b"", canonicalized=b"", word_code=WORD_ALPHABET, search=search_index)
    page = _first_leaf(subbook, search_index.start_page)
    return _iter_leaf_hits(subbook, page, query, everything, everything)


def search_word(subbook: SubBook, text: str, limit: int | None = None) -> list[Hit]:
    """前方一致。eblook の ``search`` にあたる。"""
    return search(subbook, text, exact=False, limit=limit)


def search_endword(subbook: SubBook, text: str, limit: int | None = None) -> list[Hit]:
    """後方一致。``text`` で **終わる** 見出し語を探す。"""
    return search(subbook, text, exact=False, limit=limit, backward=True)


def search_exactword(
    subbook: SubBook, text: str, limit: int | None = None
) -> list[Hit]:
    """見出し語全体の一致。eblook で exact 索引を指定した ``search`` にあたる。"""
    return search(subbook, text, exact=True, limit=limit)


# -- ワイルドカード -------------------------------------------------------

WILDCARD = "*"


class Pattern(NamedTuple):
    """ワイルドカードを解いた問い合わせ。語と、どちらの端に錨を下ろすか。"""

    word: str
    backward: bool


def parse_pattern(text: str) -> Pattern:
    """見出し語のどちらの端に錨を下ろすかを示す ``*`` を読む。

    ``ization`` も ``ization*`` も、その語で始まる見出し語に一致する。これが
    既定で、どの本も対応している。``*ization`` はその語で終わる見出し語に
    一致し、本の endword 索引を使う。

    両端いっぺんに指定すれば部分一致検索になる。手元のディスクの索引には
    それ用に鍵づけられたものが 1 つもない——見出し語は先頭の文字か末尾の
    文字で収められていて、途中で収められることはない——ので、黙って別の
    答えを返すのではなく拒否する。
    """
    word = text.strip()
    backward = word.startswith(WILDCARD)
    if backward:
        word = word[1:]
    forward = word.endswith(WILDCARD)
    if forward:
        word = word[:-1]
    word = word.strip()

    if backward and forward and word:
        raise WordError(
            f"{text!r}: matching both ends at once would need a substring "
            f"index, which these dictionaries do not carry"
        )
    return Pattern(word, backward)


def search_pattern(
    subbook: SubBook, pattern: str, exact: bool = False, limit: int | None = None
) -> list[Hit]:
    """``*`` を含みうる問い合わせ文字列で検索する。"""
    word, backward = parse_pattern(pattern)
    return search(subbook, word, exact=exact, limit=limit, backward=backward)


# -- keyword 検索と cross 検索 --------------------------------------------

#: 葉に見出し語ではなく **出現箇所** を収めている索引の ID。
KEYWORD_INDEXES = ("keyword", "cross")

GROUP_MEMBER_SIZE = 7
POSITION_SIZE = 6


def _keyword_index(subbook: SubBook, name: str):
    subbook.load()
    if name not in KEYWORD_INDEXES:
        raise NoSuchSearchError(f"{name!r} is not a keyword index")
    search_index = subbook.searches.get(name)
    if search_index is None or search_index.start_page == 0:
        raise NoSuchSearchError(f"{subbook.title!r}: no {name} index")
    return search_index


def _keyword_query(subbook: SubBook, word: str, search_index) -> Query:
    if subbook.book.character_code == CHARCODE_ISO8859_1:
        raw, word_code = convert_to_latin(word)
    else:
        raw, word_code = convert_to_jisx0208(word)
    return fix_word(search_index, raw, subbook.book.character_code, word_code)


def iter_keyword_hits(
    subbook: SubBook, word: str, index: str = "keyword", headings: bool = True
) -> Iterator[Hit]:
    """keyword（または cross）索引で ``word`` が収められている箇所をすべて返す。

    これらの索引は語索引とは別の問いに答える。見出し語索引は見出し語を収めて
    その項目を指す。keyword 索引は **本文中の語** を収めて、それを使っている
    項目をすべて指す。だから 「black sheep」 で、そんな見出し語を持たない
    和英辞典から 恥晒し を見つけられる。

    葉の配置は以下のとおり。語索引と違い、寸法は宣言されておらず固定である:

    * ``0x00`` — 1 度しか使われない keyword。鍵、続いてその項目の本文位置と
      見出し位置が 6 バイトずつ。
    * ``0x80`` — グループの頭。鍵、続く構成要素の数、見出し位置 1 つ。
    * ``0xc0`` — 構成要素。項目の本文位置だけ。

    構成要素の見出しは索引にはまったく入っていない。グループの見出し位置は
    見出しの **連なり** の始まりで、構成要素 1 つにつき 1 つ、本の見出し領域に
    端から端まで並んでいる——つまり各構成要素の見出しは、1 つ前の見出しを
    読み終えたところにある。この走査が位置を集めてあとで引くのではなく、
    進みながら見出しを読んでいるのはそのためである。

    そしてその読み出しが高くつく——構成要素ごとに 1 回で、研究社新和英大辞典
    の 「A」 には 4 万件ある——ので、本文位置しか要らないときは
    ``headings=False`` でそれを省ける。そのときヒットが持つ見出しはグループ
    自身のものになり、最初の構成要素については正しく、残りについては誤る。
    """
    from .text import read_heading  # text はここから Position を取り込む。

    search_index = _keyword_index(subbook, index)
    query = _keyword_query(subbook, word, search_index)
    latin = subbook.book.character_code == CHARCODE_ISO8859_1
    compare_pre = (
        _match.exact_pre_match_word_latin if latin else _match.exact_pre_match_word_jis
    )
    compare = _match.exact_match_word_latin if latin else _match.exact_match_word_jis

    page = _descend(subbook, search_index.start_page, query, compare_pre)
    if page is None:
        return

    in_group = False
    heading: Position | None = None
    while True:
        data = subbook.zio.read_page(page)
        if len(data) < 4:
            raise SearchError(f"page {page}: truncated keyword page")
        if not data[0] & PAGE_ID_LEAF:
            raise SearchError(f"page {page}: expected a keyword leaf page")
        count = _uint(data, 2, 2)
        offset = 4

        for _entry in range(count):
            if offset >= len(data):
                raise SearchError(f"page {page}: keyword entry overruns the page")
            group_id = data[offset]

            if group_id == GROUP_SINGLE:
                length = data[offset + 1]
                base = offset + 2 + length
                comparison = compare(query.word, data[offset + 2 : base])
                if comparison == 0:
                    yield Hit(
                        text=Position(_uint(data, base, 4), _uint(data, base + 4, 2)),
                        heading=Position(
                            _uint(data, base + 6, 4), _uint(data, base + 10, 2)
                        ),
                    )
                elif comparison < 0:
                    return
                in_group = False
                offset = base + 2 * POSITION_SIZE

            elif group_id == GROUP_START:
                length = data[offset + 1]
                comparison = compare(query.word, data[offset + 6 : offset + 6 + length])
                if comparison < 0:
                    return
                in_group = comparison == 0
                base = offset + 6 + length
                heading = Position(_uint(data, base, 4), _uint(data, base + 4, 2))
                offset = base + POSITION_SIZE

            elif group_id == GROUP_MEMBER:
                if in_group and heading is not None:
                    text = Position(
                        _uint(data, offset + 1, 4), _uint(data, offset + 5, 2)
                    )
                    yield Hit(text=text, heading=heading)
                    if headings:
                        heading = read_heading(subbook, heading).next_position
                offset += GROUP_MEMBER_SIZE

            else:
                raise SearchError(
                    f"page {page}: unknown keyword entry id 0x{group_id:02x}"
                )

        if data[0] & PAGE_ID_LAYER_END:
            return
        page += 1


def keyword_positions(
    subbook: SubBook, word: str, index: str = "keyword"
) -> set[Position]:
    """keyword が指す項目だけを返す。見出しは 1 つも読まない。"""
    return {
        hit.text
        for hit in iter_keyword_hits(subbook, word, index, headings=False)
    }


def search_keyword(
    subbook: SubBook,
    words,
    index: str = "keyword",
    limit: int | None = None,
) -> list[Hit]:
    """``words`` の **すべて** を含む項目を返す。

    語は 1 つずつ引き、その結果を項目の本文位置で共通部分を取る。それが
    AND になる仕組みである。「black」 と 「sheep」 なら両方を使っている項目が、
    最初の語の一覧が与える順で見つかる。

    残りの語は位置だけを読む。見出しまで読むのは 1 つの一覧だけ——しかも
    いちばん短いものを、上限までしか——にしてある。そうしないと 「A」 のような
    ありふれた keyword は、40 件を見せるために 4 万回の見出し読み出しを
    要求してくる。
    """
    if isinstance(words, str):
        words = [words]
    words = [word for word in words if word.strip()]
    if not words:
        return []

    # 最初の語ではなく、いちばん珍しい語の一覧をたどる。どちらをたどっても
    # ヒットは項目順で返るし、「A」 ＋ 「SHEEP」 の手間が 4 万件分ではなく
    # 8 件分で済む。
    others = []
    driver = words[0]
    if len(words) > 1:
        found = {word: keyword_positions(subbook, word, index) for word in words}
        driver = min(words, key=lambda word: len(found[word]))
        others = [found[word] for word in words if word != driver]

    hits: list[Hit] = []
    seen: set[Position] = set()
    for hit in iter_keyword_hits(subbook, driver, index):
        if hit.text in seen:
            continue
        if any(hit.text not in other for other in others):
            continue
        seen.add(hit.text)
        hits.append(hit)
        if limit is not None and len(hits) >= limit:
            break
    return hits


# -- multi 検索 -----------------------------------------------------------


def _multi_search(subbook: SubBook, number: int):
    subbook.load()
    if not subbook.multis:
        raise NoSuchSearchError(f"{subbook.title!r}: no multi search")
    try:
        return subbook.multis[number]
    except IndexError:
        raise NoSuchSearchError(
            f"{subbook.title!r}: no multi search {number}; it has "
            f"{len(subbook.multis)}"
        ) from None


def iter_multi_field(
    subbook: SubBook, entry, word: str, exact: bool = False
) -> Iterator[Hit]:
    """multi 検索のある欄が ``word`` で一致させる項目を返す。"""
    from .subbook import MULTI_KANA_INDEX_IDS

    search_index = entry.index
    if search_index is None:
        raise NoSuchSearchError(f"{entry.label!r}: this field has no index")

    if subbook.book.character_code == CHARCODE_ISO8859_1:
        raw, word_code = convert_to_latin(word)
    else:
        raw, word_code = convert_to_jisx0208(word)
    query = fix_word(
        search_index, raw, subbook.book.character_code, word_code
    )

    kana = search_index.index_id in MULTI_KANA_INDEX_IDS
    compare_pre, compare_single, compare_group = _comparators(
        subbook, "word_kana" if kana else "word_asis", exact
    )
    page = _descend(subbook, search_index.start_page, query, compare_pre)
    if page is None:
        return
    yield from _iter_leaf_hits(
        subbook, page, query, compare_single, compare_group, MULTI_LAYOUT
    )


def search_multi(
    subbook: SubBook,
    number: int,
    words,
    exact: bool = False,
    limit: int | None = None,
) -> list[Hit]:
    """multi 検索の欄を埋め、そのすべてを満たすものを返す。

    ``words`` は欄ごとに 1 語ずつ、:attr:`MultiSearch.entries` が与える順に
    並べる。空文字列ならその欄は問わない。ことわざ の multi 検索には欄が
    2 つ——事項 と 作者・作品——あるので、``["恋", "西鶴"]`` は
    井原西鶴 が恋について言ったことを尋ねることになる。
    """
    multi = _multi_search(subbook, number)
    if isinstance(words, str):
        words = [words]
    asked = [
        (entry, word)
        for entry, word in zip(multi.entries, words)
        if word and word.strip()
    ]
    if not asked:
        return []

    others = [
        {hit.text for hit in iter_multi_field(subbook, entry, word, exact)}
        for entry, word in asked[1:]
    ]

    hits: list[Hit] = []
    seen: set[Position] = set()
    entry, word = asked[0]
    for hit in iter_multi_field(subbook, entry, word, exact):
        if hit.text in seen:
            continue
        if any(hit.text not in other for other in others):
            continue
        seen.add(hit.text)
        hits.append(hit)
        if limit is not None and len(hits) >= limit:
            break
    return hits
