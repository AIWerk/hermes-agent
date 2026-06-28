"""SSRF regression tests for the CUI auto-read URL allowlist.

``_is_public_http_url`` gates the managed-autonomy auto-approve fast paths
(``_is_safe_curl_read`` and ``_is_low_risk_cui_execute_code``). Before the fix
it only rejected a handful of literal hosts and a few RFC1918 prefixes, so the
cloud-metadata endpoint, the full loopback range, IPv4-mapped IPv6 loopback,
internal-resolving hostnames, and octal/decimal-encoded IP literals were all
treated as public and auto-approved with no prompt.

These tests pin the secure behavior: ALLOW only globally-routable resolved
addresses; reject every IANA special-use range and every non-decimal-dotted IP
literal. Hostname resolution is mocked so the suite is deterministic and offline.
"""

import socket
from unittest.mock import patch as mock_patch

from tools.approval import (
    _is_low_risk_cui_execute_code,
    _is_public_http_url,
    _is_safe_curl_read,
)


def _addrinfo(*addresses):
    """Build a getaddrinfo-shaped return for the given IP strings."""
    return [
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", (addr, 0))
        for addr in addresses
    ]


class TestIsPublicHttpUrlRejectsSsrfVectors:
    def test_cloud_metadata_ip_rejected(self):
        # AWS/GCP/Azure link-local metadata endpoint.
        assert _is_public_http_url(
            "http://169.254.169.254/latest/meta-data/iam/security-credentials/"
        ) is False

    def test_loopback_range_beyond_literal_rejected(self):
        # 127.0.0.0/8 — not just the bare 127.0.0.1 the old code blocked.
        assert _is_public_http_url("http://127.0.0.2/admin") is False

    def test_ipv4_mapped_ipv6_loopback_rejected(self):
        assert _is_public_http_url("http://[::ffff:127.0.0.1]/") is False

    def test_octal_encoded_ip_literal_rejected(self):
        # 0177.0.0.1 == 127.0.0.1 to the C resolver; must not reach DNS.
        assert _is_public_http_url("http://0177.0.0.1/") is False

    def test_decimal_encoded_ip_literal_rejected(self):
        # 2130706433 == 127.0.0.1 in 32-bit decimal form.
        assert _is_public_http_url("http://2130706433/") is False

    def test_hex_encoded_ip_literal_rejected(self):
        assert _is_public_http_url("http://0x7f.0.0.1/") is False

    def test_metadata_google_internal_hostname_rejected(self):
        # Resolves to the link-local metadata IP in a real GCE environment.
        with mock_patch(
            "tools.approval.socket.getaddrinfo",
            return_value=_addrinfo("169.254.169.254"),
        ):
            assert _is_public_http_url(
                "http://metadata.google.internal/computeMetadata/v1/"
            ) is False

    def test_internal_resolving_hostname_rejected(self):
        with mock_patch(
            "tools.approval.socket.getaddrinfo",
            return_value=_addrinfo("10.0.0.5"),
        ):
            assert _is_public_http_url("http://intranet.corp/") is False

    def test_dns_rebinding_mixed_addresses_rejected(self):
        # One public + one private address must still be rejected (all-or-reject).
        with mock_patch(
            "tools.approval.socket.getaddrinfo",
            return_value=_addrinfo("93.184.216.34", "127.0.0.1"),
        ):
            assert _is_public_http_url("http://rebind.example/") is False

    def test_resolution_failure_fails_closed(self):
        with mock_patch(
            "tools.approval.socket.getaddrinfo",
            side_effect=socket.gaierror("name or service not known"),
        ):
            assert _is_public_http_url("http://does-not-resolve.invalid/") is False

    def test_rfc1918_and_cgnat_literals_rejected(self):
        for host in ("10.0.0.1", "192.168.1.1", "172.16.0.1", "100.64.0.1"):
            assert _is_public_http_url(f"http://{host}/") is False

    def test_link_local_and_unique_local_ipv6_rejected(self):
        for host in ("[fe80::1]", "[fc00::1]", "[::1]"):
            assert _is_public_http_url(f"http://{host}/") is False

    def test_non_http_scheme_rejected(self):
        assert _is_public_http_url("file:///etc/passwd") is False
        assert _is_public_http_url("gopher://127.0.0.1/") is False


class TestIsPublicHttpUrlAllowsPublic:
    def test_public_ip_literal_allowed(self):
        assert _is_public_http_url("http://93.184.216.34/") is True

    def test_public_hostname_allowed(self):
        with mock_patch(
            "tools.approval.socket.getaddrinfo",
            return_value=_addrinfo("93.184.216.34"),
        ):
            assert _is_public_http_url("https://example.com/path") is True

    def test_public_ipv6_literal_allowed(self):
        assert _is_public_http_url("http://[2606:2800:220:1:248:1893:25c8:1946]/") is True


class TestSafeCurlReadHonorsSsrfFix:
    def test_curl_to_metadata_endpoint_not_safe(self):
        assert _is_safe_curl_read(
            "curl http://169.254.169.254/latest/meta-data/"
        ) is False

    def test_curl_to_octal_loopback_not_safe(self):
        assert _is_safe_curl_read("curl http://0177.0.0.1/") is False

    def test_curl_to_public_host_still_safe(self):
        with mock_patch(
            "tools.approval.socket.getaddrinfo",
            return_value=_addrinfo("93.184.216.34"),
        ):
            assert _is_safe_curl_read("curl https://example.com/data.json") is True


class TestLowRiskExecuteCodeHonorsSsrfFix:
    def test_metadata_fetch_not_low_risk(self):
        code = (
            "from hermes_tools import terminal\n"
            "terminal(\"curl http://169.254.169.254/latest/meta-data/\")\n"
        )
        assert _is_low_risk_cui_execute_code(code) is False

    def test_decimal_loopback_fetch_not_low_risk(self):
        code = "import urllib.request as u\nu.urlopen('http://2130706433/')\n"
        assert _is_low_risk_cui_execute_code(code) is False

    def test_urlretrieve_treated_as_write_not_read(self):
        # urlretrieve downloads straight to a local path — a disk write, not a
        # read-only fetch. Must never take the low-risk fast path even for a
        # public URL.
        code = (
            "import urllib.request\n"
            "urllib.request.urlretrieve('https://example.com/a', '/tmp/a')\n"
        )
        with mock_patch(
            "tools.approval.socket.getaddrinfo",
            return_value=_addrinfo("93.184.216.34"),
        ):
            assert _is_low_risk_cui_execute_code(code) is False
