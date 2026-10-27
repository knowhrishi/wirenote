# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""A call's notes and transcript as a Word file (docs/dev/import-export-design.md).

Built from the same data the call page shows (Store.call), with the standard library:
a .docx is a zip of a few XML parts. PDF comes from the browser's print dialog, which
prints the call page with app.css's print styles.
"""

from __future__ import annotations

import datetime as dt
import io
import re
import zipfile
from xml.sax.saxutils import escape

from ..process.merge import stamp

# Characters XML 1.0 does not allow; a transcript could hold one.
_BAD_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def _x(text) -> str:
    return escape(_BAD_XML.sub("", str(text)))


def export_name(call: dict, ext: str) -> str:
    started = dt.datetime.fromisoformat(call["started"])
    safe = re.sub(r"[^\w\- ]+", "", call.get("title") or "", flags=re.UNICODE).strip()[:60] or "Call"
    return f"{started:%Y-%m-%d %H%M} {safe}.{ext}"


def _run(text: str, bold: bool = False, grey: bool = False) -> str:
    props = ("<w:b/>" if bold else "") + ('<w:color w:val="767676"/>' if grey else "")
    return (f"<w:r>{f'<w:rPr>{props}</w:rPr>' if props else ''}"
            f'<w:t xml:space="preserve">{_x(text)}</w:t></w:r>')


def _p(runs: str | list[str], style: str | None = None) -> str:
    if isinstance(runs, str):
        runs = [_run(runs)]
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{ppr}{''.join(runs)}</w:p>"


def _meta(call: dict) -> str:
    started = dt.datetime.fromisoformat(call["started"])
    parts = [f"{started:%A, %d %B %Y, %H:%M}"]
    if call.get("duration_s"):
        parts.append(f"{max(1, round(call['duration_s'] / 60))} min")
    where = " · ".join(x for x in (call.get("service"), call.get("room")) if x)
    if where:
        parts.append(where)
    return " · ".join(parts)


def body_xml(call: dict) -> str:
    names = call.get("names") or {}
    out = [_p(call.get("title") or "Call", "Title"), _p([_run(_meta(call), grey=True)])]
    people = [p["name"] for p in call.get("people") or [] if p.get("label") != "Me"] \
        or call.get("attendees") or []
    if people:
        out.append(_p([_run("With: ", bold=True), _run(", ".join(people))]))

    s = call.get("summary") or {}

    def section(head: str, items: list[str]) -> None:
        if items:
            out.append(_p(head, "Heading1"))
            out.extend(_p(f"•\t{item}", "Bullet") for item in items)

    section("Summary", s.get("summary") or [])
    titles = call.get("section_titles") or {}
    for key, items in (s.get("sections") or {}).items():
        section(titles.get(key) or key.replace("_", " ").capitalize(), items or [])
    section("Decisions", s.get("decisions") or [])
    actions = s.get("action_items") or []
    if actions:
        out.append(_p("Action items", "Heading1"))
        for a in actions:
            due = f" (due: {a['due']})" if a.get("due") else ""
            out.append(_p([_run(("☑" if a.get("done") else "☐") + "\t"),
                           _run(f"{a.get('owner') or ''}: ", bold=True),
                           _run(f"{a.get('task') or ''}{due}")], "Bullet"))
    section("Open questions", s.get("open_questions") or [])
    notes = (call.get("my_notes") or "").strip()
    if notes:
        out.append(_p("My notes", "Heading1"))
        out.extend(_p(line) for line in notes.splitlines())

    out.append(_p("Transcript", "Heading1"))
    lines = call.get("lines") or []
    if not lines:
        out.append(_p([_run("No speech was recognized.", grey=True)]))
    for line in lines:
        who = names.get(line["speaker"], line["speaker"])
        out.append(_p([_run(who, bold=True), _run(f"  {stamp(line['start'])}  ", grey=True),
                       _run(line["text"])], "Line"))
    return "".join(out)


NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
</Types>"""

RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
</Relationships>"""

DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""


def _style(sid: str, name: str, ppr: str = "", rpr: str = "") -> str:
    return (f'<w:style w:type="paragraph" w:styleId="{sid}"><w:name w:val="{name}"/>'
            f'<w:basedOn w:val="Normal"/><w:qFormat/><w:pPr>{ppr}</w:pPr><w:rPr>{rpr}</w:rPr></w:style>')


STYLES = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles {NS}>'
          '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" '
          'w:eastAsia="Calibri" w:cs="Calibri"/><w:sz w:val="22"/><w:szCs w:val="22"/>'
          '</w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="80" w:line="276" '
          'w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
          '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/>'
          '<w:qFormat/></w:style>'
          + _style("Title", "Title", '<w:spacing w:after="60"/>', '<w:b/><w:sz w:val="40"/>')
          + _style("Heading1", "heading 1", '<w:keepNext/><w:spacing w:before="320" w:after="100"/>'
                   '<w:outlineLvl w:val="0"/>', '<w:b/><w:sz w:val="28"/>')
          + _style("Bullet", "Wirenote bullet", '<w:tabs><w:tab w:val="left" w:pos="360"/></w:tabs>'
                   '<w:ind w:left="360" w:hanging="360"/>')
          + _style("Line", "Wirenote transcript line", '<w:spacing w:after="120"/>')
          + "</w:styles>")


def _core(call: dict) -> str:
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/'
            'metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f"<dc:title>{_x(call.get('title') or 'Call')}</dc:title>"
            "<dc:creator>Wirenote</dc:creator></cp:coreProperties>")


def docx_bytes(call: dict) -> bytes:
    document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {NS}>'
                f"<w:body>{body_xml(call)}<w:sectPr><w:pgSz w:w=\"11906\" w:h=\"16838\"/>"
                '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" '
                'w:header="567" w:footer="567" w:gutter="0"/></w:sectPr></w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in (("[Content_Types].xml", CONTENT_TYPES), ("_rels/.rels", RELS),
                           ("word/_rels/document.xml.rels", DOC_RELS),
                           ("word/document.xml", document), ("word/styles.xml", STYLES),
                           ("docProps/core.xml", _core(call))):
            z.writestr(name, text)
    return buf.getvalue()
