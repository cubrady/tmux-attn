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
import io
from concurrent.futures import ThreadPoolExecutor
from urllib.request import urlopen

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
        # Opening a target does not mutate its inbox state.
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
        self.assertEqual(a.status_text(a.status_groups(rows)),'🙋 0 · 👀 0 · 🏃 0 · 💤 1')

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
        stopped=a.classify('claude','Stop',{'background_tasks':[{'status':'running'}]})
        self.assertEqual((stopped[0],stopped[2]),('IDLE',True))
        self.assertIn('background tasks still running',stopped[1])
        self.assertEqual(a.classify('codex','PermissionRequest',{})[0],'WAIT')
        self.assertEqual(a.classify('codex','PreToolUse',{'tool_name':'request_user_input'})[0],'WAIT')
        self.assertEqual(a.classify('pi','ui_prompt_start',{'title':'Pick one'})[0],'WAIT')
        self.assertEqual(a.classify('pi','agent_settled',{})[0],'IDLE')
        self.assertEqual(a.classify('claude','Stop',{'last_assistant_message':'你希望採用哪個方案？'})[0],'WAIT')
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
            'wait': 1, 'review': 1, 'running': 1, 'idle': 1, 'tracked': 4, 'untracked': 1,
        })
        emojis = [row['emoji'] for row in payload['agents'] if row['tracked']]
        self.assertEqual(emojis, ['🙋', '👀', '🏃', '💤'])
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
            self.assertIn('🙋 等待回覆', page)
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

    def test_agents_payload_uses_resolved_socket_for_live_panes(self):
        a.record(self.con, '/tmp/custom.sock', '%1', '123', 'claude', 'WAIT', 'Approval needed')
        live = {('%1', '123'): {'pane': '%1', 'pid': '123', 'session': 'work', 'window': 'one', 'window_id': '@1', 'path': '/proj/demo'}}
        with patch.object(a, 'live_panes', return_value=live) as live_mock:
            payload = a.agents_payload(self.con, '/tmp/custom.sock', now=time.time()+3)
        live_mock.assert_called_once_with('/tmp/custom.sock')
        self.assertEqual(payload['summary']['wait'], 1)
        self.assertEqual(payload['agents'][0]['pane'], '%1')

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
                    for _ in range(30):
                        if predicate(): return
                        time.sleep(.1)
                    self.fail('Timed out waiting for tmux sidebar: '+run('capture-pane','-p','-t',sidebar)+'\nFILES: '+str(list(home.rglob('hook-errors.log')))+'\nLOG: '+''.join(p.read_text() for p in home.rglob('hook-errors.log'))+'\nDUMP: '+subprocess.check_output([sys.executable,str(app),'dump'],env=env,text=True))
                wait(lambda:'\t1' in run('list-panes','-a','-F','#{pane_id}\t#{@tmux_attn_sidebar}'))
                sidebar=next(l.split('\t')[0] for l in run('list-panes','-t',window,'-F','#{pane_id}\t#{@tmux_attn_sidebar}').splitlines() if l.endswith('\t1'))
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
                subprocess.check_output([sys.executable,str(app),'status'],env=env,text=True)
                self.assertEqual(run('show-options','-wv','-t',window,'@tmux_attn_badge'),'🙋')
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
                # Jump from another window to the exact agent pane; item remains pending.
                run('select-window','-t',new)
                subprocess.run([sys.executable,str(app),'next'],env=env,check=True)
                self.assertEqual(run('display-message','-p','#{pane_id}'),pane)
                run('send-keys','-t',sidebar,'d')
                wait(lambda:not json.loads(subprocess.check_output([sys.executable,str(app),'dump'],env=env,text=True))[0]['pending'])
                event('UserPromptSubmit'); event('Stop')
                wait(lambda:'👀 1' in run('capture-pane','-p','-t',sidebar))
                event('UserPromptSubmit',agent='claude')
                event('Stop',{'background_tasks':[{'status':'running'}]},agent='claude')
                wait(lambda:'背景工作仍在運行' in run('capture-pane','-p','-t',sidebar))
                wait(lambda:'👀 1' in run('capture-pane','-p','-t',sidebar))
                # A different agent registering in the same pane must retain unread work.
                event('session_start',agent='pi')
                self.assertIn('👀 1',subprocess.check_output([sys.executable,str(app),'status'],env=env,text=True))
                self.assertEqual(run('show-options','-wv','-t',window,'@tmux_attn_badge'),'👀')
                run('send-keys','-t',sidebar,'d')
                wait(lambda:not json.loads(subprocess.check_output([sys.executable,str(app),'dump'],env=env,text=True))[0]['pending'])
                subprocess.check_output([sys.executable,str(app),'status'],env=env,text=True)
                self.assertEqual(run('show-options','-wv','-t',window,'@tmux_attn_badge'),'💤')
                event('UserPromptSubmit')
                subprocess.check_output([sys.executable,str(app),'status'],env=env,text=True)
                self.assertEqual(run('show-options','-wv','-t',window,'@tmux_attn_badge'),'🏃')
                # Switch to popup, remove only marked panes, then restore sidebar mode.
                original_app = app.read_text()
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
                self.assertEqual(work_before,set(run('list-panes','-a','-F','#{pane_id} #{pane_pid} #{?@tmux_attn_sidebar,1,0}').splitlines()))
                self.assertIn('display-popup',run('list-keys','-T','prefix'))
                self.assertNotEqual(again['backup'],backup)
                app.write_text(original_app+'\n# temporary upgrade marker\n')
                subprocess.run([sys.executable,'rollback.py',again['backup']],env=env,check=True,capture_output=True)
                self.assertEqual(app.read_text(),original_app)
                wait(lambda:run('list-panes','-a','-F','#{@tmux_attn_sidebar}').splitlines().count('1')==2)
                self.assertIn('echo unrelated',(home/'.claude/settings.json').read_text())
                subprocess.run([sys.executable,'rollback.py',backup],env=env,check=True,capture_output=True)
                self.assertEqual((home/'.claude/settings.json').read_text(),old)
                self.assertFalse(alias_path.exists())
                self.assertEqual(run('show-options','-gwv','window-status-format'),old_window_format)
                self.assertNotIn('1',run('list-panes','-a','-F','#{@tmux_attn_sidebar}').splitlines())
                self.assertEqual(run('list-panes','-a','-F','#{pane_id}').splitlines().__len__(),2)
            finally:
                subprocess.run(command+['kill-server'],check=False)

if __name__=='__main__': unittest.main(verbosity=2)
