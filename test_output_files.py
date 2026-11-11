import io
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import urlopen

import attention as a


class OutputFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / '報告 with spaces.html'
        self.file.write_text('<h1>First</h1>')
        self.text = f'完成：`{self.file}`'
        for mock in (
            patch.object(a, 'local_host_name', return_value='local'),
            patch.object(a, 'db', return_value=io.StringIO()),
            patch.object(a, 'full_message_for_pane', side_effect=lambda *args: ({'text': self.text}, None)),
        ):
            mock.start()
            self.addCleanup(mock.stop)

    def query(self, **overrides):
        return {key: [value] for key, value in {
            'pane': '%1', 'pid': '123', 'path': str(self.file), **overrides}.items()}

    def serve(self):
        class Quiet(a.make_web_handler()):
            def log_message(self, *_args):
                pass
        server = a.ThreadingHTTPServer(('127.0.0.1', 0), Quiet)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_port}/api/output-file?'

    def test_extracts_paths_without_partial_duplicates_or_http_links(self):
        text = ('[report](/Users/dev/my report.html) `/home/me/a b.txt` '
                '完成 (/Users/dev/report.html)。 https://example.com/a/b '
                '[web](https://example.com/c/d) [local](file:///home/me/a%20b.txt)')
        self.assertEqual(a.output_file_paths(text), [
            '/Users/dev/my report.html', '/home/me/a b.txt', '/Users/dev/report.html'])
        self.assertEqual(a.output_file_paths('`/home/me/../secret` //host/a/b /single'), [])
        self.assertEqual(a.output_file_paths(
            r'[report](/Users/dev/report.html "Report title") [other](/home/me/a\(b\).html)'),
            ['/Users/dev/report.html', '/home/me/a(b).html'])

    def test_only_existing_regular_readable_files_are_offered(self):
        directory = Path(self.tmp.name) / 'project'
        directory.mkdir()
        missing = directory / 'example.md'
        link = directory / 'link.md'
        link.symlink_to(self.file)
        fifo = directory / 'pipe'
        os.mkfifo(fifo)
        text = ' '.join(f'`{p}`' for p in (directory, missing, link, fifo, self.file))
        self.assertEqual(a.readable_output_files(text), [str(self.file)])

    def test_fragment_references_authorize_only_the_source_file(self):
        self.text = f'[流程圖](<{self.file}#flow>)'
        self.assertEqual(a.output_file_paths(self.text), [str(self.file)])
        self.assertEqual(a.readable_output_files(self.text), [str(self.file)])
        self.assertEqual(a.output_file_response(self.query())[0], 200)
        self.assertEqual(a.output_file_response(self.query(path='/etc/passwd'))[0], 403)

    def test_old_fragment_url_redirects_and_preserves_download(self):
        self.text = f'[流程圖](<{self.file}#flow>)'
        url = self.serve() + urlencode({
            key: values[0] for key, values in self.query(path=str(self.file) + '#flow', download='1').items()})
        with urlopen(url) as response:
            parsed = urlparse(response.url)
            self.assertEqual(parse_qs(parsed.query)['path'], [str(self.file)])
            self.assertEqual(parsed.fragment, 'flow')
            self.assertEqual(response.read(), self.file.read_bytes())
            self.assertTrue(response.headers['Content-Disposition'].startswith('attachment;'))

    def test_literal_hash_filenames_are_not_redirected_or_truncated(self):
        self.file = Path(self.tmp.name) / 'report#draft.html'
        self.file.write_text('literal hash')
        for reference in (f'`{self.file}`', self.file.as_uri()):
            self.text = f'[report]({reference})' if reference.startswith('file:') else reference
            self.assertEqual(a.output_file_paths(self.text), [str(self.file)])
            code, body, headers = a.output_file_response(self.query())
            self.assertEqual((code, body), (200, b'literal hash'))
            self.assertNotIn('Location', headers)

    def test_preview_shell_escapes_titles_and_preserves_authorized_source_query(self):
        page = a.output_preview_page(self.query(path='/tmp/<script>alert(1).md', host='peer')).decode()
        self.assertIn('&lt;script&gt;', page)
        self.assertNotIn('<script>alert(1)</script>', page)
        self.assertIn('/api/output-file?', page)
        self.assertIn('host=peer', page)
        self.assertIn('DOMPurify.sanitize', page)
        self.assertIn('JSON.stringify(JSON.parse(text),null,2)', page)
        self.assertIn('cell.textContent=value', page)
        self.assertIn('download=1', page)

    def test_preview_http_has_bounded_resource_policy(self):
        url = self.serve().replace('/api/output-file?', '/api/output-preview?')
        with urlopen(url + urlencode({key: values[0] for key, values in self.query().items()})) as response:
            self.assertEqual(response.status, 200)
            self.assertIn('text/html', response.headers['Content-Type'])
            self.assertIn("connect-src 'self'", response.headers['Content-Security-Policy'])
            self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
            self.assertIn('重新整理', response.read().decode())

    def test_http_reads_latest_bytes_and_sets_isolation_and_download_headers(self):
        url = self.serve() + urlencode({key: values[0] for key, values in self.query().items()})
        with urlopen(url) as response:
            self.assertEqual(response.read(), b'<h1>First</h1>')
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertIn('text/html', response.headers['Content-Type'])
            self.assertIn('sandbox allow-scripts;', response.headers['Content-Security-Policy'])
            self.assertNotIn('allow-same-origin', response.headers['Content-Security-Policy'])
            self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
            self.assertTrue(response.headers['Content-Disposition'].startswith('inline;'))
        self.file.write_text('<h1>Latest</h1>')
        with urlopen(url + '&download=1') as response:
            self.assertEqual(response.read(), b'<h1>Latest</h1>')
            self.assertTrue(response.headers['Content-Disposition'].startswith('attachment;'))
            self.assertIn('filename*=UTF-8', response.headers['Content-Disposition'])

    def test_unreferenced_missing_symlink_directory_and_oversized_files(self):
        self.assertEqual(a.output_file_response(self.query(path='/etc/passwd'))[0], 403)
        self.file.unlink()
        self.assertEqual(a.output_file_response(self.query())[0], 404)
        self.file.symlink_to('/etc/passwd')
        self.assertEqual(a.output_file_response(self.query())[0], 403)
        self.file.unlink()
        self.file.mkdir()
        self.assertEqual(a.output_file_response(self.query())[0], 403)
        self.file.rmdir()
        self.file.write_bytes(b'12345')
        with patch.object(a, 'MAX_OUTPUT_FILE_BYTES', 4):
            self.assertEqual(a.output_file_response(self.query())[0], 413)

    def test_large_output_local_and_peer_and_oversized_rejection(self):
        with self.file.open('wb') as handle:
            handle.truncate(33 * 1024 * 1024)
        self.assertEqual(a.readable_output_files(self.text), [str(self.file)])
        self.assertEqual(a.output_file_response(self.query())[0], 200)
        url = self.serve()
        with patch.object(a, 'load_peers', return_value=[{'name': 'peer', 'url': url.split('/api/')[0]}]):
            code, body, _ = a.output_file_response(self.query(host='peer'))
        self.assertEqual(code, 200)
        self.assertEqual(len(body), 33 * 1024 * 1024)
        with self.file.open('wb') as handle:
            handle.truncate(256 * 1024 * 1024 + 1)
        self.assertEqual(a.readable_output_files(self.text), [])
        code, body, _ = a.output_file_response(self.query())
        self.assertEqual((code, body), (413, b'File too large (limit: 256 MiB)'))

    def test_fifo_cannot_block_worker(self):
        self.file.unlink()
        os.mkfifo(self.file)
        self.assertEqual(a.output_file_response(self.query())[0], 403)

    def test_unknown_formats_download_and_session_errors_are_explicit(self):
        self.file = Path(self.tmp.name) / 'report.pdf'
        self.file.write_bytes(b'%PDF example')
        self.text = f'`{self.file}`'
        code, _, headers = a.output_file_response(self.query())
        self.assertEqual(code, 200)
        self.assertTrue(headers['Content-Disposition'].startswith('attachment;'))
        with patch.object(a, 'full_message_for_pane', return_value=(None, 'pane is not live')):
            self.assertEqual(a.output_file_response(self.query())[0], 409)

    def test_peer_forwarding_preserves_signature_and_drops_host(self):
        url = self.serve()
        with patch.object(a, 'load_peers', return_value=[{'name': 'peer', 'url': url.split('/api/')[0]}]):
            # Real HTTP roundtrip through the peer endpoint, not a mocked response.
            code, body, headers = a.output_file_response(self.query(host='peer', sig='完成：'))
        self.assertEqual(code, 200)
        self.assertEqual(body, b'<h1>First</h1>')
        self.assertIn('sandbox', headers['Content-Security-Policy'])

    def test_peer_request_never_propagates_host_or_caller_url(self):
        response = io.BytesIO(b'content')
        response.headers = {'Content-Type': 'text/plain', 'Content-Disposition': 'inline'}
        with patch.object(a, 'load_peers', return_value=[{'name': 'peer', 'url': 'http://configured:7788'}]), \
                patch.object(a, 'urlopen', return_value=response) as request:
            self.assertEqual(a.output_file_response(self.query(host='peer', sig='完成：'))[0], 200)
        parsed = urlparse(request.call_args.args[0].full_url)
        self.assertEqual(parsed.netloc, 'configured:7788')
        params = parse_qs(parsed.query)
        self.assertNotIn('host', params)
        self.assertEqual(params['sig'], ['完成：'])
        self.assertEqual(a.output_file_response(self.query(host='http://untrusted'))[0], 404)

    def test_http_denies_arbitrary_paths(self):
        url = self.serve() + urlencode({'pane': '%1', 'pid': '123', 'path': '/etc/passwd'})
        with self.assertRaises(HTTPError) as error:
            urlopen(url)
        self.assertEqual(error.exception.code, 403)
        error.exception.close()


if __name__ == '__main__':
    unittest.main()
