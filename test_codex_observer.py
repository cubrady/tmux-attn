import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
import attention as a
from codex_observer import approval_screen,lifecycle,read_lifecycle,startup_match,resumed_match,retire_mapping,repair_mapping,cursor_screen,native_cursor_pids,observe_cursor,discover,claude_screen,native_claude_pids,observe_claude,native_codex_pids,is_native_codex_cli
from codex_observer import queued_questions_screen,sync_attention_overlay

class ObserverTests(unittest.TestCase):
 def test_native_codex_accepts_standalone_short_argv(self):
  table={123:(1,'zsh'),456:(123,'codex --sandbox danger-full-access resume'),789:(1,'/home/x/.codex/packages/app-server-daemon/releases/x/bin/codex app-server --managed-daemon')}
  with patch('codex_observer._process_exe', side_effect=lambda pid, args='': {
   456:'/home/x/.codex/packages/standalone/releases/0.1/bin/codex',
   789:'/home/x/.codex/packages/app-server-daemon/releases/x/bin/codex',
  }.get(pid, args.split()[0] if args else '')):
   self.assertTrue(is_native_codex_cli(456, table[456][1]))
   self.assertFalse(is_native_codex_cli(789, table[789][1]))
   self.assertEqual(native_codex_pids(123, table), [456])

 def test_queued_questions_override_running_and_clear_to_latest_lifecycle(self):
  screen='• Working (58m 18s • esc to interrupt)\n• Queued follow-up inputs\n  ? 1 question\n    ⇧← to answer\n\n› Ask Codex to do anything\nGPT high · /project · task\n← for agents'
  self.assertEqual(queued_questions_screen(screen),1)
  self.assertEqual(queued_questions_screen(screen.replace('1 question','2 questions')),2)
  self.assertEqual(queued_questions_screen('Explain Queued follow-up inputs and request_user_input_async'),0)
  self.assertEqual(queued_questions_screen(screen.replace('⇧← to answer','question text')),0)
  self.assertEqual(queued_questions_screen(screen.replace('• Queued follow-up inputs','Quoted: Queued follow-up inputs')),0)
  with tempfile.TemporaryDirectory() as directory:
   previous_state=a.STATE;a.STATE=Path(directory)
   try:
    with closing(a.db()) as con:
     key=('%1','123','root','path');overlays={}
     previous=(('RUNNING','Working',False),1,'turn')
     a.record(con,'sock','%1','123','codex',*previous[0])
     def scan(text,latest=previous):sync_attention_overlay(con,'sock','%1','123',key,text,latest,overlays)
     scan(screen)
     row=con.execute('SELECT * FROM panes').fetchone()
     self.assertEqual(row['state'],'WAIT')
     self.assertIn('1 個問題等你回答',a.display_reason(dict(row),False))
     a.acknowledge(con,row)
     acknowledged=dict(con.execute('SELECT * FROM panes').fetchone())
     scan(screen)
     current=dict(con.execute('SELECT * FROM panes').fetchone())
     self.assertEqual(current['revision'],acknowledged['revision'])
     self.assertEqual(current['acknowledged'],acknowledged['acknowledged'])
     scan(screen.replace('1 question','2 questions'))
     self.assertGreater(con.execute('SELECT revision FROM panes').fetchone()[0],current['revision'])
     overlays.clear()  # Observer restarted while the question was visible.
     scan('• Working (esc to interrupt)')
     self.assertEqual(con.execute('SELECT state FROM panes').fetchone()[0],'RUNNING')
     scan(screen)
     scan('',(('IDLE','Response finished; review needed',True),2,'turn'))
     self.assertEqual(con.execute('SELECT state FROM panes').fetchone()[0],'IDLE')
     # Removing the HUD must not erase a newer native approval/input event.
     scan(screen)
     a.record(con,'sock','%1','123','codex','WAIT','New native approval')
     scan('')
     self.assertEqual(con.execute('SELECT reason FROM panes').fetchone()[0],'New native approval')
   finally:a.STATE=previous_state

 def test_claude_stale_permission_recovers_without_overriding_dialog_or_ack(self):
  busy='✢ Scurrying… (running PostToolUse hooks… 3/8 · 7m 38s)\n────\n❯\n────\n⏵⏵ auto mode on · esc to interrupt'
  done='⏺ Finished the update.\n✻ Sautéed for 7m 58s · done 下午2:46\n────\n❯\n────\n⏵⏵ auto mode on'
  self.assertEqual(claude_screen(busy),('RUNNING','Working',False))
  waiting='✻ Waiting for 5 background agents to finish\n'+('\n'.join(['agents panel']*20))
  self.assertEqual(claude_screen(waiting),('RUNNING','Working',False))
  self.assertEqual(claude_screen(waiting+'\n'+done)[0],'IDLE')
  self.assertEqual(claude_screen(done)[0],'IDLE')
  self.assertEqual(claude_screen(done.replace('Finished the update.','Which option？'))[0],'WAIT')
  for modal in ['Do you want to proceed?\n❯ 1. Yes\n2. No', 'Claude Code needs your approval for the plan', 'Enter to select · Esc to cancel','Submit answers','Press Enter to continue','Session paused']:
   self.assertIsNone(claude_screen(modal+'\n'+busy))
   self.assertIsNone(claude_screen(modal+'\n'+done))
  self.assertIsNone(claude_screen('Quoted: '+busy))
  self.assertIsNone(claude_screen('❯\n⏵⏵ auto mode on'))
  table={123:(1,'zsh'),456:(123,'claude --resume root'),500:(456,'claude daemon run'),501:(500,'claude bg-pty-host --bg-pty-host socket'),502:(501,'claude bg-spare --bg-spare socket'),600:(456,'claude -p child')}
  self.assertEqual(native_claude_pids('123',table),[456])
  self.assertEqual(native_claude_pids('502',table),[])
  with tempfile.TemporaryDirectory() as directory:
   previous=a.STATE;a.STATE=Path(directory)
   try:
    with closing(a.db()) as con:
     live={('%1','123'):{'path':'/project'}}
     a.record(con,'sock','%1','123','claude','WAIT','Claude needs your permission',event_time=1000000000)
     def scan(screen):
      with patch('codex_observer.tmux',return_value=screen):observe_claude(con,'sock',live,table)
     before=dict(con.execute('SELECT * FROM panes').fetchone())
     scan('Do you want to proceed?\n'+busy)
     self.assertEqual(dict(con.execute('SELECT * FROM panes').fetchone()),before)
     scan(busy)
     self.assertEqual(con.execute('SELECT state FROM panes').fetchone()[0],'RUNNING')
     with patch('codex_observer.time.time',return_value=2000000000):scan(done)
     row=con.execute('SELECT * FROM panes').fetchone()
     self.assertEqual((row['state'],row['needs_review']),('IDLE',1))
     a.acknowledge(con,row)
     before=dict(con.execute('SELECT * FROM panes').fetchone())
     scan(done)
     self.assertEqual(dict(con.execute('SELECT * FROM panes').fetchone()),before)
   finally:a.STATE=previous

 def test_cursor_footer_lifecycle_and_acknowledgement(self):
  def screen(body,active=False):
   return body+'\n\n → Add a follow-up'+('    ctrl+c to stop' if active else '')+'\n\n Auto · 16.6%    Run Everything\n /project · main\n'
  self.assertEqual(cursor_screen(screen('Done.'),'/project'),('IDLE','Response finished; review needed',True))
  self.assertEqual(cursor_screen(screen('Pick one？'),'/project')[0],'WAIT')
  self.assertEqual(cursor_screen(screen('Done.',True),'/project')[0],'RUNNING')
  self.assertIsNone(cursor_screen(screen('Done.'),'/other'))
  self.assertIsNone(cursor_screen('Done.\nAdd a follow-up\n/project · main','/project'))
  approval=screen('Run this command?\nRun (once) (y)\nSkip & tell the agent what to do instead (esc or n)',True)
  self.assertEqual(cursor_screen(approval,'/project')[0],'WAIT')
  self.assertEqual(cursor_screen(screen('Delete this file?\nDelete (y)\nKeep (n)',True),'/project')[0],'WAIT')
  table={123:(1,'zsh'),456:(123,'/bin/agent --use-system-ca /home/.local/share/cursor-agent/versions/test/index.js'),789:(123,'node other.js')}
  self.assertEqual(native_cursor_pids('123',table),[456])
  self.assertEqual(native_cursor_pids('789',table),[])
  with tempfile.TemporaryDirectory() as directory:
   previous=a.STATE;a.STATE=Path(directory)
   try:
    with closing(a.db()) as con:
     live={('%1','123'):{'path':'/project'}}
     a.record(con,'sock','%1','123','claude','RUNNING','Working')
     def scan(text):
      with patch('codex_observer.tmux',return_value=text):observe_cursor(con,'sock',live,table)
     scan(screen('Done.'))
     row=con.execute('SELECT * FROM panes').fetchone()
     self.assertEqual((row['agent'],row['state']),('cursor','IDLE'))
     a.acknowledge(con,row)
     scan(screen('Done.'));scan(screen('Done.'))
     row2=con.execute('SELECT * FROM panes').fetchone()
     self.assertEqual(row2['revision'],row2['acknowledged'])
     scan(screen('Working',True));scan(screen('New completion.'))
     row3=con.execute('SELECT * FROM panes').fetchone()
     self.assertGreater(row3['revision'],row3['acknowledged'])
   finally:a.STATE=previous

 def test_resume_matching_requires_exact_footer_unique_name_and_root(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);database=root/'state.sqlite';path=root/'root.jsonl'
   header={'type':'session_meta','payload':{'id':'root','originator':'codex-tui','cwd':'/project','source':'vscode'}}
   path.write_text(json.dumps(header)+'\n')
   with closing(sqlite3.connect(database)) as con,con:
    con.execute('CREATE TABLE threads(id,rollout_path,cwd,agent_path,name)')
    con.execute('INSERT INTO threads VALUES(?,?,?,?,?)',('root',str(path),'/project',None,'My task'))
   screen='GPT medium · /project · My task\n← for agents · ? for shortcuts'
   self.assertEqual(resumed_match(database,'/project',screen),('root',str(path)))
   self.assertEqual(resumed_match(database,'/project','GPT medium · /project · My task        Goal achieved (1h 2m)\n← for agents'),('root',str(path)))
   with closing(sqlite3.connect(database)) as con,con:
    con.execute("UPDATE threads SET name='My  task'")
   self.assertEqual(resumed_match(database,'/project','GPT medium · /project · My  task        Goal achieved (1h 2m)'),('root',str(path)))
   with closing(sqlite3.connect(database)) as con,con:
    con.execute("UPDATE threads SET name='My task'")
   self.assertIsNone(resumed_match(database,'/other',screen))
   self.assertIsNone(resumed_match(database,'/project','My task\nGPT medium · /project · Other task'))
   other=root/'other.jsonl';header['payload']['id']='other';other.write_text(json.dumps(header)+'\n')
   with closing(sqlite3.connect(database)) as con,con:
    con.execute('INSERT INTO threads VALUES(?,?,?,?,?)',('other',str(other),'/project',None,'My task'))
   self.assertIsNone(resumed_match(database,'/project',screen))
   with closing(sqlite3.connect(database)) as con,con:
    con.execute('DELETE FROM threads WHERE id=?',('other',))
   header['payload']['id']='child';header['payload']['source']={'subagent':{}}
   path.write_text(json.dumps(header)+'\n')
   self.assertIsNone(resumed_match(database,'/project',screen))
 def test_unique_startup_match_rejects_newest_and_ambiguous_sessions(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);path=root/'rollout.jsonl';stamp=1700000000
   header={'type':'session_meta','payload':{'originator':'codex-tui','id':'session-1','cwd':'/project','timestamp':datetime.fromtimestamp(stamp+2,timezone.utc).isoformat()}}
   path.write_text(json.dumps(header)+'\n')
   database=root/'state.sqlite'
   with closing(sqlite3.connect(database)) as con, con:
    con.execute('CREATE TABLE threads(id,rollout_path,created_at,cwd,agent_path)')
    con.execute('INSERT INTO threads VALUES(?,?,?,?,?)',('session-1',str(path),stamp+2,'/project',None))
    # A newer session and a simultaneous child must not become the mapping.
    con.execute('INSERT INTO threads VALUES(?,?,?,?,?)',('newer','missing',stamp+90,'/project',None))
    con.execute('INSERT INTO threads VALUES(?,?,?,?,?)',('child','missing',stamp+2,'/project','/child'))
   self.assertEqual(startup_match(database,'/project',stamp),('session-1',str(path)))
   second=root/'second.jsonl';header['payload']['id']='session-2';second.write_text(json.dumps(header)+'\n')
   with closing(sqlite3.connect(database)) as con, con:
    con.execute('INSERT INTO threads VALUES(?,?,?,?,?)',('session-2',str(second),stamp+2,'/project',None))
   self.assertIsNone(startup_match(database,'/project',stamp))
 def test_incremental_rollout_handles_partial_line_and_completion(self):
  with tempfile.TemporaryDirectory() as directory:
   p=Path(directory)/'session.jsonl'
   start={'timestamp':'2026-10-03T07:00:00Z','type':'event_msg','payload':{'type':'task_started','turn_id':'turn1'}}
   done={'timestamp':'2026-10-03T07:01:00Z','type':'event_msg','payload':{'type':'task_complete','turn_id':'turn1','last_agent_message':'Finished.'}}
   p.write_text(json.dumps(start)+'\n'+json.dumps(done)[:20])
   offset,event=read_lifecycle(p)
   self.assertEqual(event[0][0],'RUNNING')
   with p.open('a') as f:f.write(json.dumps(done)[20:]+'\n')
   offset,event=read_lifecycle(p,offset)
   self.assertEqual(event[0],('IDLE','Response finished; review needed',True))
   self.assertIsNone(read_lifecycle(p,offset)[1])
 def test_pending_approval_requires_actual_dialog(self):
  self.assertFalse(approval_screen('sandbox_permissions: require_escalated'))
  self.assertFalse(approval_screen('Would you like to run tests?'))
  self.assertTrue(approval_screen('Would you like to run the following command?\n› 1. Yes, proceed\n  2. No, tell Codex what to do differently'))
 def test_stale_rollout_cannot_overwrite_hook_and_new_turn_realerts(self):
  with tempfile.TemporaryDirectory() as directory:
   previous=a.STATE;a.STATE=Path(directory)
   try:
    with closing(a.db()) as con:
     a.record(con,'socket','%1','123','codex','WAIT','Approve',event_time=200)
     a.record(con,'socket','%1','123','codex','RUNNING','Working',event_time=100)
     self.assertEqual(con.execute('SELECT state FROM panes').fetchone()[0],'WAIT')
     a.record(con,'socket','%1','123','codex','IDLE','Finished',True,event_time=300,event_key='turn1')
     item=dict(con.execute('SELECT * FROM panes').fetchone());a.acknowledge(con,item)
     a.record(con,'socket','%1','123','codex','IDLE','Finished',True,event_time=400,event_key='turn2')
     row=con.execute('SELECT revision,acknowledged FROM panes').fetchone()
     self.assertGreater(row[0],row[1])
   finally:a.STATE=previous
 def test_same_running_state_advances_freshness_without_extra_revision(self):
  with tempfile.TemporaryDirectory() as directory:
   previous=a.STATE;a.STATE=Path(directory)
   try:
    with closing(a.db()) as con:
     a.record(con,'socket','%1','123','codex','RUNNING','Working',event_time=100,event_key='old')
     a.record(con,'socket','%1','123','codex','RUNNING','Working',event_time=300,event_key='new')
     a.record(con,'socket','%1','123','codex','IDLE','Finished',True,event_time=200,event_key='old')
     row=con.execute('SELECT * FROM panes').fetchone()
     self.assertEqual((row['state'],row['revision'],row['event_key'],row['last_event_time']),('RUNNING',1,'new',300))
   finally:a.STATE=previous
 def test_legacy_cli_exit_becomes_idle_once_and_preserves_other_agent(self):
  with tempfile.TemporaryDirectory() as directory:
   previous=a.STATE;a.STATE=Path(directory)
   try:
    with closing(a.db()) as con:
     mapping={'socket':'socket','pane':'%1','pid':'123','cli_pid':999}
     a.record(con,'socket','%1','123','codex','RUNNING','Working')
     retire_mapping(con,mapping);retire_mapping(con,mapping)
     row=con.execute('SELECT * FROM panes').fetchone()
     self.assertEqual((row['state'],row['needs_review'],row['revision']),('IDLE',1,2))
     a.record(con,'socket','%1','123','pi','RUNNING','Working')
     retire_mapping(con,mapping)
     self.assertEqual(con.execute('SELECT agent,state FROM panes').fetchone()[:],('pi','RUNNING'))
   finally:a.STATE=previous
 def test_changed_thread_never_keeps_previous_rollout(self):
  with tempfile.TemporaryDirectory() as directory:
   previous=a.STATE;a.STATE=Path(directory)
   try:
    with closing(a.db()) as con:
     path=Path(directory)/'old.jsonl'
     path.write_text(json.dumps({'type':'session_meta','payload':{'id':'test-old-thread','source':'vscode'}})+'\n')
     a.register_codex_session(con,'socket','%1','123',{'session_id':'test-old-thread','transcript_path':str(path)})
     a.register_codex_session(con,'socket','%1','123',{'session_id':'test-old-thread'})
     self.assertEqual(con.execute('SELECT rollout_path FROM codex_sessions').fetchone()[0],str(path))
     con.execute('UPDATE codex_sessions SET cli_pid=999')
     a.register_codex_session(con,'socket','%1','123',{'session_id':'test-new-thread'})
     self.assertEqual(con.execute('SELECT thread_id,rollout_path FROM codex_sessions').fetchone()[:],('test-new-thread',''))
     self.assertEqual(con.execute('SELECT cli_pid FROM codex_sessions').fetchone()[0],0)
   finally:a.STATE=previous

