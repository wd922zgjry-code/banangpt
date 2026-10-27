# -*- coding: utf-8 -*-
"""Настоящие PDF и DOCX из текста ответа.

PDF строится matplotlib'ом в ОТДЕЛЬНОМ процессе (шрифт DejaVu из комплекта matplotlib — кириллица, греческие
буквы и математические знаки работают без доп. файлов; в основном процессе бота matplotlib не импортируется).
DOCX собирается вручную через zipfile — без python-docx/lxml, поэтому на любой версии Python ставится без проблем.

blocks — список (kind, text), kind: "text" (обычный текст) или "code" (моноширинный блок).
images — список PNG (bytes), вставляются в конец документа.
"""
import base64
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import zipfile
from io import BytesIO
from xml.sax.saxutils import escape

MAX_PDF_CHARS = 120000

_PDF_SCRIPT = r'''
import base64, io, json, re, sys
import matplotlib
matplotlib.use("Agg")
matplotlib.rcParams["pdf.fonttype"] = 42
import matplotlib.image as mpimg
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.font_manager import FontProperties

data = json.load(sys.stdin)
out_path = sys.argv[1]
W, H = 8.27, 11.69
LM = RM = 0.85
TM, BM = 0.85, 0.95
TEXT_W = (W - LM - RM) * 72.0
BODY, CODE, TITLE, HEAD = 10.5, 8.6, 16, 12.5

fig0 = plt.figure(figsize=(W, H), dpi=72)
renderer = fig0.canvas.get_renderer()
props = {
    "body": FontProperties(family="DejaVu Sans", size=BODY),
    "bold": FontProperties(family="DejaVu Sans", size=BODY, weight="bold"),
    "code": FontProperties(family="DejaVu Sans Mono", size=CODE),
    "title": FontProperties(family="DejaVu Sans", size=TITLE, weight="bold"),
}

def width(s, key):
    return renderer.get_text_width_height_descent(s, props[key], False)[0]

def wrap(paragraph, key, max_w):
    words = paragraph.split(" ")
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w) if cur else w
        if width(trial, key) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    # очень длинные "слова" режем жёстко
    fixed = []
    for ln in lines:
        while width(ln, key) > max_w and len(ln) > 8:
            cut = len(ln)
            while cut > 4 and width(ln[:cut], key) > max_w:
                cut -= 1
            fixed.append(ln[:cut]); ln = ln[cut:]
        fixed.append(ln)
    return fixed

pages, ops, y = [], [], 0.0
def ensure(h):
    global ops, y
    if y + h > H - BM:
        pages.append(ops)
        ops, y = [], TM

ops, y = [], TM
LH_BODY, LH_CODE = BODY * 1.5 / 72, CODE * 1.38 / 72
BULLET = re.compile(r"^(\u2022|-|\*|\d+[.)])\s+")

def add_text(x_in, s, key, lh):
    global y
    ensure(lh)
    ops.append(("text", x_in, y, s, key))
    y += lh

if data.get("title"):
    for ln in wrap(data["title"], "title", TEXT_W):
        ensure(0.36)
        ops.append(("text", LM, y, ln, "title")); y += 0.34
    ops.append(("line", y + 0.02)); y += 0.22

for kind, text in data["blocks"]:
    if kind == "code":
        ensure(LH_CODE * 2)
        for raw in text.replace("\t", "    ").split("\n"):
            rest = raw.rstrip()
            while True:  # код переносим по символам, сохраняя отступы
                cut = len(rest)
                while cut > 4 and width(rest[:cut], "code") > TEXT_W - 12:
                    cut -= 1
                add_text(LM + 0.08, rest[:cut], "code", LH_CODE)
                rest = rest[cut:]
                if not rest:
                    break
        y += 0.12
        continue
    for par in re.split(r"\n\s*\n", text):
        for line in par.split("\n"):
            if not line.strip():
                y += LH_BODY * 0.4
                continue
            m = BULLET.match(line)
            if m:
                prefix = m.group(0).strip()
                ind = width(prefix + " ", "body")
                for i, ln in enumerate(wrap(line[m.end():], "body", TEXT_W - ind)):
                    if i == 0:
                        add_text(LM, prefix + " " + ln, "body", LH_BODY)
                    else:
                        add_text(LM + ind / 72, ln, "body", LH_BODY)
            else:
                for ln in wrap(line, "body", TEXT_W):
                    add_text(LM, ln, "body", LH_BODY)
        y += LH_BODY * 0.55

imgs = []
for b64 in data.get("images", []):
    try:
        imgs.append(mpimg.imread(io.BytesIO(base64.b64decode(b64)), format="png"))
    except Exception:
        pass
for arr in imgs:
    h_px, w_px = arr.shape[0], arr.shape[1]
    w_in = W - LM - RM
    h_in = w_in * h_px / w_px
    if h_in > H - TM - BM:
        h_in = H - TM - BM
        w_in = h_in * w_px / h_px
    ensure(h_in + 0.2)
    ops.append(("image", LM, y, w_in, h_in, arr)); y += h_in + 0.25

pages.append(ops)
pages = [p for p in pages if p] or [[]]

with PdfPages(out_path) as pdf:
    for n, page_ops in enumerate(pages, 1):
        fig = plt.figure(figsize=(W, H), dpi=72)
        for op in page_ops:
            if op[0] == "text":
                _, x, top, s, key = op
                if s is None:
                    continue
                p = props[key]
                fig.text(x / W, 1 - top / H, s, fontproperties=p, va="top", ha="left")
            elif op[0] == "line":
                fig.add_artist(plt.Line2D([LM / W, 1 - RM / W], [1 - op[1] / H] * 2, color="#888888", lw=0.8))
            else:
                _, x, top, w_in, h_in, arr = op
                ax = fig.add_axes([x / W, 1 - (top + h_in) / H, w_in / W, h_in / H])
                ax.imshow(arr); ax.axis("off")
        fig.text(0.5, 0.035, str(n), ha="center", va="bottom", fontsize=8, color="#777777")
        pdf.savefig(fig)
        plt.close(fig)
'''


