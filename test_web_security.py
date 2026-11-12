from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from web_security import AUTH_CLIENT_JS, COOKIE_NAME, WebSecurity, load_owner_token, login_html


class WebSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'config/auth.key'
        self.now = 100.0
        self.security = WebSecurity(self.path, clock=lambda: self.now)
        self.host = '127.0.0.1:7788'
        self.origin = 'http://' + self.host

    def headers(self, **extra):
        return {'Host': self.host, **extra}

    def browser_write(self, **extra):
        return self.headers(**{'Origin': self.origin, 'Content-Type': 'application/json',
                               'X-Tmux-Attn-Request': '1', **extra})

    def login(self, security=None, headers=None):
        security = self.security if security is None else security
        headers = self.browser_write() if headers is None else headers
        status, body, result_headers = security.login(security.token, headers)
        self.assertEqual(status, 200)
        cookie = SimpleCookie(result_headers['Set-Cookie'])
        return f'{COOKIE_NAME}={cookie[COOKIE_NAME].value}', body, result_headers

    def test_private_owner_key_is_created_once_and_reused(self):
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        token = self.security.token
        self.assertGreaterEqual(len(token), 32)
        self.assertEqual(WebSecurity(self.path).token, token)
        self.assertEqual(load_owner_token(self.path), token)

    def test_concurrent_initializers_publish_one_complete_key(self):
        path = Path(self.tmp.name) / 'other/auth.key'
        with ThreadPoolExecutor(max_workers=12) as pool:
            tokens = list(pool.map(lambda _: load_owner_token(path), range(30)))
        self.assertEqual(len(set(tokens)), 1)
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_unsafe_key_files_fail_closed_without_overwrite(self):
        original = self.path.read_bytes()
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            WebSecurity(self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.path.chmod(0o600)
        self.path.write_bytes(b'invalid\n')
        with self.assertRaises(ValueError):
            WebSecurity(self.path)
        self.path.write_bytes(b'\xff' * 43)
        with self.assertRaises(ValueError):
            WebSecurity(self.path)

    def test_symlinks_directories_and_fifos_cannot_be_keys(self):
        for kind in ('link', 'directory', 'fifo'):
            path = Path(self.tmp.name) / kind
            if kind == 'link':
                path.symlink_to(self.path)
            elif kind == 'directory':
                path.mkdir()
            else:
                os.mkfifo(path, 0o600)
            with self.subTest(kind=kind), self.assertRaises((ValueError, OSError)):
                WebSecurity(path)

    def test_environment_configuration_is_explicit_and_validated(self):
        with patch.dict(os.environ, {'TMUX_ATTN_AUTH_KEY_FILE': str(self.path),
                                     'TMUX_ATTN_ALLOWED_ORIGINS': 'https://work.example, http://100.64.0.2:7788'}):
            security = WebSecurity.from_environment()
        headers = {'Host': 'work.example', 'Origin': 'https://work.example'}
        self.assertIsNone(security.authorize('GET', '/api/health', headers))
        with patch.dict(os.environ, {'TMUX_ATTN_AUTH_KEY_FILE': 'relative'}):
            with self.assertRaises(ValueError):
                WebSecurity.from_environment()
        for origin in ('*', 'null', 'https://*.example', 'https://name:secret@work.example',
                       'https://work.example/path', 'https://work.example?query', 'https://work.example#hash',
                       'https://work.example:0', 'http://work.example:99999', 'https://work.example\\evil'):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                WebSecurity(self.path, allowed_origins=[origin])

    def test_static_and_public_health_are_accessible_but_private_api_requires_auth(self):
        for path in ('/', '/icons/project-cli.svg', '/auth-client.js', '/api/health', '/api/auth'):
            self.assertIsNone(self.security.authorize('GET', path, self.headers()))
        for path in ('/api/agents', '/api/info', '/api/fleet', '/api/output-file', '/api/output-preview'):
            denied = self.security.authorize('GET', path, self.headers())
            self.assertEqual(denied[0], 401, path)

    def test_bearer_authenticates_read_and_write_without_browser_origin(self):
        headers = self.headers(**{'Authorization': 'Bearer ' + self.security.token,
                                  'Content-Type': 'application/json'})
        self.assertTrue(self.security.authenticated(headers))
        for method, path in (('GET', '/api/agents'), ('POST', '/api/send'), ('POST', '/api/peers'),
                             ('POST', '/api/update'), ('POST', '/api/fleet/update')):
            self.assertIsNone(self.security.authorize(method, path, headers))
        headers['Authorization'] = 'Bearer wrong'
        self.assertEqual(self.security.authorize('GET', '/api/agents', headers)[0], 401)

    def test_host_allowlist_rejects_rebinding_even_with_valid_bearer(self):
        for host in ('attacker.example:7788', '127.0.0.1.attacker.example:7788',
                     '127.0.0.1:7788@attacker.example', '127.0.0.1:7788/extra',
                     '127.0.0.1:7788,attacker.example', '127.0.0.1', '', '127.0.0.1:bad'):
            headers = {'Host': host, 'Authorization': 'Bearer ' + self.security.token}
            with self.subTest(host=host):
                self.assertEqual(self.security.authorize('GET', '/api/health', headers)[0], 403)

    def test_explicit_bind_and_loopback_dynamic_port_are_trusted(self):
        security = WebSecurity(self.path, bind='100.64.0.2')
        self.assertIsNone(security.authorize('GET', '/api/health', {'Host': '100.64.0.2:7788'}))
        self.assertIsNone(security.authorize('GET', '/api/health', {'Host': 'localhost:51234'}, server_port=51234))
        self.assertEqual(security.authorize('GET', '/api/health', {'Host': 'localhost:7788'}, server_port=51234)[0], 403)
        wildcard = WebSecurity(self.path, bind='0.0.0.0')
        self.assertEqual(wildcard.authorize('GET', '/api/health', {'Host': '100.64.0.2:7788'})[0], 403)

    def test_login_issues_private_cookie_without_returning_owner_token(self):
        cookie, body, headers = self.login()
        self.assertTrue(body['authenticated'])
        self.assertNotIn(self.security.token, json.dumps([body, headers]))
        for attr in ('HttpOnly', 'SameSite=Strict', 'Path=/', 'Max-Age=86400'):
            self.assertIn(attr, headers['Set-Cookie'])
        self.assertNotIn('; Secure', headers['Set-Cookie'])
        self.assertTrue(self.security.authenticated(self.headers(Cookie=cookie)))
        self.assertIsNone(self.security.authorize('GET', '/api/agents', self.headers(Cookie=cookie)))

    def test_https_cookie_is_secure_and_forwarded_headers_cannot_make_origin_trusted(self):
        security = WebSecurity(self.path, allowed_origins=['https://work.example'])
        headers = {'Host': 'work.example', 'Origin': 'https://work.example',
                   'Content-Type': 'application/json', 'X-Tmux-Attn-Request': '1'}
        _cookie, _body, result = self.login(security=security, headers=headers)
        self.assertIn('; Secure', result['Set-Cookie'])
        headers['Host'] = 'attacker.example'
        headers['X-Forwarded-Host'] = 'work.example'
        self.assertEqual(security.authorize('POST', '/api/auth/session', headers)[0], 403)

    def test_authenticated_browser_writes_require_json_origin_and_custom_header(self):
        cookie, _body, _headers = self.login()
        good = self.browser_write(Cookie=cookie)
        self.assertIsNone(self.security.authorize('POST', '/api/send', good))
        for remove, expected in (('Origin', 403), ('X-Tmux-Attn-Request', 403), ('Content-Type', 415)):
            headers = dict(good)
            del headers[remove]
            self.assertEqual(self.security.authorize('POST', '/api/send', headers)[0], expected)
        for origin in ('null', 'https://attacker.example', 'http://localhost:7788'):
            headers = {**good, 'Origin': origin}
            self.assertEqual(self.security.authorize('POST', '/api/send', headers)[0], 403)
        for media_type in ('text/plain', 'application/x-www-form-urlencoded', 'multipart/form-data'):
            headers = {**good, 'Content-Type': media_type}
            self.assertEqual(self.security.authorize('POST', '/api/send', headers)[0], 415)

    def test_bearer_does_not_bypass_host_origin_or_content_type_checks(self):
        headers = self.headers(**{'Authorization': 'Bearer ' + self.security.token})
        self.assertEqual(self.security.authorize('POST', '/api/send', headers)[0], 415)
        headers.update({'Content-Type': 'application/json', 'Origin': 'https://attacker.example'})
        self.assertEqual(self.security.authorize('POST', '/api/send', headers)[0], 403)

    def test_login_is_a_protected_browser_mutation_and_wrong_key_is_not_echoed(self):
        good = self.browser_write()
        status, body, _headers = self.security.login('wrong-access-key', good)
        self.assertEqual(status, 401)
        self.assertNotIn('wrong-access-key', json.dumps(body))
        for remove in ('Origin', 'Content-Type', 'X-Tmux-Attn-Request'):
            headers = dict(good)
            del headers[remove]
            self.assertIn(self.security.login(self.security.token, headers)[0], (403, 415))
        self.assertEqual(len(self.security.sessions), 0)

    def test_duplicate_authority_and_origin_headers_fail_closed(self):
        headers = Message()
        headers['Host'] = self.host
        headers['Host'] = 'attacker.example'
        self.assertEqual(self.security.authorize('GET', '/api/health', headers)[0], 403)
        headers = Message()
        for name, value in self.browser_write().items():
            headers[name] = value
        headers['Origin'] = 'http://attacker.example'
        self.assertEqual(self.security.authorize('POST', '/api/auth/session', headers)[0], 403)

    def test_expired_sessions_are_removed_and_cookie_is_revoked_on_logout(self):
        cookie, _body, _headers = self.login()
        headers = self.headers(Cookie=cookie)
        self.now += self.security.session_ttl
        self.assertFalse(self.security.authenticated(headers))
        self.assertEqual(len(self.security.sessions), 0)
        cookie, _body, _headers = self.login()
        headers = self.headers(Cookie=cookie)
        self.assertTrue(self.security.session_status(headers)['authenticated'])
        status, body, extra = self.security.logout(headers)
        self.assertEqual(status, 200)
        self.assertFalse(body['authenticated'])
        self.assertIn('Max-Age=0', extra['Set-Cookie'])
        self.assertFalse(self.security.authenticated(headers))

    def test_session_capacity_evicts_oldest_and_multiple_logins_get_new_ids(self):
        security = WebSecurity(self.path, max_sessions=2)
        cookies = [self.login(security)[0] for _ in range(3)]
        self.assertEqual(len(set(cookies)), 3)
        self.assertEqual(len(security.sessions), 2)
        self.assertFalse(security.authenticated(self.headers(Cookie=cookies[0])))
        self.assertTrue(security.authenticated(self.headers(Cookie=cookies[-1])))

    def test_login_page_does_not_store_secrets_in_browser_storage_or_url(self):
        page = login_html()
        self.assertIn('type="password"', page)
        self.assertIn("fetch('/api/auth/session'", page)
        self.assertNotIn('localStorage', page)
        self.assertNotIn('sessionStorage', page)
        self.assertNotIn(self.security.token, page)

    def test_browser_fetch_wrapper_only_adds_header_on_same_origin_mutations(self):
        script = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const calls = []; let reloads = 0;
const window = {location: {href:'https://work.example/', origin:'https://work.example',reload(){reloads++;}},
  async fetch(input, init) {calls.push([input, init]); return {status:String(input).includes('unauthorized') ? 401 : 200};}};
vm.runInNewContext(SCRIPT, {window, Headers, URL});
(async()=>{
await window.fetch('/api/send', {method:'POST',headers:{'Content-Type':'application/json'}});
assert.equal(calls[0][1].headers.get('X-Tmux-Attn-Request'),'1');
await window.fetch('/api/agents');
assert.equal(calls[1][1].headers,undefined);
await window.fetch('https://external.example/action',{method:'POST'});
assert.equal(calls[2][1].headers,undefined);
await window.fetch(new URL('https://work.example/api/send'),{method:'post'});
assert.equal(calls[3][1].headers.get('X-Tmux-Attn-Request'),'1');
await window.fetch('/unauthorized');await window.fetch('/unauthorized');
assert.equal(reloads,1);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        script = script.replace('SCRIPT', json.dumps(AUTH_CLIENT_JS))
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
