"""Image normalization shared by multimodal LLM call sites."""

from __future__ import annotations

from io import BytesIO


def normalize_image_for_llm(data: bytes, mime: str) -> tuple[bytes, str]:
    """Convert GIF input to a static PNG accepted by stricter VLM gateways.

    MIME metadata from upstream is not always reliable, so GIF magic bytes are
    checked as well. Animated GIFs use their first frame: the LLM request format
    carries one image block here, and PNG does not preserve GIF animation.
    """
    normalized_mime = mime.split(";", 1)[0].strip().lower() or "image/png"
    is_gif = normalized_mime == "image/gif" or data.startswith(
        (b"GIF87a", b"GIF89a")
    )
    if not is_gif:
        return data, normalized_mime

    from PIL import Image

    with Image.open(BytesIO(data)) as source:
        source.seek(0)
        frame = source.convert("RGBA")
        output = BytesIO()
        frame.save(output, format="PNG")
        return output.getvalue(), "image/png"
