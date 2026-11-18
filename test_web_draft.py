"""Cross-browser composer drafts persist on the web hub with last-write-wins."""
import json
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import attention as a
from test_http_support import domain_web_handler


def fleet(live=True, online=True, host='work'):
    return {
        'ok': True,
        'hosts': [{'name': host, 'ok': online}],
        'agents': ([{'host': host, 'pane': '%1', 'pid': '123', 'tracked': True,
                    'state': 'WAIT', 'category': 'waiting', 'revision': 2}]
                   if live else []),
        'summary': {},
    }


PNG = (
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
    b'\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00'
    b'\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82'
)


class WebDraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = patch.object(a, 'STATE', Path(self.temp.name))
        self.state.start()
        self.addCleanup(self.state.stop)
        self.target = {'host': 'work', 'pane': '%1', 'pid': '123'}

    def save(self, text='hello', attachments=None, updated_at=10):
        return a.dispatch_web_draft(self.target | {
            'action': 'save', 'text': text,
            'attachments': attachments or [], 'updated_at': updated_at,
        })

    def test_save_read_clear_and_last_write_wins(self):
        code, body = self.save('from-desktop', updated_at=5)
        self.assertEqual(code, 200)
        self.assertEqual(body['draft']['text'], 'from-desktop')
        self.assertEqual(a.read_web_draft(**self.target)['text'], 'from-desktop')

        code, body = self.save('from-phone', updated_at=9)
        self.assertEqual(code, 200)
        self.assertEqual(body['draft']['text'], 'from-phone')
        self.assertEqual(a.read_web_draft(**self.target)['updated_at'], 9)

        # Older writes must not clobber a newer hub draft.
        code, body = self.save('stale-desktop', updated_at=6)
        self.assertEqual(code, 200)
        self.assertEqual(body['draft']['text'], 'from-phone')
        self.assertEqual(a.read_web_draft(**self.target)['text'], 'from-phone')

        # Older empty saves must not delete a newer draft.
        code, body = self.save('', [], updated_at=7)
        self.assertEqual(code, 200)
        self.assertEqual(body['draft']['text'], 'from-phone')

        code, body = a.dispatch_web_draft(self.target | {'action': 'clear'})
        self.assertEqual(code, 200)
        self.assertEqual(body['draft'], a.empty_web_draft())
        self.assertEqual(a.read_web_draft(**self.target), a.empty_web_draft())

    def test_empty_save_deletes_row_and_attachments_are_validated(self):
        image = {'id': 'a' * 32 + '.png', 'name': 'shot.png', 'kind': 'image'}
        self.assertEqual(self.save('x', [image], 3)[0], 200)
        self.assertEqual(a.read_web_draft(**self.target)['attachments'], [image])

        code, body = self.save('', [], 4)
        self.assertEqual(code, 200)
        self.assertEqual(body['draft'], a.empty_web_draft())

        bad = [
            [{'id': 'nope', 'name': 'x.png', 'kind': 'image'}],
            [{'id': 'a' * 32 + '.png', 'name': 'x.png', 'kind': 'file'}],
            [{'id': 'a' * 32 + '.txt', 'name': 'note.txt', 'kind': 'image'}],
            [{'id': 'a' * 32 + '.png', 'name': '', 'kind': 'image'}],
            [image, image],
        ]
        for attachments in bad:
            with self.subTest(attachments=attachments):
                self.assertEqual(self.save('x', attachments, 5)[0], 400)

    def test_prune_drops_drafts_for_closed_online_panes_only(self):
        self.save('keep-while-offline', updated_at=2)
        a.web_later_fleet(deepcopy(fleet(live=False, online=False)))
        self.assertEqual(a.read_web_draft(**self.target)['text'], 'keep-while-offline')

        self.save('gone', updated_at=3)
        a.web_later_fleet(deepcopy(fleet(live=False, online=True)))
        self.assertEqual(a.read_web_draft(**self.target), a.empty_web_draft())

    def test_http_get_post_and_upload_file_round_trip(self):
        upload_id, error = a.store_upload(PNG)
        self.assertIsNone(error)
        attachments = [{'id': upload_id, 'name': 'dot.png', 'kind': 'image'}]

        server = a.ThreadingHTTPServer(('127.0.0.1', 0), domain_web_handler(app=a))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'

        body = json.dumps(self.target | {
            'action': 'save', 'text': 'with image', 'attachments': attachments, 'updated_at': 7,
        }).encode()
        request = Request(base + '/api/draft', data=body,
                          headers={'Content-Type': 'application/json'}, method='POST')
        with urlopen(request) as response:
            saved = json.load(response)
        self.assertEqual(saved['draft']['attachments'], attachments)

        with urlopen(base + '/api/draft?host=work&pane=%251&pid=123') as response:
            fetched = json.load(response)
        self.assertEqual(fetched['draft']['text'], 'with image')

        with urlopen(base + f'/api/upload-file?host=&pane=%251&pid=123&id={upload_id}') as response:
            self.assertEqual(response.headers.get('Content-Type'), 'image/png')
            self.assertEqual(response.read(), PNG)

        # Successful send clears the hub draft.
        with patch.object(a, 'local_host_name', return_value='work'), \
                patch.object(a, 'send_to_pane', return_value=None):
            send_body = json.dumps(self.target | {'text': 'with image', 'images': [upload_id]}).encode()
            send_req = Request(base + '/api/send', data=send_body,
                               headers={'Content-Type': 'application/json'}, method='POST')
            with urlopen(send_req) as response:
                self.assertTrue(json.load(response)['ok'])
        self.assertEqual(a.read_web_draft(**self.target), a.empty_web_draft())

    def test_upload_file_rejects_bad_ids_and_missing_files(self):
        code, body, _headers = a.upload_file_response({
            'host': [''], 'pane': ['%1'], 'pid': ['123'], 'id': ['../etc/passwd'],
        })
        self.assertEqual(code, 400)
        code, body, _headers = a.upload_file_response({
            'host': [''], 'pane': ['%1'], 'pid': ['123'], 'id': ['a' * 32 + '.png'],
        })
        self.assertEqual(code, 404)

    def test_peer_upload_file_relays_bytes(self):
        upload_id = 'b' * 32 + '.png'

        class FakeResponse:
            def __init__(self):
                self.headers = {
                    'Content-Type': 'image/png',
                    'Content-Disposition': 'inline; filename="x.png"',
                    'X-Content-Type-Options': 'nosniff',
                    'Referrer-Policy': 'no-referrer',
                }

            def read(self, _n=-1):
                return PNG

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        with patch.object(a, 'local_host_name', return_value='hub'), \
                patch.object(a, 'load_peers', return_value=[{'name': 'work', 'url': 'http://peer'}]), \
                patch.object(a, 'peer_urlopen', return_value=FakeResponse()):
            code, body, headers = a.upload_file_response({
                'host': ['work'], 'pane': ['%1'], 'pid': ['123'], 'id': [upload_id],
            })
        self.assertEqual((code, body, headers['Content-Type']), (200, PNG, 'image/png'))

        class Missing:
            def __enter__(self):
                raise HTTPError('http://peer', 404, 'no', hdrs=None, fp=None)

            def __exit__(self, *_exc):
                return False

        with patch.object(a, 'local_host_name', return_value='hub'), \
                patch.object(a, 'load_peers', return_value=[{'name': 'work', 'url': 'http://peer'}]), \
                patch.object(a, 'peer_urlopen', return_value=Missing()):
            code, _body, _headers = a.upload_file_response({
                'host': ['work'], 'pane': ['%1'], 'pid': ['123'], 'id': [upload_id],
            })
        self.assertEqual(code, 404)


if __name__ == '__main__':
    unittest.main()
