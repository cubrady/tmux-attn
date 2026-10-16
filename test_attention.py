import importlib.util
import json
from contextlib import closing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import base64
import shlex
import http.client
import io
import stat
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen

spec = importlib.util.spec_from_file_location('attention', Path(__file__).with_name('attention.py'))
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = a.STATE
        a.STATE = Path(self.tmp.name)
        self.con = a.db()
        self.live = {('%1','123'): {'pane':'%1','pid':'123','session':'project','window':'agent','path':'/project'}}
    def tearDown(self):
        self.con.close(); a.STATE = self.previous; self.tmp.cleanup()
    def write(self, state, reason, review=False):
        a.record(self.con,'socket','%1','123','codex',state,reason,review)
    def rows(self, **kw):
        return a.items(self.con,'socket',self.live,now=time.time()+3,**kw)[0]
    def test_lifecycle_ack_new_work(self):
        self.write('RUNNING','Working'); self.assertEqual(self.rows(),[])
        self.write('WAIT','Approve'); item=self.rows()[0]
        # Viewing confirmation does not change the content's revision.
        self.assertEqual(self.rows()[0]['revision'],item['revision'])
        a.acknowledge(self.con,item); self.assertEqual(self.rows(),[])
        self.write('RUNNING','Working'); self.write('IDLE','Finished',True)
        self.assertEqual(self.rows()[0]['state'],'IDLE')
    def test_duplicate_and_snooze(self):
        self.write('WAIT','Approve'); item=self.rows()[0]
        a.acknowledge(self.con,item,300)
        self.write('WAIT','Approve'); self.assertEqual(self.rows(),[])
        self.assertEqual(len(a.items(self.con,'socket',self.live,now=time.time()+301)[0]),1)
        self.write('WAIT','Different decision'); self.assertEqual(len(self.rows()),1)
    def test_socket_and_reused_pane_isolation(self):
        self.write('WAIT','Approve')
        self.assertEqual(a.items(self.con,'other',self.live)[0],[])
        self.assertEqual(a.items(self.con,'socket',{('%1','999'):self.live[('%1','123')]})[0],[])
    def test_concurrent_writers_no_lost_rows(self):
        def write(i):
            with closing(a.db()) as con: a.record(con,'socket',f'%{i}','123','pi','WAIT',str(i))
        with ThreadPoolExecutor(max_workers=8) as pool: list(pool.map(write,range(30)))
        self.assertEqual(self.con.execute('SELECT count(*) FROM panes').fetchone()[0],30)
    def test_stale_sidebar_ack_does_not_resurrect_or_replace_snooze(self):
        self.write('WAIT','First'); old=self.rows()[0]
        self.write('WAIT','Second'); current=self.rows()[0]
        a.acknowledge(self.con,current)
        a.acknowledge(self.con,old)
        self.assertEqual(self.rows(),[])
        self.write('WAIT','Third'); newer=self.rows()[0]
        a.acknowledge(self.con,newer,300)
        a.acknowledge(self.con,old,10)
        self.assertEqual(self.rows(),[])

    def test_status_cycle_wraparound_changes_and_client_isolation(self):
        self.live[('%1','123')]['window_id']='@1'
        self.live[('%2','456')]={'pane':'%2','pid':'456','session':'second','window_id':'@2','path':'/second'}
        self.write('WAIT','Answer')
        a.record(self.con,'socket','%2','456','claude','WAIT','Answer')
        before=[dict(row) for row in self.con.execute('SELECT * FROM panes ORDER BY pane')]
        with patch.object(a,'live_panes',return_value=self.live),patch.object(a,'jump',return_value=True) as jump:
            for expected in ['%1','%2','%1']:
                a.cycle_category(self.con,'socket','client-a','wait')
                self.assertEqual(jump.call_args.args[0]['pane'],expected)
            a.cycle_category(self.con,'socket','client-b','wait')
            self.assertEqual(jump.call_args.args[0]['pane'],'%1')
            jump.reset_mock()
            a.cycle_category(self.con,'socket','client-a','review')
            jump.assert_not_called()
            self.assertEqual(before,[dict(row) for row in self.con.execute('SELECT * FROM panes ORDER BY pane')])
            # Departing pane and changing category cannot leave a stale target.
            self.write('RUNNING','Working')
            a.cycle_category(self.con,'socket','client-a','wait')
            self.assertEqual(jump.call_args.args[0]['pane'],'%2')
            del self.live[('%2','456')]
            jump.reset_mock()
            a.cycle_category(self.con,'socket','client-a','wait')
            jump.assert_not_called()
        self.write('IDLE','Completed',True)
        rows,_=a.items(self.con,'socket',self.live,True,now=time.time()+3)
        groups=a.status_groups(rows)
        self.assertEqual(a.status_text(groups),'🙋 0 · 👀 1 · 🏃 0 · 💤 0')
        self.assertIn('#[range=control|1]👀 1#[norange]',a.status_text(groups,True))
        a.acknowledge(self.con,rows[0])
        rows,_=a.items(self.con,'socket',self.live,True,now=time.time()+3)
        groups=a.status_groups(rows)
        self.assertEqual(a.status_text(groups),'🙋 0 · 👀 0 · 🏃 0 · 💤 1')
        self.assertEqual(groups['review'], [])
        self.assertEqual(groups['idle'], rows)
        with patch.object(a,'live_panes',return_value=self.live),patch.object(a,'jump',return_value=True) as jump:
            a.cycle_category(self.con,'socket','client-a','review')
            jump.assert_not_called()
            a.cycle_category(self.con,'socket','client-a','idle')
            self.assertEqual(jump.call_args.args[0]['pane'],'%1')

    def test_verified_identity_replacement_is_new_unread_even_for_same_state(self):
        a.record(self.con,'socket','%1','123','codex','WAIT','Answer needed',event_time=2000000000)
        old=self.con.execute('SELECT * FROM panes').fetchone()
        a.acknowledge(self.con,old)
        a.record(self.con,'socket','%1','123','codex','WAIT','Answer needed',event_time=1000000000,replace_identity=True)
        current=self.con.execute('SELECT * FROM panes').fetchone()
        self.assertGreater(current['revision'],current['acknowledged'])
        self.assertEqual(current['updated'],1000000000)

    def test_classifiers(self):
        self.assertEqual(a.classify('cursor','stop',{'status':'completed'})[0],'IDLE')
        self.assertEqual(a.classify('cursor','stop',{'status':'aborted'})[0],'IDLE')
        self.assertIn('error',a.classify('cursor','stop',{'status':'error'})[1])
        self.assertIsNone(a.classify('claude','Notification',{'notification_type':'idle_prompt'}))
        self.assertEqual(a.classify('codex','PermissionRequest',{})[0],'WAIT')
        self.assertEqual(a.classify('codex','PreToolUse',{'tool_name':'request_user_input'})[0],'WAIT')
        self.assertEqual(a.classify('pi','ui_prompt_start',{'title':'Pick one'})[0],'WAIT')
        self.assertEqual(a.classify('pi','agent_settled',{})[0],'IDLE')
        soft=a.classify('claude','Stop',{'last_assistant_message':'你希望採用哪個方案？'})
        self.assertEqual((soft[0],soft[2]),('IDLE',True))
        self.assertIn('review suggested',soft[1])
        soft_en=a.classify('codex','Stop',{'last_assistant_message':'Which option do you prefer?'})
        self.assertEqual((soft_en[0],soft_en[2]),('IDLE',True))
        self.assertEqual(a.classify('codex','Stop',{'last_assistant_message':'All done.'})[0],'IDLE')

    def test_cursor_fast_completion_and_inherited_identity(self):
        def hook(agent,event,data):
            with patch.object(a.sys,'stdin',io.StringIO(json.dumps(data))), patch.object(a.sys,'stdout',io.StringIO()) as out, patch.object(a,'hook_target',return_value=('socket','%1','123')):
                a.event_command(agent,event)
                self.assertEqual(json.loads(out.getvalue()),{})
        hook('claude','PostToolUse',{'cursor_version':'2026.10.01'})
        self.assertEqual(self.con.execute('SELECT agent FROM panes').fetchone()[0],'cursor')
        hook('cursor','stop',{'status':'completed','generation_id':'one'})
        row=self.con.execute('SELECT * FROM panes').fetchone()
        a.acknowledge(self.con,row)
        hook('cursor','stop',{'status':'completed','generation_id':'one'})
        self.assertEqual(self.rows(),[])
        hook('cursor','stop',{'status':'completed','generation_id':'two'})
        self.assertEqual(len(self.rows()),1)

    def test_project_aliases_path_overrides_and_collisions(self):
        paths=['/a/web-app-sampleapp','/a/a-long-new-project','/b/a-long-new-platform','/a/sampleapp-api','/a/sampleapp-web','/a/shared','/b/shared']
        aliases={'web-app-sampleapp':'sampleapp','sampleapp-api':'demo','sampleapp-web':'demo','/b/shared':'other'}
        labels=a.project_labels(paths,aliases)
        self.assertEqual(labels['/a/web-app-sampleapp'],'sampleapp')
        self.assertEqual(labels['/b/shared'],'other')
        self.assertEqual(labels['/a/sampleapp-api'],'demo-api')
        self.assertEqual(labels['/a/sampleapp-web'],'demo-web')
        self.assertEqual(len(set(labels.values())),len(paths))
        self.assertTrue(all(len(label)<=12 for label in labels.values()))
        self.assertEqual(labels,a.project_labels(list(reversed(paths))+paths,aliases))

    def test_project_labels_can_use_a_longer_limit_for_the_web(self):
        long_name = '/w/sample-roven-ai'
        self.assertEqual(a.project_labels([long_name], {})[long_name], 'sample-rove…')
        self.assertEqual(a.project_labels([long_name], {}, limit=28)[long_name], 'sample-roven-ai')
        too_long = '/w/' + 'x' * 40
        self.assertEqual(a.project_labels([too_long], {}, limit=28)[too_long], 'x' * 27 + '…')
        paths = ['/a/service-api', '/b/service-api', '/a/service-web', '/b/billing-service-gateway']
        labels = a.project_labels(paths, {}, limit=28)
        self.assertEqual(len(set(labels.values())), len(paths))
        self.assertTrue(all(len(label) <= 28 for label in labels.values()))

    def test_web_payload_shows_project_names_that_fit_a_card_title(self):
        a.record(self.con, 'socket', '%1', '123', 'claude', 'IDLE', 'Finished', True)
        live = {('%1', '123'): {'pane': '%1', 'pid': '123', 'session': 'work', 'window': 'one', 'window_id': '@1', 'path': '/proj/sample-roven-ai'}}
        with patch.object(a, 'live_panes', return_value=live):
            payload = a.agents_payload(self.con, 'socket', now=time.time() + 3)
        self.assertEqual(payload['agents'][0]['project'], 'sample-roven-ai')

    def test_child_codex_hooks_cannot_change_root_state_or_transcript(self):
        root = Path(self.tmp.name)/'root.jsonl'
        child = Path(self.tmp.name)/'child.jsonl'
        root.write_text(json.dumps({'type':'session_meta','payload':{'id':'test-root','source':'vscode'}})+'\n')
        child.write_text(json.dumps({'type':'session_meta','payload':{'id':'test-child','session_id':'test-root','source':{'subagent':{'parent_thread_id':'test-root'}}}})+'\n')
        def hook(event,path):
            data={'session_id':'test-root','transcript_path':str(path),'turn_id':'turn-'+path.stem}
            with patch.object(a.sys,'stdin',io.StringIO(json.dumps(data))), patch.object(a.sys,'stdout',io.StringIO()) as out, patch.object(a,'hook_target',return_value=('socket','%1','123')):
                a.event_command('codex',event)
                self.assertEqual(json.loads(out.getvalue()),{})
        hook('UserPromptSubmit',root)
        before=dict(self.con.execute('SELECT * FROM panes').fetchone())
        mapping=dict(self.con.execute('SELECT * FROM codex_sessions').fetchone())
        for event in ['PostToolUse','Stop','PermissionRequest','SessionEnd']:
            hook(event,child)
            self.assertEqual(dict(self.con.execute('SELECT * FROM panes').fetchone()),before)
            self.assertEqual(dict(self.con.execute('SELECT * FROM codex_sessions').fetchone()),mapping)
        # Registering a wrong path also cannot replace the validated root path.
        a.register_codex_session(self.con,'socket','%1','123',{'session_id':'test-root','transcript_path':str(child)})
        self.assertEqual(self.con.execute('SELECT rollout_path FROM codex_sessions').fetchone()[0],str(root))
        hook('Stop',root)
        row=self.con.execute('SELECT * FROM panes').fetchone()
        self.assertEqual((row['state'],row['needs_review']),('IDLE',1))

    def test_agents_payload_sorting_counts_and_aliases(self):
        self.live = {
            ('%1', '123'): {'pane': '%1', 'pid': '123', 'session': 'work', 'window': 'one', 'window_id': '@1', 'path': '/proj/example-wallet-gateway'},
            ('%2', '456'): {'pane': '%2', 'pid': '456', 'session': 'work', 'window': 'two', 'window_id': '@2', 'path': '/proj/web-app-sampleapp'},
            ('%3', '789'): {'pane': '%3', 'pid': '789', 'session': 'work', 'window': 'three', 'window_id': '@3', 'path': '/proj/lending'},
            ('%4', '101'): {'pane': '%4', 'pid': '101', 'session': 'work', 'window': 'four', 'window_id': '@4', 'path': '/proj/idle-box'},
            ('%5', '202'): {'pane': '%5', 'pid': '202', 'session': 'work', 'window': 'shell', 'window_id': '@5', 'path': '/proj/shell-only', 'command': 'python'},
        }
        a.record(self.con, 'socket', '%1', '123', 'claude', 'WAIT', 'Approval needed')
        a.record(self.con, 'socket', '%2', '456', 'codex', 'IDLE', 'Response finished; review needed', True)
        a.record(self.con, 'socket', '%3', '789', 'cursor', 'RUNNING', 'Working')
        a.record(self.con, 'socket', '%4', '101', 'pi', 'IDLE', 'Waiting for next task')
        with patch.object(a, 'load_project_aliases', return_value={'example-wallet-gateway': 'wallet', 'web-app-sampleapp': 'sampleapp'}):
            payload = a.agents_payload(self.con, 'socket', self.live, now=time.time()+3)
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['summary'], {
            'wait': 1, 'review': 1, 'review_seen': 0, 'running': 1, 'idle': 1, 'tracked': 4, 'untracked': 1,
        })
        emojis = [row['emoji'] for row in payload['agents'] if row['tracked']]
        self.assertEqual(emojis, ['🙋', '👀', '🏃', '💤'])
        self.assertEqual([row['category'] for row in payload['agents'] if row['tracked']],
                         ['wait', 'review', 'running', 'idle'])
        self.assertEqual([row['project'] for row in payload['agents'] if row['tracked']], ['wallet', 'sampleapp', 'lending', 'idle-box'])
        self.assertEqual([row['cli'] for row in payload['agents'] if row['tracked']], ['claude', 'codex', 'agent', 'pi'])
        self.assertEqual(payload['agents'][0]['pane'], '%1')
        self.assertIn('pane', payload['agents'][0])
        self.assertIn('pid', payload['agents'][0])
        self.assertIn('需要批准', payload['agents'][0]['reason'])
        self.assertEqual(a.cli_label('cursor'), 'agent')
        self.assertTrue(any(not row['tracked'] and row['cli'] == 'python' for row in payload['agents']))
        self.assertEqual(payload['summary']['untracked'], 1)
        self.assertEqual(a.agent_severity({'reason': 'Agent stopped with an error; review needed'}), 'error')
        self.assertEqual(a.agent_severity({'reason': 'Interrupted; review current work'}), 'interrupt')
        self.assertEqual(a.agent_severity({'reason': 'Working'}), '')
        self.assertEqual(payload['agents'][0]['source'], 'hook')
        self.assertEqual(next(row['source'] for row in payload['agents'] if not row['tracked']), 'shell')

    def test_record_source_and_notify_on_new_wait(self):
        calls = []
        with patch.object(a, 'maybe_notify_wait', side_effect=lambda *args: calls.append(args)):
            a.record(self.con, 'socket', '%1', '123', 'claude', 'RUNNING', 'Working', source='hook')
            self.assertEqual(calls, [])
            a.record(self.con, 'socket', '%1', '123', 'claude', 'WAIT', 'Approval needed', source='hook')
            self.assertEqual(calls, [('claude', 'Approval needed', '%1')])
            rev = self.con.execute('SELECT revision FROM panes').fetchone()['revision']
            # Same WAIT from observer only refreshes source — no revision bump / ntfy.
            a.record(self.con, 'socket', '%1', '123', 'claude', 'WAIT', 'Approval needed', source='observer')
            self.assertEqual(len(calls), 1)
            self.assertEqual(self.con.execute('SELECT revision,source FROM panes').fetchone()['revision'], rev)
            self.assertEqual(self.con.execute('SELECT source FROM panes').fetchone()['source'], 'observer')
            # Reason change while still WAIT (unread) updates row but does not re-notify.
            a.record(self.con, 'socket', '%1', '123', 'claude', 'WAIT', 'Still waiting', source='observer')
            self.assertEqual(len(calls), 1)
        row = self.con.execute('SELECT source,state FROM panes').fetchone()
        self.assertEqual((row['source'], row['state']), ('observer', 'WAIT'))


    def test_conversation_preview_clip_and_parse(self):
        self.assertEqual(a.clip_preview('x' * a.PREVIEW_CHARS), 'x' * a.PREVIEW_CHARS)
        self.assertTrue(a.clip_preview('x' * (a.PREVIEW_CHARS + 1)).endswith('…'))
        self.assertEqual(len(a.clip_preview('x' * (a.PREVIEW_CHARS + 1))), a.PREVIEW_CHARS)
        self.assertEqual(len(a.clip_preview('y' * 500, a.PREVIEW_CHARS_FULL)), 500)
        md = a.clip_preview('# Title\n\n- item **one**\n- item two\n\n```\ncode\n```', 200)
        self.assertIn('\n', md)
        self.assertIn('# Title', md)
        self.assertIn('**one**', md)
        self.assertTrue(a.user_text_is_noise('<environment_context>\n  <cwd>/tmp</cwd>'))
        self.assertFalse(a.user_text_is_noise('修好登入流程'))
        claude = Path(self.tmp.name) / 'claude.jsonl'
        claude.write_text('\n'.join([
            json.dumps({'type': 'user', 'isMeta': True, 'message': {'role': 'user', 'content': '<local-command-caveat>x'}}),
            json.dumps({'type': 'user', 'message': {'role': 'user', 'content': '第一句任務：修好登入'}}),
            json.dumps({'type': 'user', 'message': {'role': 'user', 'content': '第二句：加上測試'}}),
            json.dumps({'type': 'user', 'message': {'role': 'user', 'content': '第三句：準備送審'}}),
            json.dumps({'type': 'user', 'message': {'role': 'user', 'content': '第四句：部署'}}),
            json.dumps({'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': '已準備 PR，還要我開嗎？'}]}}),
        ]) + '\n')
        preview = a.conversation_preview(claude, 'claude')
        self.assertEqual(preview['users'], ['第二句：加上測試', '第三句：準備送審', '第四句：部署'])
        self.assertIn('已準備 PR', preview['agent'])
        codex = Path(self.tmp.name) / 'rollout-demo.jsonl'
        codex.write_text('\n'.join([
            json.dumps({'type': 'event_msg', 'payload': {'type': 'user_message', 'message': '請檢查捐款錯誤'}}),
            json.dumps({'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': '根因是 JSON parse'}}),
        ]) + '\n')
        preview = a.conversation_preview(codex, 'codex')
        self.assertEqual(preview['users'], ['請檢查捐款錯誤'])
        self.assertEqual(preview['agent'], '根因是 JSON parse')

        cursor = Path(self.tmp.name) / 'cursor.jsonl'
        cursor.write_text('\n'.join([
            json.dumps({'role': 'user', 'message': {'content': [{'type': 'text', 'text': '請修 Cursor 預覽'}]}}),
            json.dumps({'role': 'assistant', 'message': {'content': [{'type': 'text', 'text': '已接上 agent-transcripts'}]}}),
        ]) + '\n')
        preview = a.conversation_preview(cursor, 'cursor')
        self.assertEqual(preview['users'], ['請修 Cursor 預覽'])
        self.assertIn('agent-transcripts', preview['agent'])
        with patch.object(a, 'latest_cursor_transcript', return_value=str(cursor)):
            self.assertEqual(a.latest_cursor_transcript('/x'), str(cursor))


    def test_git_workdir_info_and_payload_env(self):
        a._GIT_ENV_CACHE.clear()
        with patch.object(a.subprocess, 'run') as run:
            run.return_value = type('R', (), {
                'returncode': 0,
                'stdout': '## feat/demo...origin/feat/demo\n M attention.py\n',
                'stderr': '',
            })()
            info = a.git_workdir_info('/proj/web-app-sampleapp')
        self.assertEqual(info['repo'], 'web-app-sampleapp')
        self.assertEqual(info['branch'], 'feat/demo')
        self.assertTrue(info['dirty'])
        a._GIT_ENV_CACHE.clear()
        with patch.object(a.subprocess, 'run') as run:
            run.return_value = type('R', (), {
                'returncode': 0,
                'stdout': '## No commits yet on main\n',
                'stderr': '',
            })()
            empty = a.git_workdir_info('/proj/new-repo')
        self.assertEqual(empty['branch'], 'main')
        self.assertFalse(empty['dirty'])
        self.live = {
            ('%1', '123'): {'pane': '%1', 'pid': '123', 'session': 'work', 'window': 'one', 'window_id': '@1', 'path': '/proj/web-app-sampleapp'},
        }
        a.record(self.con, 'socket', '%1', '123', 'claude', 'IDLE', 'Waiting for next task')
        with patch.object(a, 'git_workdir_info', return_value={'repo': 'web-app-sampleapp', 'branch': 'feat/demo', 'dirty': True}), \
             patch.object(a, 'load_project_aliases', return_value={'web-app-sampleapp': 'sampleapp'}):
            payload = a.agents_payload(self.con, 'socket', self.live, now=time.time()+3)
        row = payload['agents'][0]
        self.assertEqual(row['project'], 'sampleapp')
        self.assertEqual(row['repo'], 'web-app-sampleapp')
        self.assertEqual(row['branch'], 'feat/demo')
        self.assertTrue(row['dirty'])

    def test_agents_payload_includes_preview(self):

        self.live = {
            ('%1', '123'): {'pane': '%1', 'pid': '123', 'session': 'work', 'window': 'one', 'window_id': '@1', 'path': '/proj/demo'},
        }
        a.record(self.con, 'socket', '%1', '123', 'claude', 'WAIT', 'Approval needed')
        a.remember_preview(self.con, 'socket', '%1', '123', user='修好 OAuth', agent='需要你確認 redirect URI')
        payload = a.agents_payload(self.con, 'socket', self.live, now=time.time()+3)
        preview = payload['agents'][0]['preview']
        self.assertEqual(preview['users'], ['修好 OAuth'])
        self.assertIn('redirect URI', preview['agent'])

    def test_transcript_model_reads_latest_model_and_effort(self):
        claude = Path(self.tmp.name) / 'model-claude.jsonl'
        claude.write_text('\n'.join([
            json.dumps({'type': 'assistant', 'message': {'model': 'claude-opus-5-5', 'content': 'a'}}),
            json.dumps({'type': 'assistant', 'message': {'model': 'claude-sonnet-5-5', 'content': 'b'}}),
            json.dumps({'type': 'assistant', 'message': {'model': '<synthetic>', 'content': 'c'}}),
            json.dumps({'type': 'user', 'message': {'content': 'd'}}),
        ]) + '\n')
        self.assertEqual(a.transcript_model(claude, 'claude'), ('claude-sonnet-5-5', ''))
        codex = Path(self.tmp.name) / 'rollout-model.jsonl'
        codex.write_text('\n'.join([
            json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-6-luna', 'effort': 'low'}}),
            json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-6.1-sol', 'effort': 'xhigh'}}),
            json.dumps({'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': 'x'}}),
        ]) + '\n')
        self.assertEqual(a.transcript_model(codex, 'codex'), ('gpt-6.1-sol', 'xhigh'))
        cursor = Path(self.tmp.name) / 'cursor-model.jsonl'
        cursor.write_text(json.dumps({'role': 'assistant', 'message': {'content': []}}) + '\n')
        self.assertEqual(a.transcript_model(cursor, 'cursor'), ('', ''))
        self.assertEqual(a.transcript_model(Path(self.tmp.name) / 'missing.jsonl', 'claude'), ('', ''))

    def test_transcript_model_keeps_last_known_when_tail_loses_it(self):
        path = Path(self.tmp.name) / 'model-tail.jsonl'
        path.write_text(json.dumps({'type': 'assistant', 'message': {'model': 'claude-opus-5-5', 'content': 'a'}}) + '\n')
        self.assertEqual(a.transcript_model(path, 'claude')[0], 'claude-opus-5-5')
        path.write_text(json.dumps({'type': 'user', 'message': {'content': 'long tool output'}}) + '\n')
        self.assertEqual(a.transcript_model(path, 'claude')[0], 'claude-opus-5-5')

    def test_transcript_model_finds_turn_context_beyond_the_tail(self):
        path = Path(self.tmp.name) / 'rollout-long.jsonl'
        filler = json.dumps({'type': 'event_msg', 'payload': {'type': 'exec_command_output', 'text': 'x' * 2000}})
        path.write_text('\n'.join(
            [json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-6.1-sol', 'effort': 'high'}})]
            + [filler] * ((a.TRANSCRIPT_TAIL_BYTES // 2000) + 20)) + '\n')
        self.assertGreater(path.stat().st_size, a.TRANSCRIPT_TAIL_BYTES)
        self.assertEqual(a.transcript_model(path, 'codex'), ('gpt-6.1-sol', 'high'))

    def test_model_label_shortens_known_ids_only(self):
        self.assertEqual(a.model_label('claude-sonnet-5-5'), 'sonnet 5.5')
        self.assertEqual(a.model_label('claude-opus-5-5'), 'opus 5.5')
        self.assertEqual(a.model_label('claude-haiku-4-5-20251001'), 'haiku 4.5')
        self.assertEqual(a.model_label('claude-fable-5-1'), 'fable 5.1')
        self.assertEqual(a.model_label('claude-3-5-sonnet-20241022'), 'sonnet 3.5')
        self.assertEqual(a.model_label('gpt-6.1-sol'), 'gpt-6.1-sol')
        self.assertEqual(a.model_label(''), '')

    def test_claude_stop_waiting_on_background_tasks_is_running(self):
        tasks = [{'type': 'shell', 'status': 'running'}, {'type': 'subagent', 'status': 'running'},
                 {'type': 'shell', 'status': 'running'}, {'type': 'shell', 'status': 'completed'}]
        self.assertEqual(a.classify('claude', 'Stop', {'background_tasks': tasks}),
                         ('RUNNING', 'Background work: waiting on 2 shell, 1 subagent', False))
        # Finished tasks, malformed payloads and other agents keep the normal finished-turn result.
        for data in ({'background_tasks': [{'type': 'shell', 'status': 'completed'}]},
                     {'background_tasks': []}, {'background_tasks': 'running'}, {'background_tasks': ['running', None]}):
            self.assertEqual(a.classify('claude', 'Stop', data), ('IDLE', 'Response finished; review needed', True))
        self.assertEqual(a.classify('codex', 'Stop', {'background_tasks': tasks})[0], 'IDLE')
        # Something the user must answer outranks the wait; an untyped task is still counted.
        self.assertEqual(a.classify('claude', 'Stop', {'last_assistant_message': '要繼續嗎？', 'background_tasks': tasks})[1],
                         'Response ends with a question; review suggested')
        self.assertEqual(a.classify('claude', 'Stop', {'background_tasks': [{'status': 'running'}]})[1],
                         'Background work: waiting on 1 task')

    def test_background_wait_is_a_running_row_with_its_own_icon(self):
        a.record(self.con, 'socket', '%1', '123', 'claude', *a.classify('claude', 'Stop', {'background_tasks': [{'type': 'shell', 'status': 'running'}]}))
        row = a.agents_payload(self.con, 'socket', self.live, now=time.time()+3)['agents'][0]
        self.assertEqual((row['category'], row['emoji'], row['background_wait'], row['pending'], row['unread']),
                         ('running', '⏳', True, False, False))
        self.assertEqual(row['reason'], '等待背景工作（1 shell）· 可先看進度')
        self.assertEqual(a.attention_summary([row])['running'], 1)
        self.assertEqual(a.window_badge([row]), '🏃')
        # The same row relayed through the fleet keeps its icon; a peer without the field shows the plain one.
        self.assertEqual(a.tag_host({'agents': [row]}, 'peer')[0][0]['emoji'], '⏳')
        self.assertEqual(a.tag_host({'agents': [{**row, 'background_wait': False}]}, 'peer')[0][0]['emoji'], '🏃')
        # Waking up replaces the reason, so the wait marker leaves with it.
        a.record(self.con, 'socket', '%1', '123', 'claude', 'RUNNING', 'Working')
        woken = a.agents_payload(self.con, 'socket', self.live, now=time.time()+3)['agents'][0]
        self.assertEqual((woken['emoji'], woken['background_wait'], woken['reason']), ('🏃', False, '正在工作'))

    def test_agents_payload_includes_model_and_effort(self):
        self.live = {
            ('%1', '123'): {'pane': '%1', 'pid': '123', 'session': 'work', 'window': 'one', 'window_id': '@1', 'path': '/proj/demo'},
        }
        rollout = Path(self.tmp.name) / 'rollout-payload.jsonl'
        rollout.write_text(json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-6.1-sol', 'effort': 'xhigh'}}) + '\n')
        a.record(self.con, 'socket', '%1', '123', 'codex', 'WAIT', 'Approval needed')
        a.remember_preview(self.con, 'socket', '%1', '123', user='修好 OAuth', agent='確認', transcript_path=str(rollout))
        row = a.agents_payload(self.con, 'socket', self.live, now=time.time()+3)['agents'][0]
        self.assertEqual((row['model'], row['model_label'], row['effort']), ('gpt-6.1-sol', 'gpt-6.1-sol', 'xhigh'))
        shell = {('%2', '9'): {'pane': '%2', 'pid': '9', 'session': 'work', 'window': 'two', 'window_id': '@2', 'path': '/x', 'command': 'zsh'}}
        untracked = a.agents_payload(self.con, 'socket', shell, now=time.time()+3)['agents'][0]
        self.assertEqual((untracked['model'], untracked['model_label'], untracked['effort']), ('', '', ''))

    def test_agent_sort_key_newest_pending_first(self):
        older = {'tracked': True, 'state': 'IDLE', 'needs_review': True, 'unread': True, 'pending': True, 'updated': 100, 'host': 'a', 'pane': '%1'}
        newer = {'tracked': True, 'state': 'IDLE', 'needs_review': True, 'unread': True, 'pending': True, 'updated': 200, 'host': 'a', 'pane': '%2'}
        wait = {'tracked': True, 'state': 'WAIT', 'pending': True, 'updated': 50, 'host': 'a', 'pane': '%3'}
        rows = [older, newer, wait]
        rows.sort(key=a.agent_sort_key)
        self.assertEqual([row['pane'] for row in rows], ['%3', '%2', '%1'])


    def test_full_preview_refreshes_stale_short_cache(self):
        long_agent = '完整回覆：' + ('內容' * 120)
        transcript = Path(self.tmp.name) / 'claude.jsonl'
        transcript.write_text(json.dumps({
            'type': 'assistant',
            'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': long_agent}]},
        }) + '\n')
        self.live = {
            ('%1', '123'): {'pane': '%1', 'pid': '123', 'session': 'work', 'window': 'one', 'window_id': '@1', 'path': '/proj/demo'},
        }
        a.record(self.con, 'socket', '%1', '123', 'claude', 'IDLE', 'Response finished; review needed', True)
        short = a.clip_preview(long_agent, 100)
        with self.con:
            self.con.execute(
                'UPDATE panes SET preview_agent=?, preview_users=?, preview_mtime=?, transcript_path=? WHERE pane=? AND pid=?',
                (short, '[]', transcript.stat().st_mtime, str(transcript), '%1', '123'),
            )
        row = dict(self.con.execute('SELECT * FROM panes').fetchone())
        with patch.object(a, 'resolve_transcript_path', return_value=str(transcript)):
            full = a.pane_conversation_preview(self.con, row, self.live[('%1', '123')], limit=a.PREVIEW_CHARS_FULL)
        self.assertGreater(len(full['agent']), len(short))
        self.assertIn('完整回覆', full['agent'])

    def test_pane_preview_card_short_sheet_long(self):
        long_agent = '完成。' + ('細節' * 200)
        self.live = {
            ('%1', '123'): {'pane': '%1', 'pid': '123', 'session': 'work', 'window': 'one', 'window_id': '@1', 'path': '/proj/demo'},
        }
        a.record(self.con, 'socket', '%1', '123', 'claude', 'IDLE', 'Response finished; review needed', True)
        a.remember_preview(self.con, 'socket', '%1', '123', user='長任務說明', agent=long_agent)
        row = dict(self.con.execute('SELECT * FROM panes').fetchone())
        card = a.pane_conversation_preview(self.con, row, self.live[('%1', '123')], limit=a.PREVIEW_CHARS)
        full = a.pane_conversation_preview(self.con, row, self.live[('%1', '123')], limit=a.PREVIEW_CHARS_FULL)
        self.assertLessEqual(len(card['agent']), a.PREVIEW_CHARS)
        self.assertTrue(card['agent'].endswith('…'))
        self.assertGreater(len(full['agent']), len(card['agent']))
        self.assertIn('完成。', full['agent'])


    def test_dispatch_preview_requires_live_tracked_pane(self):
        with patch.object(a, 'resolve_socket', return_value='socket'), \
             patch.object(a, 'live_panes') as live_mock:
            for live, error in [({}, 'pane is not live'), (self.live, 'pane is not tracked')]:
                with self.subTest(error=error):
                    live_mock.return_value = live
                    code, body = a.dispatch_preview({'pane': '%1', 'pid': '123'})
                    self.assertEqual(code, 409)
                    self.assertFalse(body['ok'])
                    self.assertEqual(body['error'], error)
        with patch.object(a, 'local_host_name', return_value='home'), \
             patch.object(a, 'load_peers', return_value=[{'name': 'lab', 'url': 'http://lab:7788'}]), \
             patch.object(a, 'urlopen') as urlopen_mock:
            urlopen_mock.return_value.__enter__.return_value.read.return_value = json.dumps({
                'ok': True, 'preview': {'users': ['hi'], 'agent': 'there'},
            }).encode()
            code, body = a.dispatch_preview({'host': 'lab', 'pane': '%9', 'pid': '999'})
        self.assertEqual(code, 200)
        self.assertEqual(body['preview']['agent'], 'there')
        self.assertEqual(urlopen_mock.call_args.args[0].full_url, 'http://lab:7788/api/preview')

    def test_maybe_notify_wait_posts_when_enabled(self):

        cfg = {'enabled': True, 'server': 'https://ntfy.example', 'topic': 'attn-test', 'title': 'tmux-attn'}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'notify.json'
            path.write_text(json.dumps(cfg))
            with patch.dict(os.environ, {'TMUX_ATTN_NOTIFY': str(path)}), \
                 patch.object(a, 'local_host_name', return_value='home'), \
                 patch.object(a, 'urlopen') as urlopen_mock:
                class FakeResp:
                    def read(self): return b''
                urlopen_mock.return_value = FakeResp()
                a.maybe_notify_wait('claude', 'Approval needed', '%1')
            request = urlopen_mock.call_args.args[0]
            self.assertEqual(request.full_url, 'https://ntfy.example/attn-test')
            self.assertEqual(request.data, b'home claude %1: Approval needed')
            self.assertEqual(request.get_header('Title'), 'tmux-attn')

    def test_dispatch_send_local_and_peer(self):
        def fake_send(pane, pid, text, submit=True, images=()):
            if not str(text).strip():
                return 'empty message'
            return None
        with patch.object(a, 'send_to_pane', side_effect=fake_send) as send_mock, \
             patch.object(a, 'local_host_name', return_value='home'):
            code, body = a.dispatch_send({'pane': '%1', 'pid': '123', 'text': 'hello', 'submit': True})
            self.assertEqual(code, 200)
            self.assertTrue(body['ok'])
            send_mock.assert_called_once_with('%1', '123', 'hello', submit=True, images=[])
            code, body = a.dispatch_send({'pane': '%1', 'pid': '123', 'text': '   '})
            self.assertEqual((code, body.get('error')), (409, 'empty message'))
        with patch.object(a, 'local_host_name', return_value='home'), \
             patch.object(a, 'load_peers', return_value=[{'name': 'lab', 'url': 'http://lab:7788'}]), \
             patch.object(a, 'urlopen') as urlopen_mock:
            class FakeResp:
                def __enter__(self): return self
                def __exit__(self, *args): return False
                def read(self): return json.dumps({'ok': True, 'pane': '%9'}).encode()
            urlopen_mock.return_value = FakeResp()
            code, body = a.dispatch_send({'host': 'lab', 'pane': '%9', 'pid': '999', 'text': 'yo'})
            self.assertEqual(code, 200)
            self.assertTrue(body['ok'])
            request = urlopen_mock.call_args.args[0]
            self.assertEqual(request.full_url, 'http://lab:7788/api/send')
            forwarded = json.loads(request.data.decode())
            self.assertTrue(forwarded['_local_only'])
            self.assertEqual(forwarded['pane'], '%9')


    def test_dispatch_kill_and_peer_register(self):
        with patch.object(a, 'kill_pane', return_value=None) as kill_mock, \
             patch.object(a, 'local_host_name', return_value='home'):
            code, body = a.dispatch_kill({'pane': '%1', 'pid': '123'})
            self.assertEqual(code, 200)
            self.assertTrue(body['killed'])
            kill_mock.assert_called_once_with('%1', '123')
        with patch.object(a, 'kill_pane', return_value='pane pid changed'):
            code, body = a.dispatch_kill({'pane': '%1', 'pid': '123'})
            self.assertEqual((code, body.get('error')), (409, 'pane pid changed'))
        with patch.object(a, 'local_host_name', return_value='home'), \
             patch.object(a, 'load_peers', return_value=[{'name': 'lab', 'url': 'http://lab:7788'}]), \
             patch.object(a, 'urlopen') as urlopen_mock:
            class FakeResp:
                def __enter__(self): return self
                def __exit__(self, *args): return False
                def read(self): return json.dumps({'ok': True, 'killed': True}).encode()
            urlopen_mock.return_value = FakeResp()
            code, body = a.dispatch_kill({'host': 'lab', 'pane': '%9', 'pid': '999'})
            self.assertEqual(code, 200)
            self.assertEqual(urlopen_mock.call_args.args[0].full_url, 'http://lab:7788/api/kill')
        peers = Path(self.tmp.name) / 'peers.json'
        with patch.object(a, 'peers_config_path', return_value=peers):
            code, body = a.dispatch_peer_register({'name': 'cicd', 'url': 'http://100.1.2.3:7788'})
            self.assertEqual(code, 200)
            self.assertEqual(body['action'], 'added')
            code, body = a.dispatch_peer_register({'name': 'cicd', 'url': 'http://100.1.2.3:7789'})
            self.assertEqual(body['action'], 'updated')
            saved = json.loads(peers.read_text())
            self.assertEqual(saved['peers'][0]['url'], 'http://100.1.2.3:7789')

    def test_send_to_pane_rejects_pid_mismatch(self):

        live = {('%1', '123'): {'pane': '%1', 'pid': '123', 'session': 'work', 'window': 'one', 'window_id': '@1', 'path': '/proj'}}
        with patch.object(a, 'live_panes', return_value=live), \
             patch.object(a, 'tmux', return_value='999'):
            self.assertEqual(a.send_to_pane('%1', '123', 'hi'), 'pane pid changed')
        with patch.object(a, 'live_panes', return_value=live):
            self.assertEqual(a.send_to_pane('%1', '456', 'hi'), 'pane not found or pid mismatch')
            self.assertEqual(a.send_to_pane('%1', '123', '  '), 'empty message')

    def test_send_to_pane_rejects_control_characters_before_pasting(self):
        # ESC would end the bracketed paste early and reach the TUI as keys (Esc interrupts the agent).
        live = {('%1', '123'): {'pane': '%1', 'pid': '123', 'session': 'work', 'window': 'one', 'window_id': '@1', 'path': '/proj'}}
        with patch.object(a, 'live_panes', return_value=live), patch.object(a, 'tmux', return_value='123'), \
             patch.object(a, 'paste_to_pane') as paste:
            for text in ('hi\x1b[201~\x1b', 'stop\x03', 'csi\x9b2~', 'nul\x00', 'half \ud800'):
                self.assertEqual(a.send_to_pane('%1', '123', text), 'message contains control characters', repr(text))
            paste.assert_not_called()
            # Line breaks and tabs are ordinary message content.
            self.assertIsNone(a.send_to_pane('%1', '123', 'line one\r\nline two\ttabbed'))
            paste.assert_called_once_with('%1', 'line one\r\nline two\ttabbed', None)

    PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 24
    LIVE = {('%1', '123'): {'pane': '%1', 'pid': '123', 'session': 'work', 'window': 'one', 'window_id': '@1', 'path': '/proj'}}

    def test_store_upload_accepts_images_and_issues_server_names(self):
        samples = {
            'png': self.PNG,
            'jpg': b'\xff\xd8\xff\xe0' + b'\x00' * 20,
            'gif': b'GIF89a' + b'\x00' * 20,
            'webp': b'RIFF\x00\x00\x00\x00WEBPVP8 ' + b'\x00' * 12,
        }
        for ext, data in samples.items():
            image_id, error = a.store_upload(data)
            self.assertIsNone(error, ext)
            self.assertRegex(image_id, r'^[0-9a-f]{32}\.' + ext + '$')
            path = a.upload_path(image_id)
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(path.parent, a.STATE / 'uploads')
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((a.STATE / 'uploads').stat().st_mode), 0o700)

    def test_store_upload_rejects_non_images_empty_and_oversize(self):
        for data in (b'<svg xmlns="http://www.w3.org/2000/svg"/>', b'#!/bin/sh\necho hi\n', b'RIFF\x00\x00\x00\x00WAVEfmt '):
            self.assertEqual(a.store_upload(data), (None, 'unsupported image type'))
        self.assertEqual(a.store_upload(b''), (None, 'empty image'))
        with patch.object(a, 'MAX_UPLOAD_BYTES', 16):
            self.assertEqual(a.store_upload(self.PNG), (None, 'image too large'))
        self.assertEqual(list((a.STATE / 'uploads').glob('*')) if (a.STATE / 'uploads').exists() else [], [])

    @staticmethod
    def office_zip(ext):
        import io
        import zipfile
        part, kind = {'docx': ('word/document.xml', 'wordprocessingml.document'),
                      'xlsx': ('xl/workbook.xml', 'spreadsheetml.sheet'),
                      'pptx': ('ppt/presentation.xml', 'presentationml.presentation')}[ext]
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('[Content_Types].xml',
                             '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                             f'<Override PartName="/{part}" ContentType="application/vnd.openxmlformats-officedocument.{kind}.main+xml"/>'
                             '</Types>')
            archive.writestr(part, '<document/>')
        return output.getvalue()

    @staticmethod
    def office_compound(ext):
        # Header, FAT and directory sectors; the empty named stream needs no data sector.
        header = bytearray(512)
        header[:8] = bytes.fromhex('d0cf11e0a1b11ae1')
        header[26:32] = bytes.fromhex('0300feff0900')
        header[32:34] = bytes.fromhex('0600')
        header[44:48] = (1).to_bytes(4, 'little')
        header[48:52] = (1).to_bytes(4, 'little')
        header[56:60] = (4096).to_bytes(4, 'little')
        header[60:64] = (0xfffffffe).to_bytes(4, 'little')
        header[68:72] = (0xfffffffe).to_bytes(4, 'little')
        header[76:512] = b'\xff' * 436
        header[76:80] = bytes(4)
        fat = bytearray(b'\xff' * 512)
        fat[:8] = bytes.fromhex('fdfffffffeffffff')
        directory = bytearray(512)
        for offset, name, kind in ((0, 'Root Entry', 5), (128, {'doc': 'WordDocument', 'xls': 'Workbook', 'ppt': 'PowerPoint Document'}[ext], 2)):
            encoded = (name + '\0').encode('utf-16le')
            directory[offset:offset + len(encoded)] = encoded
            directory[offset + 64:offset + 66] = len(encoded).to_bytes(2, 'little')
            directory[offset + 66] = kind
            directory[offset + 116:offset + 120] = (0xfffffffe).to_bytes(4, 'little')
        return bytes(header + fat + directory)

    def test_text_uploads_validate_encoding_content_and_extension(self):
        for ext in a.TEXT_EXTENSIONS:
            for raw in ('一般文字\nhello\tworld'.encode(), '文字'.encode('utf-16')):
                with self.subTest(ext=ext, raw=raw):
                    file_id, error = a.store_upload(raw, 'NOTES.' + ext.upper())
                    self.assertIsNone(error)
                    self.assertEqual(a.upload_path(file_id).read_bytes(), raw)
                    self.assertTrue(file_id.endswith('.' + ext))
        for raw, name in ((b'MZ\x00binary', 'notes.txt'), (b'\xff\xff', 'notes.md'),
                          (b'hello', 'script.exe'), (self.PNG, 'fake.csv'), (b'hi', 'notes.pdf')):
            self.assertEqual(a.store_upload(raw, name), (None, 'unsupported file type'))
        self.assertEqual(a.store_upload(b'', 'a.txt'), (None, 'empty file'))
        with patch.object(a, 'MAX_UPLOAD_BYTES', 2):
            self.assertEqual(a.store_upload(b'hello', 'a.txt'), (None, 'file too large'))

    def test_office_uploads_require_matching_container_contents(self):
        for ext in a.OFFICE_EXTENSIONS:
            raw = self.office_zip(ext) if ext.endswith('x') else self.office_compound(ext)
            with self.subTest(ext=ext):
                file_id, error = a.store_upload(raw, 'report.' + ext)
                self.assertIsNone(error)
                self.assertEqual(a.upload_path(file_id).read_bytes(), raw)
                self.assertEqual(a.store_upload(raw, 'fake.' + ('xlsx' if ext != 'xlsx' else 'docx')),
                                 (None, 'unsupported file type'))
        for raw in (b'PK\x03\x04not a zip', b'plain text', self.PNG):
            self.assertEqual(a.store_upload(raw, 'fake.docx'), (None, 'unsupported file type'))
        # A fabricated directory entry outside the declared chain is rejected.
        fake = bytearray(self.office_compound('doc'))
        fake[48:52] = (1000).to_bytes(4, 'little')
        self.assertEqual(a.store_upload(fake, 'fake.doc'), (None, 'unsupported file type'))
        fake = bytearray(self.office_compound('doc'))
        fake[516:520] = (1).to_bytes(4, 'little')  # cyclic directory chain
        self.assertEqual(a.store_upload(fake, 'fake.doc'), (None, 'unsupported file type'))

    def test_corrupt_office_deflate_returns_validation_error(self):
        raw = bytearray(self.office_zip('docx'))
        offset = 30 + len('[Content_Types].xml')
        raw[offset:offset + 2] = b'\xff\xff'
        self.assertEqual(a.store_upload(raw, 'broken.docx'), (None, 'unsupported file type'))

    def test_office_manifest_rejects_unexpected_zip_compression(self):
        raw = bytearray(self.office_zip('docx'))
        # Mark both local header and central directory as LZMA, leaving invalid properties.
        raw[8:10] = (14).to_bytes(2, 'little')
        offset = raw.index(b'PK\x01\x02')
        raw[offset + 10:offset + 12] = (14).to_bytes(2, 'little')
        self.assertEqual(a.store_upload(raw, 'broken.docx'), (None, 'unsupported file type'))

    def test_document_upload_dispatch_local_and_peer(self):
        payload = {'pane': '%1', 'pid': '123', 'filename': '筆記.txt', 'data': base64.b64encode(b'hello').decode()}
        with patch.object(a, 'live_panes', return_value=self.LIVE):
            code, body = a.dispatch_upload(payload)
            self.assertEqual(code, 200)
            self.assertEqual(a.upload_path(body['id']).read_bytes(), b'hello')
            for name in (None, 5, 'x' * 256):
                self.assertEqual(a.dispatch_upload(payload | {'filename': name})[0], 400)
            with patch.object(a, 'MAX_UPLOAD_BYTES', 2):
                self.assertEqual(a.dispatch_upload(payload)[0], 413)
        class Reply:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps({'ok': True, 'id': 'a' * 32 + '.txt'}).encode()
        with patch.object(a, 'local_host_name', return_value='home'), \
             patch.object(a, 'load_peers', return_value=[{'name': 'lab', 'url': 'http://lab:7788'}]), \
             patch.object(a, 'urlopen', return_value=Reply()) as request:
            self.assertEqual(a.dispatch_upload(payload | {'host': 'lab'})[0], 200)
            self.assertEqual(json.loads(request.call_args.args[0].data)['filename'], '筆記.txt')
            self.assertEqual(a.dispatch_send({'host': 'lab', 'pane': '%1', 'pid': '123', 'text': '', 'files': ['a' * 32 + '.txt']})[0], 200)
            self.assertEqual(json.loads(request.call_args.args[0].data)['files'], ['a' * 32 + '.txt'])
            self.assertEqual(request.call_args.kwargs['timeout'], a.PEER_UPLOAD_TIMEOUT)

    def test_document_send_validation_and_all_paths_checked_before_paste(self):
        file_id, _ = a.store_upload(b'hello', 'notes.txt')
        image_id, _ = a.store_upload(self.PNG)
        with patch.object(a, 'send_to_pane', return_value=None) as send:
            self.assertEqual(a.dispatch_send({'pane': '%1', 'pid': '123', 'text': '', 'files': [file_id]})[0], 200)
            self.assertEqual(send.call_args.kwargs['files'], [file_id])
            for files in ('bad', [None], ['../notes.txt'], [image_id], [file_id] * 5):
                self.assertEqual(a.dispatch_send({'pane': '%1', 'pid': '123', 'text': '', 'files': files})[0], 400)
            self.assertEqual(a.dispatch_send({'pane': '%1', 'pid': '123', 'text': '', 'images': [image_id] * 3, 'files': [file_id] * 2})[0], 400)
        with patch.object(a, 'live_panes', return_value=self.LIVE), patch.object(a, 'tmux', return_value='123'), \
             patch.object(a, 'paste_to_pane') as paste, patch.object(a.time, 'sleep'):
            self.assertIsNone(a.send_to_pane('%1', '123', '', files=[file_id]))
            paste.assert_called_once_with('%1', shlex.quote(str(a.upload_path(file_id))), None)
            paste.reset_mock()
            self.assertEqual(a.send_to_pane('%1', '123', 'hello', images=[image_id], files=['b' * 32 + '.txt']), 'file not found')
            paste.assert_not_called()

    def test_upload_path_only_resolves_server_issued_ids(self):
        image_id, _ = a.store_upload(self.PNG)
        self.assertIsNotNone(a.upload_path(image_id))
        for bad in ('../' + image_id, '/etc/passwd', 'abc.png', image_id.upper(), image_id + '/x', image_id + '\n', '', None, 7,
                    '0' * 32 + '.png', '0' * 32 + '.svg'):
            self.assertIsNone(a.upload_path(bad), bad)

    def test_prune_uploads_drops_expired_and_keeps_newest_within_cap(self):
        now = time.time()
        ids = []
        for age in (30 * 86400, 10 * 86400, 300, 200, 100):
            image_id, _ = a.store_upload(self.PNG)
            os.utime(a.upload_path(image_id), (now - age, now - age))
            ids.append(image_id)
        with patch.object(a, 'UPLOAD_MAX_FILES', 2):
            a.prune_uploads(now=now)
        kept = [i for i in ids if a.upload_path(i)]
        self.assertEqual(kept, ids[3:])

    def test_dispatch_upload_stores_on_live_pane_only(self):
        encoded = base64.b64encode(self.PNG).decode()
        base = {'pane': '%1', 'pid': '123', 'data': encoded, 'name': '../../evil.sh'}
        self.assertEqual(a.dispatch_upload('x')[0], 400)
        self.assertEqual(a.dispatch_upload({'pane': '%1', 'pid': '123'})[0], 400)
        with patch.object(a, 'live_panes', return_value=self.LIVE), patch.object(a, 'local_host_name', return_value='home'):
            code, body = a.dispatch_upload(base)
            self.assertEqual(code, 200)
            self.assertTrue(body['ok'])
            self.assertEqual(body['bytes'], len(self.PNG))
            self.assertEqual(a.upload_path(body['id']).read_bytes(), self.PNG)
            self.assertNotIn('evil', body['id'])
            self.assertEqual(a.dispatch_upload(base | {'data': '***not base64***'})[0], 400)
            code, body = a.dispatch_upload(base | {'data': base64.b64encode(b'MZ-not-an-image').decode()})
            self.assertEqual((code, body['error']), (400, 'unsupported image type'))
            with patch.object(a, 'MAX_UPLOAD_BYTES', 8):
                self.assertEqual(a.dispatch_upload(base)[0], 413)
            code, body = a.dispatch_upload(base | {'pid': '999'})
            self.assertEqual((code, body['error']), (409, 'pane not found or pid mismatch'))

    def test_dispatch_upload_forwards_to_peer_with_longer_timeout(self):
        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return json.dumps({'ok': True, 'id': 'f' * 32 + '.png'}).encode()
        with patch.object(a, 'local_host_name', return_value='home'), \
             patch.object(a, 'load_peers', return_value=[{'name': 'lab', 'url': 'http://lab:7788'}]), \
             patch.object(a, 'urlopen', return_value=FakeResp()) as urlopen_mock:
            code, body = a.dispatch_upload({'host': 'lab', 'pane': '%9', 'pid': '999', 'data': 'AAAA'})
            self.assertEqual((code, body['id']), (200, 'f' * 32 + '.png'))
            request = urlopen_mock.call_args.args[0]
            self.assertEqual(request.full_url, 'http://lab:7788/api/upload')
            self.assertEqual(json.loads(request.data.decode())['data'], 'AAAA')
            self.assertGreaterEqual(urlopen_mock.call_args.kwargs['timeout'], 15)
            code, body = a.dispatch_upload({'host': 'nowhere', 'pane': '%9', 'pid': '999', 'data': 'AAAA'})
            self.assertEqual(code, 404)

    def test_dispatch_send_validates_and_forwards_image_ids(self):
        image_id = 'a' * 32 + '.png'
        with patch.object(a, 'send_to_pane', return_value=None) as send_mock, patch.object(a, 'local_host_name', return_value='home'):
            code, _ = a.dispatch_send({'pane': '%1', 'pid': '123', 'text': '', 'images': [image_id]})
            self.assertEqual(code, 200)
            send_mock.assert_called_once_with('%1', '123', '', submit=True, images=[image_id])
            send_mock.reset_mock()
            for bad in (['../x.png'], [image_id] * (a.MAX_IMAGES + 1), 'a.png', [7], [None], [image_id + '\n']):
                code, body = a.dispatch_send({'pane': '%1', 'pid': '123', 'text': 'x', 'images': bad})
                self.assertEqual((code, body['error']), (400, 'invalid images'), bad)
            send_mock.assert_not_called()
        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return json.dumps({'ok': True}).encode()
        with patch.object(a, 'local_host_name', return_value='home'), \
             patch.object(a, 'load_peers', return_value=[{'name': 'lab', 'url': 'http://lab:7788'}]), \
             patch.object(a, 'urlopen', return_value=FakeResp()) as urlopen_mock:
            a.dispatch_send({'host': 'lab', 'pane': '%9', 'pid': '999', 'text': 'yo', 'images': [image_id]})
            self.assertEqual(json.loads(urlopen_mock.call_args.args[0].data.decode())['images'], [image_id])

    def _send_events(self, text, ids, submit=True):
        events = []
        def fake_run(cmd, input=None, **kwargs):
            events.append(('load', input.decode()))
        def fake_tmux(*args, **kwargs):
            if args[0] == 'display-message':
                return '123'
            if args[0] == 'paste-buffer':
                events.append(('paste', '-p' in args))
            elif args[0] == 'send-keys':
                events.append(('key', args[-1]))
            return ''
        with patch.object(a, 'live_panes', return_value=self.LIVE), patch.object(a, 'tmux', side_effect=fake_tmux), \
             patch.object(a.subprocess, 'run', side_effect=fake_run), patch.object(a, 'IMAGE_SETTLE_SECONDS', 0):
            error = a.send_to_pane('%1', '123', text, submit=submit, images=ids)
        return error, events

    def test_send_to_pane_pastes_text_then_each_image_path_alone_then_enter(self):
        first, _ = a.store_upload(self.PNG)
        second, _ = a.store_upload(self.PNG)
        error, events = self._send_events('look at these', [first, second])
        self.assertIsNone(error)
        self.assertEqual(events, [
            ('load', 'look at these'), ('paste', True), ('key', 'Space'),
            ('load', str(a.upload_path(first))), ('paste', True), ('key', 'Space'),
            ('load', str(a.upload_path(second))), ('paste', True), ('key', 'Enter'),
        ])
        # Each path is its own paste: Codex only attaches a paste that is exactly an image path.
        self.assertTrue(all(Path(value).is_absolute() for kind, value in events if kind == 'load' and value != 'look at these'))

    def test_send_to_pane_allows_image_only_and_reports_missing_image(self):
        image_id, _ = a.store_upload(self.PNG)
        error, events = self._send_events('   ', [image_id])
        self.assertIsNone(error)
        self.assertEqual([kind for kind, _ in events], ['load', 'paste', 'key'])
        self.assertEqual(events[0], ('load', str(a.upload_path(image_id))))
        self.assertEqual(events[-1], ('key', 'Enter'))
        error, events = self._send_events('hi', ['b' * 32 + '.png'])
        self.assertEqual((error, events), ('image not found', []))
        error, events = self._send_events('hi', [image_id], submit=False)
        self.assertNotIn(('key', 'Enter'), events)

    def test_send_to_pane_text_only_sequence_is_unchanged(self):
        error, events = self._send_events('hello', [])
        self.assertEqual((error, events), (None, [('load', 'hello'), ('paste', True), ('key', 'Enter')]))

    def test_web_upload_endpoint_and_request_size_limit(self):
        handler = a.make_web_handler()

        class Quiet(handler):
            def log_message(self, format, *args):
                pass

        def post(port, path, payload):
            request = Request(f'http://127.0.0.1:{port}{path}', data=json.dumps(payload).encode(),
                              headers={'Content-Type': 'application/json'}, method='POST')
            return urlopen(request, timeout=2)

        server = a.ThreadingHTTPServer(('127.0.0.1', 0), Quiet)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            port = server.server_address[1]
            with patch.object(a, 'live_panes', return_value=self.LIVE), patch.object(a, 'load_peers', return_value=[]):
                body = json.loads(post(port, '/api/upload', {
                    'pane': '%1', 'pid': '123', 'data': base64.b64encode(self.PNG).decode()}).read())
                self.assertTrue(body['ok'])
                self.assertEqual(a.upload_path(body['id']).read_bytes(), self.PNG)
                shown = {'role': 'agent', 'text': 'full', 'chars': 4, 'truncated': False}
                with patch.object(a, 'full_message_for_pane', return_value=(shown, None)) as full_mock:
                    body = json.loads(post(port, '/api/message', {'pane': '%1', 'pid': '123', 'role': 'agent'}).read())
                self.assertEqual((body['ok'], body['text']), (True, 'full'))
                self.assertEqual(full_mock.call_args.args[1:], ('%1', '123', 'agent', 0, ''))
                with patch.object(a, 'MAX_BODY_BYTES', 64), self.assertRaises(HTTPError) as caught:
                    post(port, '/api/send', {'pane': '%1', 'pid': '123', 'text': 'x' * 200})
                self.assertEqual(caught.exception.code, 413)
        finally:
            server.shutdown()
            server.server_close()

    def _message_fixture(self, users, agent_text, cached_agent=None):
        path = Path(self.tmp.name) / 'session.jsonl'
        rows = [{'type': 'user', 'message': {'content': text}} for text in users]
        rows.append({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': agent_text}]}})
        path.write_text('\n'.join(json.dumps(row) for row in rows))
        a.record(self.con, 'socket', '%1', '123', 'claude', 'IDLE', 'Finished', True)
        with self.con:
            self.con.execute('UPDATE panes SET transcript_path=?, preview_agent=? WHERE pane=?',
                             (str(path), cached_agent or '', '%1'))
        return path

    def _full_message(self, role, index=0, sig=''):
        with patch.object(a, 'resolve_socket', return_value='socket'), patch.object(a, 'live_panes', return_value=self.LIVE):
            return a.full_message_for_pane(self.con, '%1', '123', role, index, sig)

    LONG_REPORT = '\n'.join(f'line {n:05d} ' + 'x' * 40 for n in range(300))

    def test_full_message_returns_text_beyond_the_4000_char_preview_clip(self):
        self._message_fixture(['first', 'second', 'third', 'fourth'], self.LONG_REPORT)
        shown = a.clip_preview(self.LONG_REPORT, a.PREVIEW_CHARS_FULL)
        self.assertLess(len(shown), len(self.LONG_REPORT))
        result, error = self._full_message('agent', sig=shown[:200])
        self.assertIsNone(error)
        self.assertEqual(result['text'], self.LONG_REPORT)
        self.assertEqual((result['role'], result['chars'], result['truncated'], result['partial']), ('agent', len(self.LONG_REPORT), False, False))

    def test_full_message_indexes_the_last_three_user_messages_and_checks_the_signature(self):
        self._message_fixture(['first', 'second prompt ' + 'y' * 5000, 'third', 'fourth'], 'done')
        result, error = self._full_message('user', 0, sig='second prompt ')
        self.assertIsNone(error)
        self.assertEqual(len(result['text']), len('second prompt ') + 5000)
        self.assertEqual(self._full_message('user', 2, sig='fourth')[0]['text'], 'fourth')
        # The sheet showed a different message at this slot than the transcript has now.
        self.assertEqual(self._full_message('user', 0, sig='first'), (None, 'content changed; refresh preview'))
        self.assertEqual(self._full_message('user', 3)[1], 'content changed; refresh preview')
        self.assertEqual(self._full_message('user', 1)[0]['text'], 'third')  # no signature: positional

    def test_full_message_falls_back_to_the_hook_cache_when_the_transcript_lags(self):
        self._message_fixture(['go'], 'older transcript answer', cached_agent='Final answer from the hook')
        result, error = self._full_message('agent', sig='Final answer')
        self.assertIsNone(error)
        self.assertEqual(result['text'], 'Final answer from the hook')

    def test_full_message_is_capped_and_flags_truncation(self):
        self._message_fixture(['go'], 'z' * 500)
        with patch.object(a, 'MAX_MESSAGE_CHARS', 100):
            result, error = self._full_message('agent')
        self.assertIsNone(error)
        self.assertEqual((len(result['text']), result['chars'], result['truncated']), (100, 500, True))

    def test_full_message_rejects_unknown_roles_and_panes(self):
        self._message_fixture(['go'], 'done')
        self.assertEqual(self._full_message('system')[1], 'role must be user or agent')
        with patch.object(a, 'resolve_socket', return_value='socket'), patch.object(a, 'live_panes', return_value={}):
            self.assertEqual(a.full_message_for_pane(self.con, '%1', '123', 'agent', 0, ''), (None, 'pane is not live'))
        with patch.object(a, 'resolve_socket', return_value='socket'), patch.object(a, 'live_panes', return_value=self.LIVE):
            self.assertEqual(a.full_message_for_pane(self.con, '%1', '123', 'agent', 0, '')[1], None)
            self.con.execute('DELETE FROM panes')
            self.con.commit()
            self.assertEqual(a.full_message_for_pane(self.con, '%1', '123', 'agent', 0, ''), (None, 'pane is not tracked'))

    def test_dispatch_message_validates_input_and_forwards_to_peer(self):
        self.assertEqual(a.dispatch_message('x')[0], 400)
        for bad in ({'pane': '%1'}, {'pane': '%1', 'pid': '1', 'role': 'bot'},
                    {'pane': '%1', 'pid': '1', 'role': 'user', 'index': -1},
                    {'pane': '%1', 'pid': '1', 'role': 'user', 'index': 'x'},
                    {'pane': '%1', 'pid': '1', 'role': 'user', 'index': True},
                    {'pane': '%1', 'pid': '1', 'role': 'agent', 'sig': 7}):
            self.assertEqual(a.dispatch_message(bad)[0], 400, bad)
        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return json.dumps({'ok': True, 'text': 'hi', 'role': 'agent', 'chars': 2}).encode()
        with patch.object(a, 'local_host_name', return_value='home'), \
             patch.object(a, 'load_peers', return_value=[{'name': 'lab', 'url': 'http://lab:7788'}]), \
             patch.object(a, 'urlopen', return_value=FakeResp()) as urlopen_mock:
            code, body = a.dispatch_message({'host': 'lab', 'pane': '%9', 'pid': '9', 'role': 'agent', 'sig': 'ab'})
            self.assertEqual((code, body['text']), (200, 'hi'))
            request = urlopen_mock.call_args.args[0]
            self.assertEqual(request.full_url, 'http://lab:7788/api/message')
            forwarded = json.loads(request.data.decode())
            self.assertEqual((forwarded['role'], forwarded['sig'], forwarded['_local_only']), ('agent', 'ab', True))
            self.assertNotIn('host', forwarded)

    def test_dispatch_message_maps_local_errors_to_http_codes(self):
        self._message_fixture(['go'], 'done')
        with patch.object(a, 'resolve_socket', return_value='socket'), patch.object(a, 'live_panes', return_value=self.LIVE), \
             patch.object(a, 'local_host_name', return_value='home'):
            code, body = a.dispatch_message({'pane': '%1', 'pid': '123', 'role': 'agent', 'sig': 'done'})
            self.assertEqual((code, body['ok'], body['text'], body['host']), (200, True, 'done', 'home'))
            code, body = a.dispatch_message({'pane': '%1', 'pid': '123', 'role': 'agent', 'sig': 'nope'})
            self.assertEqual((code, body['error']), (409, 'content changed; refresh preview'))

    def test_fleet_only_mode_requires_a_host_on_every_action_route(self):
        handler = a.make_web_handler(fleet_only=True)

        class Quiet(handler):
            def log_message(self, format, *args):
                pass

        server = a.ThreadingHTTPServer(('127.0.0.1', 0), Quiet)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            port = server.server_address[1]
            for path in ('/api/kill', '/api/send', '/api/upload', '/api/preview', '/api/message', '/api/ack'):
                request = Request(f'http://127.0.0.1:{port}{path}', data=b'{"pane": "%1", "pid": "1"}',
                                  headers={'Content-Type': 'application/json'}, method='POST')
                with self.assertRaises(HTTPError, msg=path) as caught:
                    urlopen(request, timeout=2)
                self.assertEqual(caught.exception.code, 400, path)
                self.assertEqual(json.loads(caught.exception.read())['error'], 'host required in fleet mode', path)
        finally:
            server.shutdown()
            server.server_close()

    def test_prune_uploads_also_enforces_a_total_byte_cap(self):
        now = time.time()
        ids = []
        for age in (300, 200, 100):
            image_id, _ = a.store_upload(self.PNG)
            os.utime(a.upload_path(image_id), (now - age, now - age))
            ids.append(image_id)
        with patch.object(a, 'UPLOAD_MAX_TOTAL_BYTES', len(self.PNG) * 2 + 1):
            a.prune_uploads(now=now)
        self.assertEqual([i for i in ids if a.upload_path(i)], ids[1:])

    def test_store_upload_removes_a_partial_file_when_the_write_fails(self):
        class Broken:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def write(self, data): raise OSError(28, 'No space left on device')
        with patch.object(a.os, 'fdopen', return_value=Broken()), self.assertRaises(OSError):
            a.store_upload(self.PNG)
        self.assertEqual(list((a.STATE / 'uploads').glob('*')), [])

    def test_peer_replies_are_reduced_to_typed_fields(self):
        def reply(payload):
            class FakeResp:
                def __enter__(self): return self
                def __exit__(self, *args): return False
                def read(self): return json.dumps(payload).encode()
            return FakeResp()
        peers = [{'name': 'lab', 'url': 'http://lab:7788'}]
        good = {'ok': True, 'role': 'agent', 'text': 'hi', 'chars': 2, 'truncated': False, 'partial': False}
        with patch.object(a, 'local_host_name', return_value='home'), patch.object(a, 'load_peers', return_value=peers):
            with patch.object(a, 'urlopen', return_value=reply(good | {'extra': '<script>'})):
                code, body = a.dispatch_message({'host': 'lab', 'pane': '%9', 'pid': '9', 'role': 'agent'})
            self.assertEqual((code, body), (200, good))
            for hostile in ({'chars': '<img src=x onerror=alert(1)>'}, {'text': {'length': '<b>'}}, {'role': 'root'}, {'chars': True}):
                with patch.object(a, 'urlopen', return_value=reply(good | hostile)):
                    code, body = a.dispatch_message({'host': 'lab', 'pane': '%9', 'pid': '9', 'role': 'agent'})
                self.assertEqual((code, body['error']), (502, 'peer message failed: bad reply'), hostile)
            with patch.object(a, 'urlopen', return_value=reply({'ok': True, 'id': '../../etc/passwd', 'bytes': 3})):
                code, body = a.dispatch_upload({'host': 'lab', 'pane': '%9', 'pid': '9', 'data': 'AAAA'})
            self.assertEqual((code, body['error']), (502, 'peer upload failed: bad reply'))
            with patch.object(a, 'urlopen', return_value=reply({'ok': True, 'id': 'c' * 32 + '.png', 'x': 1})):
                code, body = a.dispatch_upload({'host': 'lab', 'pane': '%9', 'pid': '9', 'data': 'AAAA'})
            self.assertEqual((code, body), (200, {'ok': True, 'id': 'c' * 32 + '.png'}))

    def test_peer_http_errors_pass_through_for_send_upload_and_message(self):
        def http_error(code, payload):
            return HTTPError('http://lab:7788/x', code, 'err', {}, io.BytesIO(json.dumps(payload).encode()))
        peers = [{'name': 'lab', 'url': 'http://lab:7788'}]
        calls = [
            (a.dispatch_send, {'host': 'lab', 'pane': '%9', 'pid': '9', 'text': 'x'}, 409, 'image not found'),
            (a.dispatch_upload, {'host': 'lab', 'pane': '%9', 'pid': '9', 'data': 'AAAA'}, 413, 'image too large'),
            (a.dispatch_message, {'host': 'lab', 'pane': '%9', 'pid': '9', 'role': 'agent'}, 409, 'content changed; refresh preview'),
        ]
        with patch.object(a, 'local_host_name', return_value='home'), patch.object(a, 'load_peers', return_value=peers):
            for dispatch, payload, code, message in calls:
                with patch.object(a, 'urlopen', side_effect=http_error(code, {'ok': False, 'error': message})):
                    self.assertEqual(dispatch(payload), (code, {'ok': False, 'error': message}), dispatch.__name__)

    def test_a_peer_without_the_new_routes_is_reported_as_outdated(self):
        peers = [{'name': 'lab', 'url': 'http://lab:7788'}]
        def missing_route(*args, **kwargs):
            raise HTTPError('http://lab:7788/x', 404, 'Not Found', {}, io.BytesIO(b'{"ok": false, "error": "not found"}'))
        with patch.object(a, 'local_host_name', return_value='home'), patch.object(a, 'load_peers', return_value=peers), \
             patch.object(a, 'urlopen', side_effect=missing_route):
            for dispatch, payload in (
                    (a.dispatch_message, {'host': 'lab', 'pane': '%9', 'pid': '9', 'role': 'agent'}),
                    (a.dispatch_upload, {'host': 'lab', 'pane': '%9', 'pid': '9', 'data': 'AAAA'})):
                code, body = dispatch(payload)
                self.assertEqual((code, body), (502, {'ok': False, 'error': a.PEER_OUTDATED}), dispatch.__name__)

    def test_peer_send_allows_extra_time_when_images_are_attached(self):
        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return json.dumps({'ok': True}).encode()
        peers = [{'name': 'lab', 'url': 'http://lab:7788'}]
        with patch.object(a, 'local_host_name', return_value='home'), patch.object(a, 'load_peers', return_value=peers), \
             patch.object(a, 'urlopen', return_value=FakeResp()) as urlopen_mock:
            a.dispatch_send({'host': 'lab', 'pane': '%9', 'pid': '9', 'text': 'plain'})
            self.assertEqual(urlopen_mock.call_args.kwargs['timeout'], 3)
            a.dispatch_send({'host': 'lab', 'pane': '%9', 'pid': '9', 'text': 'pics', 'images': ['d' * 32 + '.png']})
            self.assertGreaterEqual(urlopen_mock.call_args.kwargs['timeout'], 15)

    def test_each_action_route_reaches_its_own_dispatcher_and_unknown_paths_404(self):
        names = {'/api/kill': 'dispatch_kill', '/api/send': 'dispatch_send', '/api/upload': 'dispatch_upload',
                 '/api/preview': 'dispatch_preview', '/api/message': 'dispatch_message', '/api/ack': 'dispatch_ack'}
        patches = [patch.object(a, name, return_value=(200, {'ok': True, 'handler': name})) for name in names.values()]
        for active in patches:
            active.start()
            self.addCleanup(active.stop)
        handler = a.make_web_handler()

        class Quiet(handler):
            def log_message(self, format, *args):
                pass

        server = a.ThreadingHTTPServer(('127.0.0.1', 0), Quiet)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            port = server.server_address[1]
            for path, name in names.items():
                request = Request(f'http://127.0.0.1:{port}{path}', data=b'{}', method='POST')
                self.assertEqual(json.loads(urlopen(request, timeout=2).read())['handler'], name, path)
            with self.assertRaises(HTTPError) as caught:
                urlopen(Request(f'http://127.0.0.1:{port}/api/nope', data=b'{}', method='POST'), timeout=2)
            self.assertEqual(caught.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()

    def test_post_body_limits_differ_for_uploads_and_a_bad_length_is_a_400(self):
        handler = a.make_web_handler()

        class Quiet(handler):
            def log_message(self, format, *args):
                pass

        server = a.ThreadingHTTPServer(('127.0.0.1', 0), Quiet)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        def raw_post(path, length_header, body=b''):
            conn = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=2)
            conn.putrequest('POST', path)
            conn.putheader('Content-Type', 'application/json')
            conn.putheader('Content-Length', length_header)
            conn.endheaders(body)
            response = conn.getresponse()
            return response.status, json.loads(response.read() or b'{}')

        try:
            self.assertEqual(raw_post('/api/send', 'abc')[0], 400)
            self.assertEqual(raw_post('/api/send', '-5')[0], 400)
            self.assertGreater(a.MAX_UPLOAD_BODY_BYTES, a.MAX_BODY_BYTES)
            with patch.object(a, 'MAX_BODY_BYTES', 64), patch.object(a, 'MAX_UPLOAD_BODY_BYTES', 4096), \
                 patch.object(a, 'live_panes', return_value={}):
                body = json.dumps({'pane': '%1', 'pid': '1', 'data': 'A' * 500}).encode()
                self.assertEqual(raw_post('/api/send', str(len(body)), body)[0], 413)
                self.assertEqual(raw_post('/api/upload', str(len(body)), body)[0], 409)  # accepted, then no such pane
                big = b'x' * 5000
                self.assertEqual(raw_post('/api/upload', str(len(big)), big)[0], 413)
        finally:
            server.shutdown()
            server.server_close()

    def test_full_message_matches_what_the_sheet_showed_when_the_hook_cache_is_authoritative(self):
        long_answer = '\n'.join(f'row {n:05d} ' + 'q' * 30 for n in range(400))
        self._message_fixture(['go'], 'older transcript answer')
        a.remember_preview(self.con, 'socket', '%1', '123', user='go', agent=long_answer)
        with patch.object(a, 'resolve_socket', return_value='socket'), patch.object(a, 'live_panes', return_value=self.LIVE):
            snapshot, error = a.local_preview_for_pane(self.con, '%1', '123', snapshot=True)
        self.assertIsNone(error)
        shown = snapshot['preview']['agent']
        self.assertTrue(shown.endswith('…'))
        result, error = self._full_message('agent', sig=shown[:200])
        self.assertIsNone(error)
        self.assertEqual(result['text'], shown)
        self.assertEqual((result['truncated'], result['partial']), (True, True))

    def test_full_message_user_index_skips_messages_that_normalise_to_nothing(self):
        self._message_fixture(['go', '\x00\x01\x02', 'real one'], 'done')
        self.assertEqual(self._full_message('user', 1, sig='real')[0]['text'], 'real one')

    def test_web_api_smoke(self):
        a.record(self.con, 'socket', '%1', '123', 'claude', 'WAIT', 'Approval needed')
        live = {('%1', '123'): {'pane': '%1', 'pid': '123', 'session': 'work', 'window': 'one', 'window_id': '@1', 'path': '/proj/demo'}}
        handler = a.make_web_handler()

        class Quiet(handler):
            def log_message(self, format, *args):
                pass

        server = a.ThreadingHTTPServer(('127.0.0.1', 0), Quiet)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            with patch.object(a, 'resolve_socket', return_value='socket'), patch.object(a, 'live_panes', return_value=live), patch.object(a, 'load_peers', return_value=[]):
                health = json.loads(urlopen(f'http://127.0.0.1:{port}/api/health', timeout=2).read())
                agents = json.loads(urlopen(f'http://127.0.0.1:{port}/api/agents', timeout=2).read())
                fleet = json.loads(urlopen(f'http://127.0.0.1:{port}/api/fleet', timeout=2).read())
                page = urlopen(f'http://127.0.0.1:{port}/', timeout=2).read().decode()
            self.assertTrue(health['ok'])
            self.assertTrue(agents['ok'])
            self.assertEqual(agents['summary']['wait'], 1)
            self.assertEqual(agents['agents'][0]['emoji'], '🙋')
            self.assertTrue(fleet['ok'])
            self.assertEqual(fleet['agents'][0]['host'], a.local_host_name())
            self.assertIn('/api/fleet', page)
            self.assertIn('/api/send', page)
            self.assertIn('/api/preview', page)
            self.assertIn('id="group-${key}"', page)
            self.assertIn('jumpToGroup', page)
            self.assertIn('等待回覆', page)
            self.assertIn('sheet-form', page)
            self.assertIn('sheet-attach', page)
            self.assertIn('/api/upload', page)
            self.assertNotIn('sheet-mic', page)
        finally:
            server.shutdown()
            server.server_close()

    def test_fleet_merges_peer_and_marks_offline(self):
        a.record(self.con, 'socket', '%1', '123', 'claude', 'WAIT', 'Approval needed')
        live = {('%1', '123'): {'pane': '%1', 'pid': '123', 'session': 'work', 'window': 'one', 'window_id': '@1', 'path': '/proj/demo', 'command': 'claude'}}
        peer_payload = {
            'ok': True,
            'summary': {'wait': 0, 'review': 0, 'running': 1, 'idle': 0, 'tracked': 1, 'untracked': 0},
            'agents': [{
                'pane': '%9', 'pid': '999', 'session': 'x', 'window': 'y', 'window_id': '@9',
                'agent': 'codex', 'cli': 'codex', 'command': 'codex', 'tracked': True,
                'project': 'other', 'path': '/other', 'state': 'RUNNING', 'pending': False,
                'emoji': '🏃', 'reason': '正在工作', 'updated': time.time(), 'age': '1s',
            }],
        }
        with patch.object(a, 'resolve_socket', return_value='socket'), \
             patch.object(a, 'live_panes', return_value=live), \
             patch.object(a, 'local_host_name', return_value='home'), \
             patch.object(a, 'load_peers', return_value=[
                 {'name': 'lab', 'url': 'http://lab:7788'},
                 {'name': 'down', 'url': 'http://down:7788'},
             ]), \
             patch.object(a, 'fetch_peer_agents', side_effect=lambda url, timeout=1.5: peer_payload if 'lab' in url else (_ for _ in ()).throw(RuntimeError('down'))):
            fleet = a.fleet_payload(include_local=True)
        self.assertEqual(fleet['summary']['wait'], 1)
        self.assertEqual(fleet['summary']['running'], 1)
        self.assertEqual({row['host'] for row in fleet['agents']}, {'home', 'lab'})
        self.assertEqual(fleet['agents'][0]['emoji'], '🙋')
        self.assertEqual(fleet['agents'][0]['host'], 'home')
        status = {row['name']: row['ok'] for row in fleet['hosts']}
        self.assertEqual(status, {'home': True, 'lab': True, 'down': False})

    def test_fleet_retains_stale_peer_on_transient_failure_until_ttl_expires(self):
        a._FLEET_HOST_CACHE.clear()
        peer_payload = {
            'ok': True,
            'summary': {'wait': 0, 'review': 0, 'running': 1, 'idle': 0, 'tracked': 1, 'untracked': 0},
            'agents': [{
                'pane': '%9', 'pid': '999', 'session': 'x', 'window': 'y', 'window_id': '@9',
                'agent': 'codex', 'cli': 'codex', 'command': 'codex', 'tracked': True,
                'project': 'other', 'path': '/other', 'state': 'RUNNING', 'pending': False,
                'emoji': '🏃', 'reason': '正在工作', 'updated': 100.0, 'age': '1s',
            }],
        }
        peers = [{'name': 'lab', 'url': 'http://lab:7788'}]
        with patch.object(a, 'load_peers', return_value=peers), \
             patch.object(a, 'fetch_peer_agents', return_value=peer_payload):
            first = a.fleet_payload(include_local=False, now=100.0, stale_ttl=30.0)
        self.assertEqual(len(first['agents']), 1)
        self.assertTrue(first['hosts'][0]['ok'])

        with patch.object(a, 'load_peers', return_value=peers), \
             patch.object(a, 'fetch_peer_agents', side_effect=TimeoutError('slow peer')):
            stale = a.fleet_payload(include_local=False, now=110.0, stale_ttl=30.0)
            expired = a.fleet_payload(include_local=False, now=140.0, stale_ttl=30.0)
        self.assertEqual(len(stale['agents']), 1)
        self.assertEqual(stale['agents'][0]['host'], 'lab')
        self.assertEqual(stale['summary']['running'], 1)
        self.assertFalse(stale['hosts'][0]['ok'])
        self.assertTrue(stale['hosts'][0].get('stale'))
        self.assertEqual(stale['hosts'][0].get('stale_age'), 10)
        self.assertEqual(expired['agents'], [])
        self.assertEqual(expired['summary']['running'], 0)
        self.assertFalse(expired['hosts'][0].get('stale', False))

    def test_large_transcript_tail_parse_and_fallback(self):
        large = Path(self.tmp.name) / 'large.jsonl'
        filler = json.dumps({'type': 'assistant', 'message': {'content': 'x' * 1024}}) + '\n'
        tail_rows = [
            {'type': 'user', 'message': {'content': f'問題 {i}'}}
            for i in range(1, 5)
        ] + [{'type': 'assistant', 'message': {'content': '尾端回覆\u2028完成'}}]
        large.write_text(
            filler * 300 + ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in tail_rows)
        )
        self.assertGreater(large.stat().st_size, a.TRANSCRIPT_TAIL_BYTES)
        preview = a.conversation_preview(large, 'claude')
        self.assertEqual(preview['users'], ['問題 2', '問題 3', '問題 4'])
        self.assertEqual(preview['agent'], '尾端回覆完成')

        sparse = Path(self.tmp.name) / 'sparse.jsonl'
        early_user = json.dumps({'type': 'user', 'message': {'content': '開頭問題'}}, ensure_ascii=False) + '\n'
        final_agent = json.dumps({'type': 'assistant', 'message': {'content': '最後結果'}}, ensure_ascii=False) + '\n'
        sparse.write_text(early_user + filler * 300 + final_agent)
        sparse_preview = a.conversation_preview(sparse, 'claude')
        self.assertEqual(sparse_preview['users'], ['開頭問題'])
        self.assertEqual(sparse_preview['agent'], '最後結果')

    def test_agents_payload_retries_transient_empty_live_panes(self):
        a.record(self.con, 'socket', '%1', '123', 'claude', 'WAIT', 'Approval needed')
        live = {('%1', '123'): {'pane': '%1', 'pid': '123', 'session': 'work', 'window': 'one', 'window_id': '@1', 'path': '/proj/demo'}}
        with patch.object(a, 'live_panes', side_effect=[{}, live]) as live_mock, \
             patch.object(a.time, 'sleep'):
            payload = a.agents_payload(self.con, 'socket', now=time.time() + 3)
        self.assertEqual(live_mock.call_count, 2)
        self.assertEqual(len(payload['agents']), 1)
        self.assertEqual(payload['agents'][0]['pane'], '%1')

    def test_agents_payload_uses_resolved_socket_for_live_panes(self):
        a.record(self.con, '/tmp/custom.sock', '%1', '123', 'claude', 'WAIT', 'Approval needed')
        live = {('%1', '123'): {'pane': '%1', 'pid': '123', 'session': 'work', 'window': 'one', 'window_id': '@1', 'path': '/proj/demo'}}
        with patch.object(a, 'live_panes', return_value=live) as live_mock:
            payload = a.agents_payload(self.con, '/tmp/custom.sock', now=time.time()+3)
        live_mock.assert_called_once_with('/tmp/custom.sock')
        self.assertEqual(payload['summary']['wait'], 1)
        self.assertEqual(payload['agents'][0]['pane'], '%1')

    def test_prune_stale_panes_removes_dead_rows_keeps_live(self):
        a.record(self.con, 'sock', '%1', '123', 'codex', 'IDLE', 'live')
        a.record(self.con, 'sock', '%2', '999', 'pi', 'IDLE', 'dead')
        with self.con:
            self.con.execute(
                "INSERT INTO codex_sessions(socket,pane,pid,thread_id,rollout_path,origin) VALUES('sock','%2','999','t','p','native')"
            )
            self.con.execute(
                "INSERT INTO cycle_cursors(socket,client,category,pane,pid) VALUES('sock','c0','idle','%2','999')"
            )
            self.con.execute(
                "INSERT INTO cycle_cursors(socket,client,category,pane,pid) VALUES('sock','c0','wait','%1','123')"
            )
        live = {('%1', '123'): {'pane': '%1', 'pid': '123', 'session': 's', 'window': 'w', 'window_id': '@1', 'path': '/'}}
        removed = a.prune_stale_panes(self.con, 'sock', live)
        self.assertGreaterEqual(removed, 3)
        panes = [dict(r) for r in self.con.execute('SELECT pane,pid FROM panes WHERE socket=?', ('sock',))]
        self.assertEqual(panes, [{'pane': '%1', 'pid': '123'}])
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM codex_sessions').fetchone()[0], 0)
        cursors = [dict(r) for r in self.con.execute('SELECT pane,pid FROM cycle_cursors')]
        self.assertEqual(cursors, [{'pane': '%1', 'pid': '123'}])
        # Empty live is ambiguous with list-panes failure — must not wipe.
        self.assertEqual(a.prune_stale_panes(self.con, 'sock', {}), 0)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM panes').fetchone()[0], 1)

class ClaudeSessionResetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        state = patch.object(a, 'STATE', Path(self.tmp.name))
        state.start()
        self.addCleanup(state.stop)
        self.con = a.db()
        self.addCleanup(self.con.close)
        for name in ('publish_view_revision', 'maybe_notify_wait'):
            mock = patch.object(a, name)
            setattr(self, name, mock.start())
            self.addCleanup(mock.stop)
        self.old_path = Path(self.tmp.name) / 'old.jsonl'
        self.old_path.write_text(json.dumps({'type': 'user', 'message': {'content': 'Old task'}}) + '\n'
                                 + json.dumps({'type': 'assistant', 'message': {'content': 'Old answer'}}) + '\n')

    def row(self):
        return dict(self.con.execute('SELECT * FROM panes').fetchone())

    def seed(self, state='IDLE', agent='claude'):
        a.record(self.con, 'socket', '%1', '123', agent, state, 'Old work', state == 'IDLE')
        a.remember_preview(self.con, 'socket', '%1', '123', user='Old task', agent='Old answer',
                           transcript_path=str(self.old_path))

    def hook(self, event='SessionStart', agent='claude', **data):
        with patch.object(a.sys, 'stdin', io.StringIO(json.dumps(data))), \
                patch.object(a.sys, 'stdout', io.StringIO()) as out, \
                patch.object(a, 'hook_target', return_value=('socket', '%1', '123')), \
                patch.dict(os.environ, {'CURSOR_VERSION': ''}):
            a.event_command(agent, event)
        self.assertEqual(json.loads(out.getvalue()), {})

    def test_clear_discards_prior_state_and_previews(self):
        for state in ('RUNNING', 'WAIT', 'IDLE'):
            for path_kind in ('empty', 'missing', 'blank'):
                with self.subTest(state=state, transcript=path_kind):
                    self.seed(state)
                    before = self.row()
                    path = Path(self.tmp.name) / f'{state}-{path_kind}.jsonl'
                    if path_kind == 'empty':
                        path.write_text('')
                    transcript = '' if path_kind == 'blank' else str(path)
                    self.hook(source='clear', transcript_path=transcript)
                    row = self.row()
                    self.assertEqual((row['state'], row['needs_review']), ('IDLE', 0))
                    self.assertGreater(row['revision'], before['revision'])
                    self.assertEqual((row['preview_users'], row['preview_agent'], row['preview_mtime'],
                                      row['preview_revision'], row['transcript_path']),
                                     ('[]', '', 0, 0, transcript))
                    # A bound missing transcript must not discover the previous conversation.
                    discovered = str(self.old_path) if transcript else ''
                    with patch.object(a, 'latest_claude_transcript', return_value=discovered) as discover:
                        self.assertEqual(a.pane_conversation_preview(self.con, row, {'path': '/project'}),
                                         {'users': [], 'agent': ''})
                        if transcript:
                            discover.assert_not_called()
                    self.assertEqual(self.publish_view_revision.call_args.args, ('socket', '%1', '123', 0))

    def test_startup_and_resume_preserve_unread_completion_and_cache(self):
        for source in ('startup', 'resume'):
            with self.subTest(source=source):
                self.seed()
                before = self.row()
                self.hook(source=source, transcript_path=str(Path(self.tmp.name) / 'resumed.jsonl'))
                row = self.row()
                for key in ('state', 'needs_review', 'revision', 'acknowledged', 'preview_users',
                            'preview_agent', 'preview_mtime', 'preview_revision'):
                    self.assertEqual(row[key], before[key], key)

    def test_clear_does_not_reset_other_agents(self):
        for agent in ('cursor', 'codex', 'pi'):
            with self.subTest(agent=agent):
                self.seed(agent=agent)
                before = self.row()
                self.hook(agent=agent, source='clear')
                self.assertEqual(self.row(), before)

    def test_unbound_claude_panes_still_discover_transcripts(self):
        self.seed()
        self.hook(source='clear')
        with patch.object(a, 'latest_claude_transcript', return_value=str(self.old_path)):
            self.assertEqual(a.resolve_transcript_path(self.con, self.row(), {'path': '/project'}),
                             str(self.old_path))

    def test_new_work_completes_after_clear(self):
        self.seed('RUNNING')
        path = str(Path(self.tmp.name) / 'new.jsonl')
        self.hook(source='clear', transcript_path=path)
        self.hook('UserPromptSubmit', prompt='New task', transcript_path=path)
        self.assertEqual(self.row()['state'], 'RUNNING')
        self.hook('Stop', last_assistant_message='New answer', transcript_path=path)
        row = self.row()
        self.assertEqual((row['state'], row['needs_review']), ('IDLE', 1))
        self.assertEqual(row['preview_revision'], row['revision'])
        self.assertEqual(a.pane_conversation_preview(self.con, row, {'path': '/project'}),
                         {'users': ['New task'], 'agent': 'New answer'})

    def test_clear_cache_reset_cannot_erase_later_work(self):
        self.seed('RUNNING')
        record = a.record
        new_path = str(Path(self.tmp.name) / 'later.jsonl')

        def later_work(*args, **kwargs):
            revision = record(*args, **kwargs)
            record(self.con, 'socket', '%1', '123', 'claude', 'RUNNING', 'Working')
            a.remember_preview(self.con, 'socket', '%1', '123', user='Later task', agent='Later answer',
                               transcript_path=new_path)
            return revision

        with patch.object(a, 'record', side_effect=later_work):
            self.hook(source='clear', transcript_path=str(Path(self.tmp.name) / 'clear.jsonl'))
        row = self.row()
        self.assertEqual(row['state'], 'RUNNING')
        self.assertEqual(json.loads(row['preview_users'])[-1], 'Later task')
        self.assertEqual((row['preview_agent'], row['transcript_path']), ('Later answer', new_path))

    def test_stale_poll_cannot_restore_cache_after_clear_on_an_already_idle_pane(self):
        # record() does not bump the revision when the state is unchanged, so the reset must.
        a.record(self.con, 'socket', '%1', '123', 'claude', 'IDLE', 'Registered; no new work yet', False)
        a.remember_preview(self.con, 'socket', '%1', '123', user='Old task', agent='Old answer',
                           transcript_path=str(self.old_path))
        stale = self.row()
        self.hook(source='clear', transcript_path=str(Path(self.tmp.name) / 'new.jsonl'))
        a.pane_conversation_preview(self.con, stale, {'path': '/project'})
        row = self.row()
        self.assertEqual((row['preview_users'], row['preview_agent']), ('[]', ''))
        self.assertGreater(row['revision'], stale['revision'])


class IntegrationTests(unittest.TestCase):
    def test_install_live_hooks_sidebars_navigation_and_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            home=Path(directory)
            label='attn-test-'+str(os.getpid())
            command=['tmux','-L',label]
            subprocess.run(command+['-f','/dev/null','new-session','-d','-s','fixture','-x','160','-y','45','/bin/sh'],check=True)
            try:
                def initial(*args): return subprocess.check_output(command+list(args),text=True).strip()
                sock=initial('display-message','-p','#{socket_path}')
                pid=initial('display-message','-p','#{pid}')
                pane=initial('display-message','-p','#{pane_id}')
                window=initial('display-message','-p','#{window_id}')
                env=os.environ|{'HOME':str(home),'TMUX':f'{sock},{pid},0','TMUX_PANE':pane}
                def run(*args): return subprocess.check_output(['tmux',*args],env=env,text=True).strip()
                old_window_format=run('show-options','-gwv','window-status-format')
                (home/'.codex').mkdir()
                (home/'.codex/config.toml').write_text('[features]\nhooks = true\n')
                # Preserve an unrelated hook and validate exact rollback.
                (home/'.claude').mkdir()
                old=json.dumps({'hooks':{'Stop':[{'hooks':[{'type':'command','command':'echo unrelated'}]}]}})
                (home/'.claude/settings.json').write_text(old)
                (home/'.cursor').mkdir()
                cursor_old=json.dumps({'version':1,'hooks':{'stop':[{'command':'echo unrelated-cursor'}]}})
                (home/'.cursor/hooks.json').write_text(cursor_old)
                result=subprocess.check_output([sys.executable,'install.py','--sidebar'],env=env,text=True)
                backup=json.loads(result)['backup']
                app=home/'.local/share/tmux-attn/attention.py'
                def wait(predicate):
                    for _ in range(100):
                        if predicate(): return
                        time.sleep(.1)
                    self.fail('Timed out waiting for tmux sidebar: '+run('capture-pane','-p','-t',sidebar)+'\nWORK PANE: '+run('capture-pane','-p','-t',pane)+'\nFILES: '+str(list(home.rglob('hook-errors.log')))+'\nLOG: '+''.join(p.read_text() for p in home.rglob('hook-errors.log'))+'\nDUMP: '+subprocess.check_output([sys.executable,str(app),'dump'],env=env,text=True))
                wait(lambda:'\t1' in run('list-panes','-a','-F','#{pane_id}\t#{@tmux_attn_sidebar}'))
                sidebar=next(l.split('\t')[0] for l in run('list-panes','-t',window,'-F','#{pane_id}\t#{@tmux_attn_sidebar}').splitlines() if l.endswith('\t1'))
                def wait_badge(expected):
                    # `status` is what refreshes the window badge, so re-run it on every poll instead of
                    # reading the option once right after an asynchronous sidebar/hook update.
                    def badge_is_current():
                        subprocess.check_output([sys.executable,str(app),'status'],env=env,text=True)
                        return run('show-options','-wv','-t',window,'@tmux_attn_badge')==expected
                    wait(badge_is_current)
                def event(name,data={},agent='codex'):
                    # Replace only the disposable test pane's command with a shell and
                    # invoke the real hook as its child; intentionally poison TMUX_PANE.
                    import shlex
                    payload = home/'payload.json'
                    payload.write_text(json.dumps(data))
                    result = home/'hook-result.json'
                    hook = shlex.join([sys.executable,str(app),'event',agent,name])
                    script = 'TMUX_ATTN_STATE_DIR='+shlex.quote(str(home/'.local/state/tmux-attn'))+' TMUX_PANE=%999999 '+hook+' < '+shlex.quote(str(payload))+' > '+shlex.quote(str(result))+''
                    run('send-keys','-t',pane,script,'Enter')
                    wait(lambda:result.exists() and result.read_text().strip()=='{}')
                    self.assertEqual(json.loads(result.read_text()),{})
                    result.unlink()
                cursor_config=json.loads((home/'.cursor/hooks.json').read_text())
                self.assertIn({'command':'echo unrelated-cursor'},cursor_config['hooks']['stop'])
                self.assertIn('postToolUse',cursor_config['hooks'])
                event('postToolUse',agent='cursor'); event('stop',{'status':'completed'},agent='cursor')
                wait(lambda:'👀 1' in run('capture-pane','-p','-t',sidebar))
                event('UserPromptSubmit'); event('PermissionRequest',{'tool_input':{'description':'Approve test action'}})
                wait(lambda:'🙋 1' in run('capture-pane','-p','-t',sidebar))
                wait_badge('🙋')
                self.assertIn('@tmux_attn_badge',run('show-options','-gwv','window-status-format'))
                self.assertIn('@tmux_attn_project',run('show-options','-gwv','window-status-format'))
                project=Path(run('display-message','-p','-t',pane,'#{pane_current_path}')).name
                default_aliases=json.loads((home/'.config/tmux-attn/project-aliases.json').read_text())['aliases']
                expected_project=default_aliases.get(project) or (project if len(project)<=12 else project[:11]+'…')
                self.assertEqual(run('show-options','-wv','-t',window,'@tmux_attn_project'),expected_project)
                # New window hook automatically creates another sidebar.
                new=run('new-window','-d','-P','-F','#{window_id}','sleep 600')
                wait(lambda:'\t1' in run('list-panes','-t',new,'-F','#{pane_id}\t#{@tmux_attn_sidebar}'))
                subprocess.check_output([sys.executable,str(app),'status'],env=env,text=True)
                self.assertEqual(run('show-options','-wv','-t',new,'@tmux_attn_project'),expected_project)
                self.assertEqual(run('display-message','-p','-t',new,'#{@tmux_attn_badge}'),'')
                # Queued hooks targeting already closed windows are normal no-ops.
                departed=subprocess.run([sys.executable,str(app),'ensure','@99999999'],env=env,capture_output=True,text=True)
                self.assertEqual(departed.returncode,0)
                self.assertEqual(departed.stderr,'')
                # Closing the work pane should not leave an orphan sidebar window.
                new_work = next(l.split(' ')[0] for l in run('list-panes','-t',new,'-F','#{pane_id} #{?@tmux_attn_sidebar,1,0}').splitlines() if l.endswith(' 0'))
                run('kill-pane','-t',new_work)
                wait(lambda:new not in run('list-windows','-a','-F','#{window_id}').splitlines())
                new=run('new-window','-d','-P','-F','#{window_id}','sleep 600')
                wait(lambda:'\t1' in run('list-panes','-t',new,'-F','#{pane_id}\t#{@tmux_attn_sidebar}'))
                subprocess.run([sys.executable,str(app),'ensure'],env=env,check=True)
                self.assertEqual(run('list-panes','-a','-F','#{@tmux_attn_sidebar}').splitlines().count('1'),2)
                # A WAIT jump preserves the action that still needs a reply.
                run('select-window','-t',new)
                subprocess.run([sys.executable,str(app),'next'],env=env,check=True)
                self.assertEqual(run('display-message','-p','#{pane_id}'),pane)
                run('send-keys','-t',sidebar,'d')
                wait(lambda:not json.loads(subprocess.check_output([sys.executable,str(app),'dump'],env=env,text=True))[0]['pending'])
                event('UserPromptSubmit'); event('Stop')
                wait(lambda:'👀 1' in run('capture-pane','-p','-t',sidebar))
                event('UserPromptSubmit',agent='claude')
                event('Stop',{'background_tasks':[{'type':'shell','status':'running'}]},agent='claude')
                # Parked on background work is running, not an unread result.
                wait(lambda:'等待背景工作' in run('capture-pane','-p','-t',sidebar))
                wait(lambda:'🏃 1' in run('capture-pane','-p','-t',sidebar))
                wait_badge('🏃')
                # The wake-up report ends the wait with a normal unread result.
                event('UserPromptSubmit',agent='claude'); event('Stop',agent='claude')
                wait(lambda:'👀 1' in run('capture-pane','-p','-t',sidebar))
                # A different agent registering in the same pane must retain unread work.
                event('session_start',agent='pi')
                self.assertIn('👀 1',subprocess.check_output([sys.executable,str(app),'status'],env=env,text=True))
                self.assertEqual(run('show-options','-wv','-t',window,'@tmux_attn_badge'),'👀')
                run('send-keys','-t',sidebar,'d')
                wait(lambda:not json.loads(subprocess.check_output([sys.executable,str(app),'dump'],env=env,text=True))[0]['pending'])
                wait_badge('💤')
                event('UserPromptSubmit')
                wait_badge('🏃')
                # Switch to popup, remove only marked panes, then restore sidebar mode.
                original_app = app.read_text()
                installed_assets = [
                    app.parent/'install_web_service.sh',
                    app.parent/'contrib/systemd/tmux-attn-web.service',
                    app.parent/'contrib/launchd/ai.tmux-attn.web.plist',
                ]
                for asset in installed_assets:
                    asset.write_text('custom prior asset: '+asset.name+'\n')
                icons = app.parent/'web/icons'
                (icons/'custom.svg').write_text('<svg>custom icon</svg>')
                old_assets = {path: path.read_bytes() for path in installed_assets}
                old_icons = {path.name: path.read_bytes() for path in icons.iterdir()}
                alias_path=home/'.config/tmux-attn/project-aliases.json'
                aliases=json.loads(alias_path.read_text())
                aliases['aliases'][project]='fixture'
                alias_path.write_text(json.dumps(aliases))
                subprocess.check_output([sys.executable,str(app),'status'],env=env,text=True)
                self.assertEqual(run('show-options','-wv','-t',window,'@tmux_attn_project'),'fixture')
                work_before = {line for line in run('list-panes','-a','-F','#{pane_id} #{pane_pid} #{?@tmux_attn_sidebar,1,0}').splitlines() if line.endswith(' 0')}
                again=json.loads(subprocess.check_output([sys.executable,'install.py'],env=env,text=True))
                self.assertEqual(json.loads(alias_path.read_text()),aliases)
                self.assertNotIn('1',run('list-panes','-a','-F','#{@tmux_attn_sidebar}').splitlines())
                self.assertFalse((icons/'custom.svg').exists())
                self.assertEqual(work_before,set(run('list-panes','-a','-F','#{pane_id} #{pane_pid} #{?@tmux_attn_sidebar,1,0}').splitlines()))
                self.assertIn('display-popup',run('list-keys','-T','prefix'))
                self.assertNotEqual(again['backup'],backup)
                app.write_text(original_app+'\n# temporary upgrade marker\n')
                subprocess.run([sys.executable,'rollback.py',again['backup']],env=env,check=True,capture_output=True)
                self.assertEqual(app.read_text(),original_app)
                self.assertEqual({path: path.read_bytes() for path in installed_assets}, old_assets)
                self.assertEqual({path.name: path.read_bytes() for path in icons.iterdir()}, old_icons)
                wait(lambda:run('list-panes','-a','-F','#{@tmux_attn_sidebar}').splitlines().count('1')==2)
                self.assertIn('echo unrelated',(home/'.claude/settings.json').read_text())
                subprocess.run([sys.executable,'rollback.py',backup],env=env,check=True,capture_output=True)
                self.assertEqual((home/'.claude/settings.json').read_text(),old)
                self.assertFalse(alias_path.exists())
                self.assertFalse(icons.exists())
                for asset in installed_assets:
                    self.assertFalse(asset.exists())
                self.assertEqual(run('show-options','-gwv','window-status-format'),old_window_format)
                self.assertNotIn('1',run('list-panes','-a','-F','#{@tmux_attn_sidebar}').splitlines())
                self.assertEqual(run('list-panes','-a','-F','#{pane_id}').splitlines().__len__(),2)
            finally:
                subprocess.run(command+['kill-server'],check=False)

if __name__=='__main__': unittest.main(verbosity=2)
