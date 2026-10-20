"""Image/sticker bytes only: no credentials, redirects, proxies or private addresses."""
import http.client
import ipaddress
import socket
import ssl
import time
import re
from urllib.parse import urlsplit
from .security import CollectorError

MAX_BYTES = 20 * 1024 * 1024
SUFFIXES = ('byteimg.com', 'douyinpic.com', 'douyinstatic.com')


def destination(url):
    if not isinstance(url, str) or len(url) > 8192:
        raise CollectorError('MEDIA_URL_REJECTED')
    try:
        u = urlsplit(url)
        host = u.hostname or ''
        if (u.scheme != 'https' or u.port not in (None, 443) or u.username is not None
                or u.password is not None or u.fragment or '\\' in url
                or any(ord(c) < 33 or ord(c) > 126 for c in url)
                or not any(host == x or host.endswith('.' + x) for x in SUFFIXES)):
            raise ValueError()
    except ValueError:
        raise CollectorError('MEDIA_URL_REJECTED') from None
    return host, (u.path or '/') + ('?' + u.query if u.query else '')


def image_type(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'): return 'image/png'
    if data.startswith(b'\xff\xd8\xff'): return 'image/jpeg'
    if data[:6] in (b'GIF87a', b'GIF89a'): return 'image/gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP': return 'image/webp'
    # AVIF is an image container; generic MP4/HEIF/video brands are not accepted.
    if data[4:8] == b'ftyp' and data[8:12] in (b'avif', b'avis'): return 'image/avif'
    raise CollectorError('MEDIA_NOT_IMAGE')


def download_image(url, source_key=None):
    if source_key is not None and (not isinstance(source_key,str) or not re.fullmatch(r'[0-9a-fA-F]{64}',source_key)):
        raise CollectorError('MEDIA_SOURCE_KEY_INVALID')
    host, path = destination(url)
    conn = None
    started = time.monotonic()
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise CollectorError('MEDIA_ADDRESS_REJECTED')
        family, typ, proto, _, address = addresses[0]
        sock = socket.socket(family, typ, proto)
        try:
            sock.settimeout(12)
            sock.connect(address)  # Pin exactly the validated address; no second DNS lookup.
            if not ipaddress.ip_address(sock.getpeername()[0]).is_global:
                raise CollectorError('MEDIA_ADDRESS_REJECTED')
            tls = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        except BaseException:
            sock.close()
            raise
        conn = http.client.HTTPSConnection(host, timeout=12)
        conn.sock = tls
        conn.request('GET', path, headers={'Accept': 'image/*', 'Accept-Encoding': 'identity'})
        response = conn.getresponse()
        if response.status in (401, 403): raise CollectorError('MEDIA_ACCESS_DENIED')
        if response.status == 429: raise CollectorError('MEDIA_RATE_LIMIT')
        if 300 <= response.status < 400: raise CollectorError('MEDIA_REDIRECT_REJECTED')
        if response.status != 200: raise CollectorError('MEDIA_HTTP_FAILED')
        if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
            raise CollectorError('MEDIA_ENCODING_REJECTED')
        claimed = response.getheader('Content-Type', '').split(';')[0].strip().lower()
        if source_key is None and claimed not in {'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/avif', 'application/octet-stream'}:
            raise CollectorError('MEDIA_NOT_IMAGE')
        max_wire_bytes = MAX_BYTES + (28 if source_key is not None else 0)
        length = response.getheader('Content-Length')
        if length and (not length.isdigit() or int(length) > max_wire_bytes):
            raise CollectorError('MEDIA_SIZE_LIMIT')
        data = bytearray()
        while True:
            if time.monotonic() - started > 30: raise CollectorError('MEDIA_TIMEOUT')
            chunk = response.read(min(65536, max_wire_bytes + 1 - len(data)))
            if not chunk: break
            data.extend(chunk)
            if len(data) > max_wire_bytes: raise CollectorError('MEDIA_SIZE_LIMIT')
        if length and len(data) != int(length): raise CollectorError('MEDIA_TRUNCATED')
        if source_key is not None:
            from .chat_cipher import decrypt_source_image
            data = decrypt_source_image(bytes(data), source_key)
        mime = image_type(data)
        if source_key is None and claimed != 'application/octet-stream' and claimed != mime:
            raise CollectorError('MEDIA_TYPE_MISMATCH')
        return bytes(data), mime
    except CollectorError:
        raise
    except (TimeoutError, socket.timeout):
        raise CollectorError('MEDIA_TIMEOUT') from None
    except Exception:
        raise CollectorError('MEDIA_DOWNLOAD_FAILED') from None
    finally:
        if conn: conn.close()