def _env(tmp):
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(tmp),
           "MPLBACKEND": "Agg", "MPLCONFIGDIR": os.path.join(str(tmp), "mpl"), "LANG": "C.UTF-8"}
    for k in ("SYSTEMROOT", "TEMP", "TMP"):
        if k in os.environ:
            env[k] = os.environ[k]
    return env


def make_pdf(blocks, images=(), title="", timeout=60):
    total = sum(len(t) for _k, t in blocks)
    if total > MAX_PDF_CHARS:
        raise ValueError("текст слишком большой для PDF")
    payload = json.dumps({"title": title, "blocks": list(blocks),
                          "images": [base64.b64encode(i).decode() for i in images]}, ensure_ascii=False)
    with tempfile.TemporaryDirectory(prefix="docgen_") as td:
        os.makedirs(os.path.join(td, "mpl"), exist_ok=True)
        out = os.path.join(td, "out.pdf")
        proc = subprocess.run([sys.executable or "python3", "-I", "-c", _PDF_SCRIPT, out],
                              input=payload.encode("utf-8"), capture_output=True, timeout=timeout,
                              cwd=td, env=_env(td))
        if proc.returncode != 0 or not os.path.exists(out):
            tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["неизвестная ошибка"]
            raise RuntimeError("PDF не собрался: %s" % tail[0][:200])
        with open(out, "rb") as f:
            return f.read()


# ───────────────────────── DOCX ─────────────────────────
_NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
       'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
       'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
       'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
       'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"')
_BAD_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff]")


def _x(s):
    return escape(_BAD_XML.sub("", s))


def _run(text, bold=False, size=None, mono=False):
    props = ""
    if mono:
        props += '<w:rFonts w:ascii="Courier New" w:hAnsi="Courier New" w:cs="Courier New"/>'
    if bold:
        props += "<w:b/>"
    if size:
        props += '<w:sz w:val="%d"/>' % size
    return '<w:r>%s<w:t xml:space="preserve">%s</w:t></w:r>' % ("<w:rPr>%s</w:rPr>" % props if props else "", _x(text))


def _para(runs, shade=False, after=120, keep_empty=False):
    ppr = '<w:spacing w:after="%d"/>' % after
    if shade:
        ppr += '<w:shd w:val="clear" w:color="auto" w:fill="F2F2F2"/>'
    return "<w:p><w:pPr>%s</w:pPr>%s</w:p>" % (ppr, runs)


def _png_size(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("не PNG")
    return struct.unpack(">II", data[16:24])


def _image_par(rid, idx, w_px, h_px):
    cx = 5760000  # 16 см
    cy = int(cx * h_px / w_px)
    return (
        '<w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:drawing>'
        '<wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="%d" cy="%d"/>'
        '<wp:docPr id="%d" name="Рисунок %d"/>'
        '<wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>'
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        '<pic:pic><pic:nvPicPr><pic:cNvPr id="%d" name="chart%d.png"/><pic:cNvPicPr/></pic:nvPicPr>'
        '<pic:blipFill><a:blip r:embed="%s"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        '<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="%d" cy="%d"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic>'
        '</a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>'
    ) % (cx, cy, idx, idx, idx, idx, rid, cx, cy)


def make_docx(blocks, images=(), title=""):
    body = []
    if title:
        body.append(_para(_run(title, bold=True, size=36), after=240))
    for kind, text in blocks:
        if kind == "code":
            for line in text.replace("\t", "    ").split("\n"):
                body.append(_para(_run(line, mono=True, size=18) if line else "", shade=True, after=0))
            body.append(_para("", after=120))
            continue
        for line in text.split("\n"):
            body.append(_para(_run(line) if line.strip() else "", after=120 if line.strip() else 60))
    rels, media = [], []
    for i, png in enumerate(images, 1):
        try:
            w_px, h_px = _png_size(png)
        except Exception:
            continue
        rid = "rId%d" % i
        media.append(("word/media/image%d.png" % i, png))
        rels.append('<Relationship Id="%s" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
                    'Target="media/image%d.png"/>' % (rid, i))
        body.append(_image_par(rid, i, w_px, h_px))
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document %s><w:body>%s'
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" w:header="708" w:footer="708" w:gutter="0"/>'
        '</w:sectPr></w:body></w:document>' % (_NS, "".join(body))
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="png" ContentType="image/png"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>'
    )
    doc_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">%s</Relationships>' % "".join(rels))
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("word/document.xml", document)
        z.writestr("word/_rels/document.xml.rels", doc_rels)
        for name, data in media:
            z.writestr(name, data)
    return buf.getvalue()
