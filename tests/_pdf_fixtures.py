"""Minimal, hand-built single-page PDF generator for tests — avoids
needing a PDF-writing library just to produce fixture files; pypdf
(the app's actual dependency) only needs to *read* these, and this is
plain enough to build correctly by hand with real xref offsets.
"""


def make_test_pdf(text: str = "Hello test content") -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
        b"/MediaBox [0 0 300 300] /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    stream_content = f"BT /F1 12 Tf 20 250 Td ({text}) Tj ET".encode("latin-1")
    stream_obj = (
        b"<< /Length " + str(len(stream_content)).encode() + b" >>\nstream\n"
        + stream_content + b"\nendstream"
    )
    objects.append(stream_obj)

    buf = bytearray(b"%PDF-1.4\n")
    offsets = [0]  # object 0 is never used, placeholder for 1-indexing
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(buf))
        buf += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"

    xref_offset = len(buf)
    n = len(objects) + 1
    buf += f"xref\n0 {n}\n".encode()
    buf += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        buf += f"{off:010d} 00000 n \n".encode()
    buf += (
        f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode()
    )
    return bytes(buf)


def make_corrupt_pdf() -> bytes:
    return b"%PDF-1.4\nthis is not actually a valid pdf body at all"
