#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 satorux

"""Python 版 bkbook を正解器にして、JavaScript 版の照合用データを書き出す。

    $ python3 web/test/golden.py ~/eb > golden.json
    $ node web/test/compare.mjs golden.json ~/eb

本の目録、データファイルのページのハッシュ、検索結果、索引の全件列挙の
ハッシュを JSON に出す。JavaScript 側は同じことをして突き合わせる。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from bkbook import Book  # noqa: E402
from bkbook.book import find_books  # noqa: E402
from bkbook.search import (  # noqa: E402
    NoSuchSearchError,
    Position,
    iter_index,
    iter_keyword_hits,
    search,
    search_keyword,
    search_multi,
)
from bkbook.setword import WordError  # noqa: E402
from bkbook.zio import EbError  # noqa: E402
from bkbook import appendix as appendix_module  # noqa: E402
from bkbook import gaiji as gaiji_module  # noqa: E402
from bkbook import stopcode  # noqa: E402
from bkbook.cli import _readable, categorise, nearby_appendix  # noqa: E402
from bkbook.font import FontError, font_set  # noqa: E402
from bkbook.text import PlainTextRenderer, read_heading, read_text  # noqa: E402

TEXT_QUERIES = ["light", "ひかり", "光", "book", "a", "こころ", "*tion"]
TEXT_HITS = 5

QUERIES = [
    "book", "light", "l", "a", "the", "ｂｏｏｋ", "Book", "BOOK", "new york",
    "New York", "black sheep", "japan", "zymurgy", "zzzz", "z",
    "ひかり", "ヒカリ", "光", "こころ", "コーヒー", "こーひー", "こうこう", "きゃく",
    "がっこう", "ぱん", "ん", "ー", "日本", "日本語", "ﾋｶﾘ", "光り",
    "*ization", "*tion", "*しい", "*光", "*ー", "book*", "*", "", "   ", "café",
    "a b", "あ　い", "ａ",
]
KEYWORDS = [["light"], ["black", "sheep"], ["日本"], ["こころ"], ["A"], ["zzzz"]]
MULTI_WORDS = [["恋", ""], ["", "西鶴"], ["恋", "西鶴"], ["人生", ""], ["日本", ""]]
LIMIT = 50


def position(p):
    return [p.page, p.offset]


def hit(h):
    return [h.text.page, h.text.offset, h.heading.page, h.heading.offset]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def search_record(subbook, text, exact, backward):
    try:
        hits = search(subbook, text, exact=exact, limit=LIMIT, backward=backward)
    except WordError:
        return {"error": "WordError"}
    except NoSuchSearchError:
        return {"error": "NoSuchSearchError"}
    return {"hits": [hit(h) for h in hits]}


def index_hash(subbook, name):
    try:
        digest = hashlib.sha256()
        count = 0
        for h in iter_index(subbook, name):
            digest.update(f"{h.text.page}:{h.text.offset}/{h.heading.page}:{h.heading.offset}\n".encode())
            count += 1
        return {"count": count, "sha256": digest.hexdigest()}
    except NoSuchSearchError:
        return {"error": "NoSuchSearchError"}


def text_record(subbook, position, gaiji, heading=False):
    try:
        result = (read_heading if heading else read_text)(
            subbook, position, renderer=PlainTextRenderer(gaiji=gaiji)
        )
    except EbError as error:
        return {"error": type(error).__name__}
    return {
        "text": result.text,
        "readable": _readable(result.text),
        "stop": result.stop,
        "next": position_list(result.next_position),
        "references": [[r.text_of(result.text), r.position.page, r.position.offset] for r in result.references],
        "candidates": [
            [c.text_of(result.text), None if c.position is None else [c.position.page, c.position.offset]]
            for c in result.candidates
        ],
        "unknown_gaiji": [gaiji_module.format_code(n, c) for n, c in result.unknown_gaiji],
    }


def position_list(p):
    return [p.page, p.offset]


def font_record(subbook, narrow):
    try:
        fonts = font_set(subbook, narrow)
    except FontError:
        return None
    digest = hashlib.sha256()
    codes = list(fonts.codes())
    errors = 0
    for code in codes[:64] + codes[-8:]:
        try:
            digest.update(fonts.bitmap(code).data)
        except FontError:
            errors += 1
    return {
        "start": fonts.start, "end": fonts.end, "count": fonts.count,
        "width": fonts.width, "height": fonts.height, "codes": len(codes),
        "first": codes[:3], "last": codes[-3:], "sha256": digest.hexdigest(), "errors": errors,
    }


def table_sha(table):
    digest = hashlib.sha256()
    for key in sorted(table):
        digest.update(f"{key}\t{table[key]}\n".encode())
    return digest.hexdigest()


def search_dict(s):
    return {
        "index_id": s.index_id, "start_page": s.start_page, "end_page": s.end_page,
        "katakana": s.katakana, "lower": s.lower, "mark": s.mark,
        "long_vowel": s.long_vowel, "double_consonant": s.double_consonant,
        "contracted_sound": s.contracted_sound, "small_vowel": s.small_vowel,
        "voiced_consonant": s.voiced_consonant, "p_sound": s.p_sound, "space": s.space,
    }


def main(argv):
    root = argv[1] if len(argv) > 1 else "eb"
    full = "--quick" not in argv
    out = {"root": root, "books": []}
    for path in find_books(root):
        book = Book(path)
        record = {
            "path": os.path.relpath(path, root),
            "disc": book.disc_code,
            "character_code": book.character_code,
            "epwing_version": book.epwing_version,
            "subbooks": [],
        }
        for subbook in book.subbooks:
            subbook.load()
            zio = subbook.zio
            pages = {}
            last = (zio.file_size + 2047) // 2048
            sample = sorted({1, 2, 3, subbook.index_page, last, last - 1, last // 2, last // 3, last * 2 // 3})
            for page in sample:
                if 1 <= page <= last:
                    pages[str(page)] = sha(zio.read_page(page))
            sb = {
                "code": subbook.code,
                "title": subbook.title,
                "directory": subbook.directory_name,
                "index_page": subbook.index_page,
                "zio": {
                    "path": os.path.relpath(zio.path, root),
                    "code": zio.code,
                    "slice_size": zio.slice_size,
                    "file_size": zio.file_size,
                    "pages": pages,
                },
                "searches": {name: search_dict(s) for name, s in subbook.searches.items()},
                "multis": [
                    {
                        "label": m.label,
                        "entries": [
                            {
                                "label": e.label,
                                "index": search_dict(e.index) if e.index else None,
                                "candidates": [c.index_id for c in e.candidates],
                            }
                            for e in m.entries
                        ],
                    }
                    for m in subbook.multis
                ],
                "fonts": {
                    "narrow": {str(k): [f.page, f.file_name] for k, f in subbook.narrow_fonts.items()},
                    "wide": {str(k): [f.page, f.file_name] for k, f in subbook.wide_fonts.items()},
                },
                "queries": [],
                "keywords": [],
                "multi_queries": [],
                "indexes": {},
            }
            for text in QUERIES:
                for exact in (False, True):
                    for backward in (False, True):
                        sb["queries"].append({
                            "text": text, "exact": exact, "backward": backward,
                            **search_record(subbook, text, exact, backward),
                        })
            for words in KEYWORDS:
                try:
                    hits = [hit(h) for h in iter_keyword_hits(subbook, words[0], headings=False)]
                    positions = sorted(
                        position(h.text) for h in search_keyword(subbook, words, limit=None)
                    ) if len(words) > 1 else None
                    sb["keywords"].append({"words": words, "hits": hits[:500], "count": len(hits),
                                           "positions": positions})
                except NoSuchSearchError:
                    sb["keywords"].append({"words": words, "error": "NoSuchSearchError"})
                except WordError:
                    sb["keywords"].append({"words": words, "error": "WordError"})
            for words in MULTI_WORDS:
                try:
                    hits = search_multi(subbook, 0, words, limit=LIMIT)
                    sb["multi_queries"].append({"words": words, "hits": [hit(h) for h in hits]})
                except NoSuchSearchError:
                    sb["multi_queries"].append({"words": words, "error": "NoSuchSearchError"})
                except WordError:
                    sb["multi_queries"].append({"words": words, "error": "WordError"})
            if full:
                for name in ("word_asis", "word_kana", "endword_asis"):
                    sb["indexes"][name] = index_hash(subbook, name)

            # -- 本文層 --
            appendix = None
            source = nearby_appendix(path)
            if source:
                try:
                    appendix = appendix_module.for_subbook(subbook, source)
                except appendix_module.AppendixNotFoundError:
                    pass
            sb["appendix"] = None if appendix is None else {
                "stop_code": list(appendix.stop_code) if appendix.stop_code else None,
                "narrow": len(appendix.narrow), "wide": len(appendix.wide),
                "sha256": table_sha({gaiji_module.format_code(n, c): t for (n, c), t in appendix.as_gaiji_table().items()}),
            }
            gaiji = gaiji_module.resolve(subbook, None, appendix=appendix)
            sb["gaiji"] = {"count": len(gaiji), "sha256": table_sha({gaiji_module.format_code(n, c): t for (n, c), t in gaiji.items()})}
            sb["category"] = categorise(subbook)
            sb["inferred_stop_code"] = list(stopcode.infer(subbook)) if stopcode.infer(subbook) else None
            if appendix is not None and appendix.stop_code is not None:
                subbook.stop_code = appendix.stop_code
            sb["stop_code"] = list(subbook.stop_code) if subbook.stop_code else None
            sb["texts"] = []
            for text in TEXT_QUERIES:
                try:
                    from bkbook.search import parse_pattern
                    word, backward = parse_pattern(text)
                    hits = search(subbook, word, limit=TEXT_HITS, backward=backward)
                except (WordError, NoSuchSearchError):
                    continue
                for h in hits:
                    sb["texts"].append({
                        "query": text, "hit": hit(h),
                        "heading": text_record(subbook, h.heading, gaiji, heading=True),
                        "body": text_record(subbook, h.text, gaiji),
                    })
            menu = subbook.searches.get("menu")
            sb["menu"] = text_record(subbook, Position(menu.start_page, 0), gaiji) if menu else None
            sb["fonts16"] = {"narrow": font_record(subbook, True), "wide": font_record(subbook, False)}
            record["subbooks"].append(sb)
            print(f"{path} {subbook.title}", file=sys.stderr)
        book.close()
        out["books"].append(record)
    out["gaiji_builtin"] = {
        title: {"count": len(table), "sha256": table_sha({gaiji_module.format_code(n, c): t for (n, c), t in table.items()})}
        for title, table in gaiji_module.BUILTIN.items()
    }
    json.dump(out, sys.stdout, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
