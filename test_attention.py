import importlib.util
import json
from contextlib import closing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import io
from concurrent.futures import ThreadPoolExecutor

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

    def test_classifiers(self):
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

    def test_project_aliases_path_overrides_and_collisions(self):
        paths=['/a/web-app-minday','/a/a-long-new-project','/b/a-long-new-platform','/a/minday-api','/a/minday-web','/a/shared','/b/shared']
        aliases={'web-app-minday':'minday','minday-api':'demo','minday-web':'demo','/b/shared':'other'}
        labels=a.project_labels(paths,aliases)
        self.assertEqual(labels['/a/web-app-minday'],'minday')
        self.assertEqual(labels['/b/shared'],'other')
        self.assertEqual(labels['/a/minday-api'],'demo-api')
        self.assertEqual(labels['/a/minday-web'],'demo-web')
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
                self.assertEqual(run('show-options','-wv','-t',window,'@tmux_attn_badge'),'🔄')
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
