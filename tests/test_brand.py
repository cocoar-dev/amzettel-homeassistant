"""Ship usable local branding, including Home Assistant's dark-mode fallback."""

import struct
from pathlib import Path

import pytest
from homeassistant.setup import async_setup_component

BRAND = Path(__file__).resolve().parents[1] / "custom_components" / "amzettel" / "brand"


@pytest.mark.parametrize("name", ["icon.png", "icon@2x.png", "logo.png", "logo@2x.png"])
def test_brand_images_have_the_expected_size(name):
    data = (BRAND / name).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert data[12:16] == b"IHDR"
    expected = 512 if "@2x" in name else 256
    assert struct.unpack(">II", data[16:24]) == (expected, expected)


@pytest.mark.parametrize(
    ("requested", "local"),
    [
        ("icon.png", "icon.png"),
        ("logo.png", "logo.png"),
        ("icon@2x.png", "icon@2x.png"),
        ("logo@2x.png", "logo@2x.png"),
        ("dark_icon.png", "icon.png"),
        ("dark_logo.png", "logo.png"),
        ("dark_icon@2x.png", "icon@2x.png"),
        ("dark_logo@2x.png", "logo@2x.png"),
    ],
)
async def test_home_assistant_serves_local_brand_images(hass, hass_client, requested, local):
    assert await async_setup_component(hass, "brands", {})
    client = await hass_client()
    response = await client.get(f"/api/brands/integration/amzettel/{requested}?placeholder=no")
    assert response.status == 200
    assert response.content_type == "image/png"
    assert await response.read() == (BRAND / local).read_bytes()
