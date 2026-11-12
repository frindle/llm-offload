"""Guard of the docsum web UI: no client/tunnel/Host rejection; POSTs must be same-origin."""
import pytest

from docsum_ui_guard import guard, same_origin

H = {"Host": "192.0.2.131:7690", "Origin": "http://192.0.2.131:7690"}


@pytest.mark.parametrize("ip", ["127.0.0.1", "8.8.8.8", "2606:4700::1111", "", None])
def test_any_client_accepted(ip):
    assert guard(ip, H, "GET") is None
    assert guard(ip, H, "POST") is None


def test_tunnel_proxy_headers_and_public_host_accepted():
    h = {"Host": "queue.example.com", "Origin": "https://queue.example.com",
         "CF-Connecting-IP": "1.2.3.4", "Cf-Access-Jwt-Assertion": "x", "X-Forwarded-For": "8.8.8.8", "Via": "1.1 x"}
    assert guard("127.0.0.1", h, "GET") is None
    assert guard("127.0.0.1", h, "POST") is None


def test_cross_origin_and_missing_origin_post_rejected_get_unaffected():
    assert guard("127.0.0.1", dict(H, Origin="http://evil.example"), "POST") == (403, "E_CROSS_ORIGIN")
    assert guard("127.0.0.1", {"Host": H["Host"]}, "POST") == (403, "E_CROSS_ORIGIN")
    assert guard("127.0.0.1", dict(H, Origin="http://evil.example"), "GET") is None
    assert same_origin(H) and not same_origin({"Host": "a", "Origin": "http://b"})