class RepairTests(unittest.TestCase):
 def test_resumed_foreground_repairs_same_pid_native_root_once(self):
  with tempfile.TemporaryDirectory() as directory:
   home=Path(directory);(home/'.codex').mkdir()
   database=home/'.codex/state_5.sqlite';rollout=home/'root.jsonl'
   rollout.write_text(json.dumps({'type':'session_meta','payload':{'id':'actual','originator':'codex-tui','cwd':'/project','source':'vscode'}})+'\n'+json.dumps({'type':'event_msg','timestamp':'2026-10-03T07:00:00Z','payload':{'type':'task_started','turn_id':'actual-turn'}})+'\n')
   with closing(sqlite3.connect(database)) as source,source:
    source.execute('CREATE TABLE threads(id,rollout_path,cwd,agent_path,name)')
    source.execute('INSERT INTO threads VALUES(?,?,?,?,?)',('actual',str(rollout),'/project',None,'My task'))
   previous=a.STATE;a.STATE=home/'inbox'
   try:
    with closing(a.db()) as con:
     con.execute("INSERT INTO codex_sessions(socket,pane,pid,cli_pid,thread_id,rollout_path,origin) VALUES('sock','%1','123',456,'wrong','wrong-path','native')")
     a.record(con,'sock','%1','123','codex','IDLE','Unrelated completed response',True,event_time=2000000000,event_key='wrong-turn')
     table={123:(1,'zsh'),456:(123,'/vendor/bin/codex resume')}
     live={('%1','123'):{'path':'/project'}}
     screen='GPT medium · /project · My task        Goal achieved (1h 2m)\n← for agents'
     def scan():
      with patch('codex_observer.Path.home',return_value=home),patch('codex_observer.process_cwd',return_value='/project'),patch('codex_observer.tmux',return_value=screen):
       discover(con,'sock',live,table,{456:float('inf')})
     scan()
     mapping=con.execute('SELECT * FROM codex_sessions').fetchone()
     self.assertEqual((mapping['thread_id'],mapping['rollout_path'],mapping['origin']),('actual',str(rollout),'resume'))
     row=dict(con.execute('SELECT * FROM panes').fetchone())
     self.assertEqual((row['state'],row['event_key'],row['needs_review']),('RUNNING','actual-turn',0))
     self.assertLess(row['last_event_time'],2000000000)
     a.acknowledge(con,row)
     row=dict(con.execute('SELECT * FROM panes').fetchone())
     scan()
     self.assertEqual(dict(con.execute('SELECT * FROM panes').fetchone()),row)
     # A duplicate exact title is ambiguous; never choose newest-by-cwd or
     # override the mapping using a startup guess.
     second=home/'second.jsonl'
     second.write_text(json.dumps({'type':'session_meta','payload':{'id':'second','originator':'codex-tui','cwd':'/project','source':'vscode'}})+'\n')
     with closing(sqlite3.connect(database)) as source,source:
      source.execute('INSERT INTO threads VALUES(?,?,?,?,?)',('second',str(second),'/project',None,'My task'))
     con.execute("UPDATE codex_sessions SET thread_id='wrong',rollout_path='wrong-path',origin='native'");con.commit()
     scan()
     self.assertEqual(con.execute('SELECT thread_id FROM codex_sessions').fetchone()[0],'wrong')
     self.assertEqual(dict(con.execute('SELECT * FROM panes').fetchone()),row)
   finally:a.STATE=previous

 def test_child_completion_cannot_block_root_recovery(self):
  with tempfile.TemporaryDirectory() as directory:
   previous=a.STATE;a.STATE=Path(directory)
   try:
    root=Path(directory)/'root.jsonl'
    child=Path(directory)/'child.jsonl'
    root.write_text(json.dumps({'type':'session_meta','payload':{'id':'test-root','source':'vscode'}})+'\n'+json.dumps({'type':'event_msg','timestamp':'2026-10-03T07:00:00Z','payload':{'type':'task_started','turn_id':'root-turn'}})+'\n')
    child.write_text(json.dumps({'type':'session_meta','payload':{'id':'child','session_id':'test-root','source':{'subagent':{}}}})+'\n')
    database=Path(directory)/'threads.sqlite'
    with closing(sqlite3.connect(database)) as source,source:
     source.execute('CREATE TABLE threads(id,rollout_path)')
     source.execute('INSERT INTO threads VALUES(?,?)',('test-root',str(root)))
    with closing(a.db()) as con:
     con.execute("INSERT INTO codex_sessions(socket,pane,pid,thread_id,rollout_path,origin) VALUES('socket','%1','123','test-root',?,'native')",(str(child),))
     a.record(con,'socket','%1','123','codex','IDLE','Child finished',True,event_key='child-turn')
     mapping=dict(con.execute('SELECT * FROM codex_sessions').fetchone())
     self.assertTrue(repair_mapping(con,mapping,database))
     row=con.execute('SELECT * FROM panes').fetchone()
     self.assertEqual((row['state'],row['event_key'],row['needs_review']),('RUNNING','root-turn',0))
     mapping=dict(con.execute('SELECT * FROM codex_sessions').fetchone())
     self.assertEqual(mapping['rollout_path'],str(root))
     self.assertFalse(repair_mapping(con,mapping,database))
   finally:a.STATE=previous

if __name__=='__main__':unittest.main(verbosity=2)
