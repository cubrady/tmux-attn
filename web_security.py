"""Local web authentication and explicit browser request boundaries.

No credentials are loaded or created at import time. The web daemon owns one
instance; peer requests use its owner token as an Authorization bearer token.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import hmac
from http.cookies import SimpleCookie, CookieError
import ipaddress
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
from urllib.parse import urlsplit


COOKIE_NAME = 'tmux_attn_session'
_TOKEN_RE = re.compile(r'[A-Za-z0-9_-]{32,128}\Z')
_COOKIE_RE = re.compile(r'[A-Za-z0-9_-]{43}\Z')


def load_owner_token(path):
    """Publish a complete private key atomically, or safely read an existing key."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not os.path.lexists(path):
        temporary = path.with_name('.' + path.name + '.' + secrets.token_hex(12))
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, 'w') as stream:
                stream.write(secrets.token_urlsafe(32) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # No replacement: concurrent initializers all read the winner.
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 256):
            raise ValueError('Authentication key must be a private, owner-owned regular file')
        try:
            token = stream.read(257).decode('ascii').strip()
        except UnicodeError as exc:
            raise ValueError('Invalid authentication key file') from exc
    if not _TOKEN_RE.fullmatch(token):
        raise ValueError('Invalid authentication key file')
    return token


def _origin(value):
    """Return a canonical scheme/host/port tuple; reject URL extras and wildcards."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError('Invalid trusted origin')
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment
                or '*' in parsed.netloc or '\\' in parsed.netloc
                or any(ord(char) < 33 or ord(char) > 126 for char in parsed.netloc)):
            raise ValueError('Invalid trusted origin')
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == 'https' else 80)
        if port < 1:
            raise ValueError('Invalid trusted origin')
        return parsed.scheme, parsed.hostname.lower(), port
    except ValueError as exc:
        raise ValueError('Invalid trusted origin') from exc


def _authority(value):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError('Invalid Host')
    if any(char in value for char in '/\\?#,@') or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise ValueError('Invalid Host')
    try:
        parsed = urlsplit('//' + value)
        if not parsed.hostname or parsed.username is not None:
            raise ValueError('Invalid Host')
        return parsed.hostname.lower(), parsed.port
    except ValueError as exc:
        raise ValueError('Invalid Host') from exc


def _header(headers, name):
    """Ambiguous duplicate authority/auth headers are never accepted."""
    if hasattr(headers, 'get_all'):
        values = headers.get_all(name, [])
        return values[0] if len(values) == 1 else None
    return headers.get(name)


class WebSecurity:
    def __init__(self, key_path, bind='127.0.0.1', port=7788, allowed_origins=(),
                 session_ttl=86400, max_sessions=128, clock=time.monotonic):
        if session_ttl <= 0 or max_sessions < 1:
            raise ValueError('Invalid session limits')
        self.token = load_owner_token(key_path)
        self.bind, self.port = bind, int(port)
        self.allowed_origins = frozenset(_origin(value) for value in allowed_origins)
        self.session_ttl, self.max_sessions, self.clock = session_ttl, max_sessions, clock
        self.sessions = OrderedDict()
        self.lock = threading.Lock()

    @classmethod
    def from_environment(cls, bind='127.0.0.1', port=7788):
        configured = os.environ.get('TMUX_ATTN_AUTH_KEY_FILE')
        key_path = Path(configured).expanduser() if configured else Path.home() / '.config/tmux-attn/auth.key'
        if not key_path.is_absolute():
            raise ValueError('TMUX_ATTN_AUTH_KEY_FILE must be an absolute path')
        origins = tuple(value.strip() for value in os.environ.get('TMUX_ATTN_ALLOWED_ORIGINS', '').split(',') if value.strip())
        return cls(key_path, bind=bind, port=port, allowed_origins=origins)

    def _origins(self, server_port=None):
        port = self.port if server_port is None else int(server_port)
        origins = {('http', host, port) for host in ('127.0.0.1', 'localhost', '::1')}
        try:
            address = ipaddress.ip_address(self.bind)
            if not address.is_unspecified:
                origins.add(('http', str(address), port))
        except ValueError:
            # A named interface must be explicitly configured as an origin.
            pass
        return origins | self.allowed_origins

    def _request_origin(self, headers, server_port=None, required=False):
        try:
            hostname, host_port = _authority(_header(headers, 'Host'))
        except ValueError:
            return None, 'Untrusted Host'
        origins = self._origins(server_port)
        candidates = {origin for origin in origins if origin[1] == hostname
                      and (origin[2] == host_port if host_port is not None else origin[2] in (80, 443))}
        if not candidates:
            return None, 'Untrusted Host'
        raw = _header(headers, 'Origin')
        # Duplicate Origin is ambiguous even on a machine request.
        if hasattr(headers, 'get_all') and len(headers.get_all('Origin', [])) > 1:
            return None, 'Untrusted Origin'
        if raw is None:
            return (next(iter(candidates)), None) if not required else (None, 'Origin is required')
        try:
            origin = _origin(raw)
        except ValueError:
            return None, 'Untrusted Origin'
        return (origin, None) if origin in candidates else (None, 'Untrusted Origin')

    def _bearer(self, headers):
        raw = _header(headers, 'Authorization')
        if not isinstance(raw, str):
            return False
        scheme, separator, value = raw.partition(' ')
        return bool(separator and scheme.lower() == 'bearer' and value.isascii()
                    and hmac.compare_digest(value, self.token))

    @staticmethod
    def _session_key(headers):
        raw = _header(headers, 'Cookie')
        if not isinstance(raw, str) or len(raw) > 8192:
            return None
        try:
            cookie = SimpleCookie(raw)
            value = cookie[COOKIE_NAME].value if COOKIE_NAME in cookie else ''
        except CookieError:
            return None
        return hashlib.sha256(value.encode()).digest() if _COOKIE_RE.fullmatch(value) else None

    def _prune_sessions(self, now):
        for key, expires in list(self.sessions.items()):
            if expires <= now:
                del self.sessions[key]

    def authenticated(self, headers):
        if self._bearer(headers):
            return True
        key = self._session_key(headers)
        if key is None:
            return False
        with self.lock:
            self._prune_sessions(self.clock())
            return key in self.sessions

    def authorize(self, method, path, headers, server_port=None):
        """Return None to allow, otherwise (HTTP status, JSON-safe error body)."""
        method = method.upper()
        mutation = method not in ('GET', 'HEAD', 'OPTIONS')
        bearer = self._bearer(headers)
        _origin_value, error = self._request_origin(headers, server_port, required=mutation and not bearer)
        if error:
            return 403, {'ok': False, 'error': error}
        if mutation:
            media_type = _header(headers, 'Content-Type')
            if not isinstance(media_type, str) or media_type.split(';', 1)[0].strip().lower() != 'application/json':
                return 415, {'ok': False, 'error': 'Content-Type must be application/json'}
            if not bearer and _header(headers, 'X-Tmux-Attn-Request') != '1':
                return 403, {'ok': False, 'error': 'Same-origin request header is required'}
        public = ((method == 'GET' and path in ('/api/health', '/api/auth'))
                  or (method == 'POST' and path == '/api/auth/session'))
        if path.startswith('/api/') and not public and not self.authenticated(headers):
            return 401, {'ok': False, 'error': 'Authentication required'}
        return None

    def login(self, token, headers, server_port=None):
        denied = self.authorize('POST', '/api/auth/session', headers, server_port)
        if denied:
            return *denied, {}
        if not isinstance(token, str) or not token.isascii() or not hmac.compare_digest(token, self.token):
            return 401, {'ok': False, 'error': 'Invalid access key'}, {}
        origin, _error = self._request_origin(headers, server_port, required=True)
        # Login is a browser operation even if Authorization was supplied.
        if origin is None:
            return 403, {'ok': False, 'error': 'Origin is required'}, {}
        value = secrets.token_urlsafe(32)
        with self.lock:
            self._prune_sessions(self.clock())
            while len(self.sessions) >= self.max_sessions:
                self.sessions.popitem(last=False)
            self.sessions[hashlib.sha256(value.encode()).digest()] = self.clock() + self.session_ttl
        secure = '; Secure' if origin[0] == 'https' else ''
        return 200, {'ok': True, 'authenticated': True}, {
            'Set-Cookie': f'{COOKIE_NAME}={value}; HttpOnly; SameSite=Strict; Path=/; Max-Age={int(self.session_ttl)}{secure}',
        }

    def logout(self, headers):
        key = self._session_key(headers)
        with self.lock:
            self.sessions.pop(key, None)
        return 200, {'ok': True, 'authenticated': False}, {
            'Set-Cookie': f'{COOKIE_NAME}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0',
        }

    def session_status(self, headers):
        return {'ok': True, 'authenticated': self.authenticated(headers)}


AUTH_CLIENT_JS = r'''(() => {
  const original = window.fetch.bind(window);
  let redirecting = false;
  window.fetch = async (input, init = {}) => {
    const url = new URL(typeof input === 'string' ? input : (input.url || String(input)), window.location.href);
    const sameOrigin = url.origin === window.location.origin;
    const method = (init.method || (typeof input === 'object' && input.method) || 'GET').toUpperCase();
    if (sameOrigin && !['GET', 'HEAD', 'OPTIONS'].includes(method)) {
      const headers = new Headers(init.headers || (typeof input === 'object' && input.headers) || {});
      headers.set('X-Tmux-Attn-Request', '1');
      init = {...init, headers};
    }
    const response = await original(input, init);
    if (sameOrigin && response.status === 401 && !redirecting) {
      redirecting = true;
      window.location.reload();
    }
    return response;
  };
})();'''


def login_html():
    return r'''<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>tmux Attention · 登入</title>
<style>body{font:16px system-ui;max-width:26rem;margin:15vh auto;padding:1.5rem;color:#172b2a;background:#f7faf9}
label,input,button{display:block;box-sizing:border-box;width:100%}input,button{font:inherit;padding:.8rem;margin:.6rem 0 1rem;border-radius:.6rem;border:1px solid #8da5a0}button{background:#0f766e;color:white;cursor:pointer}p{line-height:1.6}#error{color:#a12828}</style></head>
<body><h1>tmux Attention</h1><p>請輸入這台主機的存取金鑰。登入後可查看與操作 session。</p>
<form id="login"><label for="key">存取金鑰</label><input id="key" type="password" autocomplete="current-password" required autofocus>
<button type="submit">登入</button><p id="error" role="alert"></p></form>
<script>document.getElementById('login').addEventListener('submit',async event=>{
event.preventDefault();const key=document.getElementById('key'),button=event.currentTarget.querySelector('button'),error=document.getElementById('error');
button.disabled=true;error.textContent='';try{const response=await fetch('/api/auth/session',{method:'POST',headers:{'Content-Type':'application/json','X-Tmux-Attn-Request':'1'},credentials:'same-origin',cache:'no-store',body:JSON.stringify({token:key.value})});
if(!response.ok){error.textContent=response.status===401?'金鑰不正確，請重試。':'無法登入，請確認連線網址與服務設定。';return;}key.value='';window.location.reload();
}catch(_){error.textContent='連線失敗，請稍後重試。';}finally{button.disabled=false;}});</script></body></html>'''
