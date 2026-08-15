# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 satorux

"""bkbook — EB（電子ブック）・EPWING 電子辞書を読む純 Python ライブラリ。

標準ライブラリだけで動く。ビルド不要、C 拡張なし。

    >>> from bkbook import Book
    >>> book = Book("/path/to/dictionary")
    >>> book.subbooks[0].title
"""

from .book import Book, BookError
from .subbook import Search, SubBook, SubBookError
from .zio import EbError, Zio, ZioError

__version__ = "0.1.0"

__all__ = [
    "Book",
    "BookError",
    "EbError",
    "Search",
    "SubBook",
    "SubBookError",
    "Zio",
    "ZioError",
]
