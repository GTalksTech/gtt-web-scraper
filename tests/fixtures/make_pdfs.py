"""Rebuild the PDF fixtures in tests/fixtures/pages: a text datasheet and an image-only scan.

Written by hand (no PDF library) so the fixtures don't depend on the extractor under
test. Run: python tests/fixtures/make_pdfs.py
"""

from __future__ import annotations

import zlib
from pathlib import Path

PAGES = Path(__file__).parent / "pages"


def _build(objects: list[bytes]) -> bytes:
    """A PDF whose last object is the document info dictionary."""
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info {len(objects)} 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


def _stream(data: bytes, extra: str = "") -> bytes:
    return f"<< /Length {len(data)} {extra}>>\nstream\n".encode() + data + b"\nendstream"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


_DATASHEET = [
    "GTT-7 Environmental Sensor Datasheet",
    "Overview",
    "The GTT-7 measures temperature, relative humidity and barometric pressure",
    "on a single 3 by 3 millimetre package, and reports all three over I2C or SPI.",
    "It is designed for battery powered nodes that wake, sample and sleep again,",
    "so its standby current is held under one microamp across the full range.",
    "Electrical characteristics",
    "Supply voltage ranges from 1.71 volts to 3.6 volts. Typical current while",
    "sampling all three channels at one hertz is 3.4 microamps. The interface",
    "accepts clock rates up to 3.4 megahertz in I2C high speed mode and up to",
    "10 megahertz on SPI. Pins tolerate 3.6 volts even when the part is unpowered.",
    "Measurement accuracy",
    "Temperature accuracy is plus or minus 0.3 degrees Celsius between 0 and 65",
    "degrees. Humidity accuracy is plus or minus 2 percent between 20 and 80",
    "percent relative humidity. Pressure accuracy is plus or minus 0.12 hectopascal,",
    "equivalent to about one metre of altitude, after the factory calibration.",
    "Calibration",
    "Each device is calibrated at the factory and stores its coefficients in",
    "non-volatile memory. Host software reads the coefficients once at start-up",
    "and applies the compensation formulas given in the application note.",
    "Ordering information",
    "Order code GTT-7-TR ships on tape and reel in quantities of five thousand.",
]


def text_pdf() -> bytes:
    lines = ["BT", "/F1 11 Tf", "14 TL", "50 780 Td"]
    lines += [f"({_escape(line)}) Tj T*" for line in _DATASHEET]
    lines.append("ET")
    return _build(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
            b" /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            _stream("\n".join(lines).encode()),
            b"<< /Title (GTT-7 Environmental Sensor Datasheet)"
            b" /CreationDate (D:20261002090000Z) >>",
        ]
    )


def image_pdf() -> bytes:
    """A scanned page: one grey image with dark bands where the lines of text are, and
    no text layer."""
    size = 64
    rows = []
    for y in range(size):
        band = (y // 4) % 2 == 1 and 8 <= y < 56
        rows.append(bytes(60 if band and 6 <= x < 58 else 235 for x in range(size)))
    pixels = zlib.compress(b"".join(rows))
    return _build(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
            b" /Resources << /XObject << /Im1 4 0 R >> >> /Contents 5 0 R >>",
            _stream(
                pixels,
                f"/Type /XObject /Subtype /Image /Width {size} /Height {size}"
                " /ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode ",
            ),
            _stream(b"q 512 0 0 692 50 50 cm /Im1 Do Q"),
            b"<< /Title (Scanned invoice) >>",
        ]
    )


if __name__ == "__main__":
    (PAGES / "datasheet.pdf").write_bytes(text_pdf())
    (PAGES / "scanned.pdf").write_bytes(image_pdf())
