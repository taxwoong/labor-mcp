# -*- coding: utf-8 -*-
"""Extract text from HWP 5.0 (OLE) BodyText sections and print chapter headings."""
import olefile, zlib, struct, re, sys

path = sys.argv[1]
ole = olefile.OleFileIO(path)

def get_text_from_section(data):
    try:
        data = zlib.decompress(data, -15)
    except zlib.error:
        pass
    out = []
    i = 0
    n = len(data)
    while i + 4 <= n:
        (hdr,) = struct.unpack_from("<I", data, i)
        tag = hdr & 0x3FF
        size = (hdr >> 20) & 0xFFF
        i += 4
        if size == 0xFFF:
            (size,) = struct.unpack_from("<I", data, i)
            i += 4
        if tag == 67:  # HWPTAG_PARA_TEXT
            chunk = data[i:i+size]
            text = []
            j = 0
            while j + 2 <= len(chunk):
                (ch,) = struct.unpack_from("<H", chunk, j)
                if ch in (10, 13):
                    text.append("\n"); j += 2
                elif ch < 32:
                    # control chars: some are 8 chars (16 bytes) long inline controls
                    if ch in (1,2,3,11,12,14,15,16,17,18,21,22,23):
                        j += 16
                    else:
                        j += 2
                else:
                    text.append(chr(ch)); j += 2
            out.append("".join(text))
        i += size
    return "\n".join(out)

sections = sorted(
    [e for e in ole.listdir() if e[0] == "BodyText"],
    key=lambda e: int(re.sub(r"\D", "", e[1])),
)
full = []
for e in sections:
    full.append(get_text_from_section(ole.openstream(e).read()))
text = "\n".join(full)

with open(path + ".txt", "w", encoding="utf-8") as f:
    f.write(text)

for line in text.splitlines():
    s = line.strip()
    if re.match(r"^제\s*\d+\s*장", s) or re.match(r"^제\s*\d+\s*절", s) or s.startswith("부칙") or s.startswith("부  칙"):
        print(s)
