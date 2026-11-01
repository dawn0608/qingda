import io

import pytest
from fastapi import HTTPException
from PIL import Image
from wechat_manager.profile import avatar_image


def encoded(image, format="PNG", **options):
    output = io.BytesIO()
    image.save(output, format, **options)
    return output.getvalue()


@pytest.mark.parametrize("format", ["PNG", "JPEG", "WEBP", "GIF"])
def test_avatar_supported_formats_preserve_small_source_resolution(format):
    result = Image.open(io.BytesIO(avatar_image(encoded(Image.new("RGB", (80, 64), "red"), format))))
    assert result.format == "JPEG"
    assert result.size == (64, 64)


def test_large_avatar_keeps_high_resolution_without_exceeding_limit():
    raw = encoded(Image.new("RGB", (2000, 1600), "green"))
    result = Image.open(io.BytesIO(avatar_image(raw)))
    assert result.size == (1024, 1024)


def test_avatar_preserves_fine_color_detail():
    image = Image.new("RGB", (64, 64))
    image.putdata([(255, 0, 0) if x % 2 == 0 else (0, 0, 255) for y in range(64) for x in range(64)])
    result = Image.open(io.BytesIO(avatar_image(encoded(image))))
    red, blue = result.getpixel((10, 10)), result.getpixel((11, 10))
    assert red[0] > 230 and red[2] < 40
    assert blue[2] > 230 and blue[0] < 40


def test_avatar_applies_exif_orientation_before_center_crop():
    image = Image.new("RGB", (240, 160))
    image.paste("red", (0, 0, 120, 80))
    image.paste("green", (120, 0, 240, 80))
    image.paste("blue", (0, 80, 120, 160))
    image.paste("yellow", (120, 80, 240, 160))
    exif = image.getexif()
    exif[274] = 6
    result = Image.open(io.BytesIO(avatar_image(encoded(image, "JPEG", exif=exif, quality=95))))
    assert result.size == (160, 160)
    assert result.getpixel((10, 10))[2] > 230
    assert result.getpixel((149, 10))[0] > 230
    assert not result.getexif().get(274)


def test_animated_avatar_uses_first_frame():
    raw = encoded(
        Image.new("RGB", (64, 64), "red"),
        "GIF",
        save_all=True,
        append_images=[Image.new("RGB", (64, 64), "blue")],
    )
    result = Image.open(io.BytesIO(avatar_image(raw)))
    assert result.getpixel((32, 32))[0] > 230


@pytest.mark.parametrize("raw", [b"invalid image", encoded(Image.new("RGB", (8, 8)), "TIFF")])
def test_avatar_rejects_invalid_and_unsupported_inputs(raw):
    with pytest.raises(HTTPException) as error:
        avatar_image(raw)
    assert error.value.status_code == 422


def test_avatar_retains_input_pixel_limit():
    with pytest.raises(HTTPException) as error:
        avatar_image(encoded(Image.new("RGB", (4001, 4000))))
    assert error.value.status_code == 422
