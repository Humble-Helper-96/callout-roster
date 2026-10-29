"""A tiny text-only PDF writer. No dependencies, no outside calls.

Enough for an official record: Helvetica / Helvetica-Bold / Courier, left
aligned lines, automatic page breaks, page numbers. Text is Latin-1; other
characters are replaced so the file always opens.
"""
from __future__ import annotations

PAGE_W, PAGE_H = 612, 792          # US Letter, points
MARGIN = 54
LINE_GAP = 1.35

FONTS = {"body": "Helvetica", "bold": "Helvetica-Bold", "mono": "Courier"}


def _esc(text: str) -> str:
    text = text.encode("latin-1", "replace").decode("latin-1")
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").replace("\r", "")


def _wrap(text: str, size: float, font: str, width: float) -> list[str]:
    """Greedy wrap using a rough per-character width (Courier is fixed)."""
    per_char = 0.6 * size if font == "mono" else 0.5 * size
    max_chars = max(8, int(width / per_char))
    out: list[str] = []
    for para in (text or "").split("\n"):
        if font == "mono":                 # tables: keep every space, cut at width
            out.extend([para[i:i + max_chars] for i in range(0, len(para), max_chars)] or [""])
            continue
        words, line = para.split(" "), ""
        for w in words:
            cand = (line + " " + w).strip()
            if len(cand) <= max_chars:
                line = cand
            else:
                if line:
                    out.append(line)
                while len(w) > max_chars:          # a single over-long token
                    out.append(w[:max_chars]); w = w[max_chars:]
                line = w
        out.append(line)
    return out


class Document:
    def __init__(self, title: str):
        self.title = title
        self.pages: list[list[str]] = []
        self._ops: list[str] = []
        self.y = PAGE_H
        self._new_page()

    # ---- layout --------------------------------------------------------------
    def _new_page(self):
        if self._ops:
            self.pages.append(self._ops)
        self._ops = []
        self.y = PAGE_H - MARGIN

    def _ensure(self, height: float):
        if self.y - height < MARGIN + 24:
            self._new_page()

    def line(self, text: str = "", size: float = 10, font: str = "body", indent: float = 0, gap: float = 0):
        width = PAGE_W - 2 * MARGIN - indent
        for part in _wrap(text, size, font, width):
            self._ensure(size * LINE_GAP)
            self.y -= size * LINE_GAP
            self._ops.append(f"BT /{font} {size} Tf {MARGIN + indent:.1f} {self.y:.1f} Td ({_esc(part)}) Tj ET")
        self.y -= gap

    def heading(self, text: str, size: float = 14):
        self.space(6)
        self.line(text, size=size, font="bold", gap=2)
        self._ensure(4)
        self._ops.append(f"{MARGIN} {self.y:.1f} m {PAGE_W - MARGIN} {self.y:.1f} l 0.5 w S")
        self.y -= 6

    def kv(self, key: str, value: str):
        self.line(f"{key}: {value}", size=10)

    def space(self, pts: float = 8):
        self.y -= pts

    def table(self, headers: list[str], rows: list[list[str]], widths: list[int]):
        """Fixed-width Courier columns; widths are character counts."""
        def fmt(cells):
            return "  ".join(str(c)[:w].ljust(w) for c, w in zip(cells, widths))
        self.line(fmt(headers), size=8.5, font="mono")
        self.line("-" * (sum(widths) + 2 * (len(widths) - 1)), size=8.5, font="mono")
        for r in rows:
            self.line(fmt(r), size=8.5, font="mono")

    # ---- output --------------------------------------------------------------
    def render(self) -> bytes:
        if self._ops or not self.pages:
            self.pages.append(self._ops)
        n = len(self.pages)
        objs: list[bytes] = []

        def add(body: str | bytes) -> int:
            objs.append(body.encode("latin-1") if isinstance(body, str) else body)
            return len(objs)

        font_ids = {}
        for key, name in FONTS.items():
            font_ids[key] = add(f"<< /Type /Font /Subtype /Type1 /BaseFont /{name} /Encoding /WinAnsiEncoding >>")
        res = "<< /Font << " + " ".join(f"/{k} {v} 0 R" for k, v in font_ids.items()) + " >> >>"
        pages_id = len(objs) + 1 + 2 * n   # placeholder slot computed below
        page_ids = []
        content_ids = []
        for i, ops in enumerate(self.pages, start=1):
            footer = f"BT /body 8 Tf {MARGIN} {MARGIN - 20} Td ({_esc(self.title)}  -  page {i} of {n}) Tj ET"
            stream = ("\n".join(ops + [footer])).encode("latin-1")
            cid = add(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
            content_ids.append(cid)
        pages_id = len(objs) + n + 1
        for cid in content_ids:
            pid = add(f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
                      f"/Resources {res} /Contents {cid} 0 R >>")
            page_ids.append(pid)
        kids = " ".join(f"{p} 0 R" for p in page_ids)
        assert add(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>") == pages_id
        catalog = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>")
        info = add(f"<< /Title ({_esc(self.title)}) /Producer (Callout Roster) >>")

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, body in enumerate(objs, start=1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
        for off in offsets:
            out += f"{off:010d} 00000 n \n".encode()
        out += (f"trailer\n<< /Size {len(objs) + 1} /Root {catalog} 0 R /Info {info} 0 R >>\n"
                f"startxref\n{xref}\n%%EOF\n").encode()
        return bytes(out)
