"""Authenticated peer requests, without redirects or ambient HTTP proxies."""
import ipaddress
import re
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

TOKEN_RE = re.compile(r'[A-Za-z0-9_-]{32,128}\Z')


def validate_peer_url(url):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path not in ('', '/')
                or '\\' in url or any(ord(c) <= 32 for c in url)):
            raise ValueError('Peer URL must be an HTTP(S) origin without credentials or a path')
        parsed.port  # reject invalid or out-of-range ports
        try:
            address = ipaddress.ip_address(parsed.hostname)
            allowed = address.is_private or address in ipaddress.ip_network('100.64.0.0/10')
            if address.is_unspecified or address.is_multicast or not allowed:
                raise ValueError('Peer address must be private or Tailscale')
        except ValueError as exc:
            if not parsed.hostname.endswith('.ts.net'):
                raise ValueError('Use a private IP address or a Tailscale .ts.net name') from exc
        return url.rstrip('/')
    except (ValueError, TypeError) as exc:
        raise ValueError('Invalid peer origin; use a private IP or Tailscale HTTPS name') from exc


class NoPeerRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def open_peer(request, token, *, timeout):
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        raise ValueError('Peer authentication is not configured; add its owner token')
    request.add_header('Authorization', 'Bearer ' + token)
    # Authentication credentials and request contents must never follow redirects.
    return build_opener(ProxyHandler({}), NoPeerRedirect()).open(request, timeout=timeout)
