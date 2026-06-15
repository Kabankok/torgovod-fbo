"""FBO barcode generation: EAN-13 / Code-128 → PNG bytes.

Returns PNG image bytes suitable for HTTP response or file save.
Requires: pip install python-barcode Pillow
"""

from __future__ import annotations

import io


def generate_barcode_png(
    value: str,
    barcode_type: str = "code128",
    width_mm: float = 58.0,
    height_mm: float = 28.0,
) -> bytes:
    """Generate a barcode PNG from a value string.

    Args:
        value: The barcode value (e.g. EAN-13 digits or article string)
        barcode_type: 'code128' | 'ean13' | 'ean8'
        width_mm, height_mm: Output size hint (affects dpi scaling)

    Returns:
        PNG bytes.
    """
    try:
        import barcode as bc
        from barcode.writer import ImageWriter
    except ImportError:
        raise RuntimeError(
            "python-barcode and Pillow are required: pip install python-barcode Pillow"
        )

    bc_type = barcode_type.lower()
    if bc_type == "ean13":
        bc_class = bc.get_barcode_class("ean13")
    elif bc_type == "ean8":
        bc_class = bc.get_barcode_class("ean8")
    else:
        bc_class = bc.get_barcode_class("code128")

    writer = ImageWriter()
    writer.set_options(
        {
            "module_width": 10.0,
            "module_height": 15.0,
            "quiet_zone": 6.5,
            "font_size": 10,
            "text_distance": 5.0,
            "background": "white",
            "foreground": "black",
            "write_text": True,
            "dpi": 300,
        }
    )

    buf = io.BytesIO()
    try:
        code = bc_class(value, writer=writer)
        code.write(buf)
    except Exception as e:
        raise ValueError(f"Cannot generate barcode for '{value}': {e}")

    return buf.getvalue()


def generate_barcode_for_offer(
    offer_id: str,
    barcodes: list[str] | None = None,
) -> tuple[bytes, str]:
    """Generate the best available barcode for a product.

    Tries in order: EAN-13 from barcodes list, Code-128 from offer_id.
    Returns (png_bytes, display_value).
    """
    if barcodes:
        for b in barcodes:
            cleaned = b.strip().replace(" ", "")
            if len(cleaned) == 13 and cleaned.isdigit():
                try:
                    return generate_barcode_png(cleaned, "ean13"), cleaned
                except Exception:
                    pass
            if len(cleaned) == 8 and cleaned.isdigit():
                try:
                    return generate_barcode_png(cleaned, "ean8"), cleaned
                except Exception:
                    pass

    # Fallback: Code-128 with offer_id
    safe_offer = offer_id.replace(" ", "-").upper()
    return generate_barcode_png(safe_offer, "code128"), safe_offer


def get_barcode_for_sku(sku: str, offer_id: str) -> tuple[bytes, str]:
    """Generate barcode PNG for a SKU. offer_id used as Code128 value if no EAN."""
    return generate_barcode_for_offer(offer_id, barcodes=None)
