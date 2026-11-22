"""A malformed image_url value must be rejected by the plugin, not raise a raw TypeError."""
import pytest

from directsdk import RequestRejected, content_blocks


@pytest.mark.parametrize("url", [{"href": "x"}, ["data:image/png;base64,AA=="], 7])
def test_non_string_image_url_is_rejected(url):
    with pytest.raises(RequestRejected, match="base64 data image_url"):
        content_blocks([{"type": "image_url", "image_url": {"url": url}}])


def test_valid_data_url_still_maps_to_an_image():
    out = content_blocks([{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}])
    assert out == [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}}]
