"""Photo validation and preparation."""

import io

import pytest
from PIL import Image

from app.imaging import ImageError, prepare_image
from factories import shelf_image


def test_sharp_photo_has_no_warnings():
    prepared = prepare_image(shelf_image())
    assert prepared.warnings == []
    assert prepared.mime == "image/jpeg" and prepared.sharpness > 1.8


def test_blurry_photo_gets_a_warning():
    prepared = prepare_image(shelf_image(blur=6))
    assert prepared.sharpness < 1.8
    assert any("blurry" in w for w in prepared.warnings)


def test_small_photo_gets_a_low_resolution_warning():
    prepared = prepare_image(shelf_image(size=(300, 200)))
    assert any("Low resolution" in w for w in prepared.warnings)


def test_large_photo_is_shrunk_and_keeps_its_shape():
    prepared = prepare_image(shelf_image(size=(3000, 2000)), max_side=1600)
    assert max(prepared.width, prepared.height) == 1600
    assert (prepared.original_width, prepared.original_height) == (3000, 2000)
    assert abs(prepared.width / prepared.height - 1.5) < 0.01  # boxes stay valid


def test_phone_rotation_metadata_is_applied():
    # Orientation 6 = the phone was held sideways: the pixels must be turned upright.
    prepared = prepare_image(shelf_image(size=(900, 600), orientation=6))
    assert (prepared.width, prepared.height) == (600, 900)


def test_png_with_transparency_is_accepted():
    img = Image.new("RGBA", (800, 600), (255, 0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    assert prepare_image(buf.getvalue()).mime == "image/jpeg"


def test_webp_is_accepted():
    assert prepare_image(shelf_image(fmt="WEBP")).width == 900


@pytest.mark.parametrize(
    "data, fragment",
    [
        (b"", "empty"),
        (b"hello, I am not an image", "could not be read"),
        (shelf_image(fmt="GIF"), "not supported"),
        (shelf_image(fmt="BMP"), "not supported"),
        (shelf_image()[:200], "could not be read"),  # not even a complete header
        (shelf_image()[: len(shelf_image()) // 2], "damaged"),  # a JPEG cut off part-way
    ],
)
def test_unusable_files_are_rejected_with_a_readable_message(data, fragment):
    with pytest.raises(ImageError) as exc:
        prepare_image(data)
    assert fragment in str(exc.value)
