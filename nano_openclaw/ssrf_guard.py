"""SSRF protection for web tools.

Mirrors openclaw's src/infra/net/ssrf.ts — two-phase hostname checking:
1. Pre-DNS: block literal private IPs and known-bad hostnames
2. Post-DNS: verify resolved IPs are not private/internal
"""

import socket
import ipaddress
from urllib.parse import urlparse


_BLOCKED_HOSTNAMES = {"localhost", "metadata.google.internal"}

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]


class SsrfBlockedError(Exception):
    """Raised when a URL is blocked due to SSRF policy."""
    pass


def _is_private_ip(ip_str: str) -> bool:
    """Check if an IP address is private/internal/special-use."""
    try:
        addr = ipaddress.ip_address(ip_str)
        return any(addr in net for net in _PRIVATE_NETWORKS)
    except ValueError:
        return True  # fail closed on parse errors


def _is_blocked_hostname(hostname: str) -> bool:
    """Check if a hostname is in the blocked list."""
    h = hostname.lower()
    if h in _BLOCKED_HOSTNAMES:
        return True
    return (
        h.endswith(".localhost")
        or h.endswith(".local")
        or h.endswith(".internal")
    )


def assert_public_url(url: str) -> str:
    """Assert that a URL points to a public, non-private address.
    
    Two-phase check (mirrors openclaw ssrf.ts):
    1. Pre-DNS: block literal private IPs and known-bad hostnames
    2. Post-DNS: resolve hostname and verify all IPs are public
    
    Args:
        url: Full URL string
        
    Returns:
        The original URL if allowed
        
    Raises:
        ValueError: If URL is malformed or scheme is invalid
        SsrfBlockedError: If URL points to private/internal address
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Invalid URL scheme: {parsed.scheme} (must be http or https)")
    
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Invalid URL: no hostname")
    
    # Phase 1: pre-DNS checks
    if _is_blocked_hostname(hostname):
        raise SsrfBlockedError(f"Blocked hostname: {hostname}")
    
    # Check if hostname is a literal private IP (skip domain names)
    try:
        ipaddress.ip_address(hostname)
        if _is_private_ip(hostname):
            raise SsrfBlockedError(f"Blocked private IP: {hostname}")
    except ValueError:
        pass  # Not an IP literal, it's a hostname — DNS check handles it
    
    # Phase 2: DNS resolution + post-DNS check
    try:
        results = socket.getaddrinfo(hostname, None)
        for _, _, _, _, sockaddr in results:
            ip_str = sockaddr[0]
            if _is_private_ip(ip_str):
                raise SsrfBlockedError(
                    f"Blocked: {hostname} resolves to private IP {ip_str}"
                )
    except SsrfBlockedError:
        raise
    except socket.gaierror as e:
        raise ValueError(f"Unable to resolve hostname: {hostname}: {e}")
    
    return url
