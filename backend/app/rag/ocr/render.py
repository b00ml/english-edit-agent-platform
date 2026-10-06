"""Authenticated bounded source-page rendering. PDFium calls are serialized per process."""

from __future__ import annotations

import io
import threading
from pathlib import Path

from app.errors import DocumentParseError

_LOCK = threading.Lock()


def render_page(path: Path, page_no: int) -> tuple[bytes, tuple[float, float]]:
    import pypdfium2 as pdfium

    with _LOCK:
        document = pdfium.PdfDocument(str(path))
        page = bitmap = None
        try:
            if not 1 <= page_no <= len(document):
                raise DocumentParseError("原页超出范围")
            page = document.get_page(page_no - 1)
            width, height = page.get_size()
            if width <= 0 or height <= 0:
                raise DocumentParseError("原页尺寸无效")
            bitmap = page.render(scale=min(2.0, 1600 / max(width, height)))
            image = bitmap.to_pil()
            output = io.BytesIO()
            image.save(output, format="PNG")
            image.close()
            return output.getvalue(), (width, height)
        finally:
            if bitmap is not None:
                bitmap.close()
            if page is not None:
                page.close()
            document.close()
