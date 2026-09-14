# -*- coding: utf-8 -*-
"""把 assets/SimHei.ttf 子集化：保留 ASCII + GB2312 全字集 + 路径图固定用字，
   用于大幅减小仓库字体体积，同时保证中文正常渲染。"""
import os
from fontTools import subset
from fontTools.ttLib import TTFont

SRC = "assets/SimHei.ttf"
OUT = "assets/SimHei.subset.ttf"
CHARS = "_subset_chars.txt"

chars = set()
# ASCII 可见字符
for c in range(0x20, 0x7F):
    chars.add(chr(c))
# GB2312 汉字区（0xB0A1–0xF7FE）
for hi in range(0xB0, 0xF8):
    for lo in range(0xA1, 0xFF):
        try:
            chars.add(bytes([hi, lo]).decode("gb2312"))
        except Exception:
            pass
# GB2312 符号区（0xA1A1–0xA9FE）
for hi in range(0xA1, 0xAA):
    for lo in range(0xA1, 0xFF):
        try:
            chars.add(bytes([hi, lo]).decode("gb2312"))
        except Exception:
            pass
# 路径图固定文字 + 常用标点/单位 + 常见台风名用字
chars |= set("经度纬度台风路径预报当前位置历史澄迈海口三亚"
             "°（）·—【】、。，！？：；“”‘’…～《》/-%")
chars |= set("海南省文昌万宁陵水东方儋州琼海五指山乐东")
text = "".join(sorted(chars))
with open(CHARS, "w", encoding="utf-8") as f:
    f.write(text)
print("charset size:", len(text))

argv = [SRC, "--text-file=" + CHARS, "--output-file=" + OUT,
        "--no-hinting", "--desubroutinize", "--drop-tables+=DSIG",
        "--name-IDs=*", "--recalc-bounds", "--layout-features=*"]
subset.main(argv)

before = os.path.getsize(SRC) / 1048576
after = os.path.getsize(OUT) / 1048576
f = TTFont(OUT)
name = f["name"].getDebugName(1)
print(f"before={before:.2f}MB  after={after:.2f}MB  family={name!r}  glyphs={len(f.getBestCmap())}")
