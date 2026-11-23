import threading
from contextlib import closing

import pypdfium2 as pdfium

# PDFium calls must be serialized, including calls for different documents.
PDFIUM_LOCK = threading.Lock()


def read_pdf_text(data: bytes, *, max_chars: int) -> tuple[str, dict]:
    parts = []
    pages_with_text = 0
    warnings = []
    remaining = max_chars
    with PDFIUM_LOCK, closing(pdfium.PdfDocument(data)) as document:
        page_count = len(document)
        for index in range(page_count):
            page_number = index + 1
            if remaining <= 0:
                warnings.append(
                    f"Extracted text reached the configured limit before page {page_number}."
                )
                break
            with (
                closing(document[index]) as page,
                closing(page.get_textpage()) as textpage,
            ):
                text = textpage.get_text_bounded()
            if text.strip():
                pages_with_text += 1
            parts.append(f"[Page {page_number}]\n{text[:remaining]}")
            if len(text) > remaining:
                warnings.append(
                    f"Text on page {page_number} exceeds the configured text limit."
                )
            remaining -= len(text)
    return "\n\n".join(parts), {
        "page_count": page_count,
        "pages_with_text": pages_with_text,
        "warnings": warnings,
    }
