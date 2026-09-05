#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 satorux

"""gaiji.py の組み込み対応表を、JavaScript のモジュールとして書き出す。

    $ python3 web/tools/export_gaiji.py > web/bkbook/gaiji-table.js

表の正本は Python 側。手で直すのはそちらで、こちらは生成し直す。
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from bkbook import gaiji  # noqa: E402


def main() -> int:
    tables = {}
    for title, table in gaiji.BUILTIN.items():
        tables[title] = {
            gaiji.format_code(narrow, code): text
            for (narrow, code), text in sorted(table.items(), key=lambda item: (not item[0][0], item[0][1]))
        }
    body = json.dumps(tables, ensure_ascii=False, indent=1)
    print("// SPDX-License-Identifier: BSD-3-Clause")
    print("// Copyright (c) 2026 satorux")
    print("// Copyright (c) 1997-2006 Motoyuki Kasahara")
    print()
    print("// web/tools/export_gaiji.py が bkbook/gaiji.py から生成したもの。手で直さない。")
    print("// 鍵は h（半角）か z（全角）に 16 進 4 桁を続けた外字の符号。")
    print()
    print(f"export const BUILTIN = {body};")
    return 0


if __name__ == "__main__":
    sys.exit(main())
