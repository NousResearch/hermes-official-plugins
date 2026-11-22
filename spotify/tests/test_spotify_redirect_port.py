"""A malformed or out-of-range loopback port is a shaped redirect error, not a ValueError."""
import pytest

from hermes_cli.auth_constants import AuthError
from spotify_plugin import spotify_auth


@pytest.mark.parametrize("uri", ["http://127.0.0.1:notaport/cb", "http://127.0.0.1:99999/cb"])
def test_malformed_port_is_a_shaped_auth_error(uri):
    with pytest.raises(AuthError) as exc:
        spotify_auth._validate_redirect_uri(uri)
    assert exc.value.code == "spotify_redirect_invalid"
