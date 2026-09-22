"""DOCX rendering with python-docx.

Layout is the easy half: one column, real bullet paragraphs, standard
headings, no tables used for positioning, no text boxes, nothing
content-bearing in a header or footer. ATS parsers handle that shape; they
mangle the alternatives.

Determinism is the half that needed measuring rather than assuming. What was
actually observed with python-docx 1.2.0, rather than what folklore predicts:

* **ZIP entry timestamps are the wall clock.** Every entry is stamped with
  "now" at save time, so two renders a second apart differ. This is the one
  thing that genuinely had to be normalised.
* **`docProps/core.xml` dates are NOT the clock.** They come from the default
  template as a fixed 2013 constant. They are pinned here anyway, through
  python-docx's own API, because relying on a vendored template's constant is
  relying on something nobody promised to keep.
* **`w:rsid` values do not vary.** They are template constants; python-docx
  does not mint new ones for added paragraphs. They are therefore left alone.
  Stripping attributes out of OOXML with a regex is the brittle manipulation
  this module was told to avoid, and measurement showed it buys nothing.

So normalisation is exactly one step - repack the archive with a fixed
timestamp - plus one belt-and-braces API call. No XML is rewritten, which is
why `_normalise` cannot corrupt a document: it reads entries and writes the
same bytes back under a different mtime.
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime

from packages.services.rendering.document import ResumeDocument
from packages.services.rendering.protocol import RendererUnavailableError

DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

#: Fixed stamp for every ZIP entry and for the core properties. 1980-01-01 is
#: the earliest a ZIP can represent, and it is obviously not an authoring
#: time - which is the point. Nobody should read a creation date off a
#: generated artifact and believe it.
DETERMINISTIC_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
DETERMINISTIC_DOC_TIMESTAMP = datetime(1980, 1, 1, tzinfo=UTC)

_HEADING_STYLE = "Heading 1"
_BULLET_STYLE = "List Bullet"
_BODY_STYLE = "Body Text"


def _normalise(raw: bytes) -> bytes:
    """Repack the archive with a fixed timestamp, byte-for-byte otherwise.

    Entry order, names, compression type and the external attributes are all
    preserved from the original; only `date_time` is replaced. Nothing inside
    any entry is parsed, edited or re-serialised, so there is no path by which
    this can produce an archive Word will not open.
    """
    source = io.BytesIO(raw)
    out = io.BytesIO()
    with zipfile.ZipFile(source) as zin, zipfile.ZipFile(
        out, "w", zipfile.ZIP_DEFLATED
    ) as zout:
        for item in zin.infolist():
            stamped = zipfile.ZipInfo(item.filename, date_time=DETERMINISTIC_ZIP_TIMESTAMP)
            stamped.compress_type = item.compress_type
            stamped.external_attr = item.external_attr
            stamped.internal_attr = item.internal_attr
            stamped.create_system = item.create_system
            zout.writestr(stamped, zin.read(item.filename))
    return out.getvalue()


class DocxResumeRenderer:
    """One column, real bullets, and the same bytes every time."""

    kind = "resume_docx"
    extension = "docx"
    content_type = DOCX_CONTENT_TYPE
    production = True
    implemented = True

    def __init__(self, template_version: str) -> None:
        self.template_version = template_version

    def available(self) -> bool:
        """True only when python-docx can actually be imported.

        Checked by import rather than by a settings flag: a flag records what
        someone believed when they wrote the config, this records what is
        installed in the process about to render.
        """
        try:
            import docx  # noqa: F401
        except ImportError:
            return False
        return True

    def render(self, document: object) -> bytes:
        if not isinstance(document, ResumeDocument):
            raise TypeError("DocxResumeRenderer requires a ResumeDocument")
        try:
            import docx
        except ImportError as exc:
            raise RendererUnavailableError(
                "DOCX rendering requires python-docx"
            ) from exc

        doc = docx.Document()

        # Pin the properties that could otherwise carry a clock. `revision`
        # and `last_modified_by` are set too: Word populates them on its own
        # save, and a constant here means a file that has never been opened
        # cannot be told apart from another by its metadata.
        props = doc.core_properties
        props.title = ""
        props.author = "JobForge"
        props.last_modified_by = "JobForge"
        props.comments = ""
        props.category = ""
        props.created = DETERMINISTIC_DOC_TIMESTAMP
        props.modified = DETERMINISTIC_DOC_TIMESTAMP
        props.revision = 1

        for section in document.sections:
            doc.add_paragraph(section.heading, style=_HEADING_STYLE)
            for entry in section.entries:
                # `entry.text` is copied verbatim. There is no branch here
                # that shortens, reflows, capitalises or punctuates it: the
                # words a human approved are the words that appear.
                style = _BULLET_STYLE if section.key != "summary" else _BODY_STYLE
                doc.add_paragraph(entry.text, style=style)

        buffer = io.BytesIO()
        doc.save(buffer)
        return _normalise(buffer.getvalue())
