"""Relay helpers — the /api/relay URL format and the HLS manifest rewrite.

googlevideo URLs are IP-bound to this network and carry no CORS headers,
so the Device and remote browsers fetch them through /api/relay on this box.
The route itself still lives in app.py.
"""
import re
from urllib.parse import urljoin, quote


def parse_byte_range(header):
    """'bytes=A-B' / 'bytes=A-' -> (A, B or None). Anything else (suffix,
    multipart) -> None; the relay then serves the whole resource."""
    m = re.fullmatch(r"bytes=(\d+)-(\d*)", (header or "").strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)) if m.group(2) else None


def relay_url(upstream_url, host=None):
    """Local /api/relay URL serving an upstream URL from this box. Absolute
    (with host) for the Device; relative for the Preview, which resolves it
    against whatever origin the page was loaded from."""
    prefix = f"http://{host}" if host else ""
    return f"{prefix}/api/relay?u={quote(upstream_url, safe='')}"


def rewrite_hls_manifest(text, base_url, proxy_url):
    """Route every URI in an m3u8 playlist through proxy_url(absolute_url).
    Covers segment/variant lines and URI="..." attributes (EXT-X-MAP,
    EXT-X-KEY); relative URIs are resolved against the manifest's URL."""
    def sub_uri_attr(match):
        return f'URI="{proxy_url(urljoin(base_url, match.group(1)))}"'

    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
        elif stripped.startswith("#"):
            out.append(re.sub(r'URI="([^"]+)"', sub_uri_attr, line))
        else:
            out.append(proxy_url(urljoin(base_url, stripped)))
    return "\n".join(out) + "\n"
