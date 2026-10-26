import json
import io
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
import attention as a
from codex_observer import approval_screen,read_lifecycle,startup_match,resumed_match,retire_mapping,repair_mapping,cursor_screen,native_cursor_pids,observe_cursor,discover,claude_screen,native_claude_pids,observe_claude,native_codex_pids,is_native_codex_cli,pi_screen,native_pi_pids
from codex_observer import queued_questions_screen,sync_attention_overlay,observe_codex_unmapped
from codex_observer import _darwin_procargs_environment, process_codex_home

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

 def test_standalone_discovery_routes_detached_hook_by_verified_session(self):
  with tempfile.TemporaryDirectory() as directory:
   home=Path(directory);(home/'.codex').mkdir()
   database=home/'.codex/state_5.sqlite';rollout=home/'root.jsonl'
   rollout.write_text(json.dumps({'type':'session_meta','payload':{'id':'root','originator':'codex-tui','cwd':'/project','source':'vscode'}})+'\n')
   with closing(sqlite3.connect(database)) as source,source:
    source.execute('CREATE TABLE threads(id,rollout_path,cwd,agent_path,name)')
    source.execute('INSERT INTO threads VALUES(?,?,?,?,?)',('root',str(rollout),'/project',None,'My task'))
   with patch.object(a,'STATE',home/'inbox'),closing(a.db()) as con:
    table={123:(1,'zsh'),456:(123,'codex --sandbox danger-full-access resume')}
    live={('%1','123'):{'path':'/project'}}
    screen='GPT medium · /project · My task\n← for agents'
    standalone=str(home/'.codex/packages/standalone/releases/version/bin/codex')
    executable=lambda pid,args='': standalone if pid == 456 else (args.split(None,1)[0] if args else '')
    with patch('codex_observer._process_environment',return_value=os.fsencode('HOME='+str(home))+b'\0'),patch('codex_observer.Path.home',return_value=home),patch('codex_observer.process_cwd',return_value='/project'),patch('codex_observer._process_exe',side_effect=executable),patch('codex_observer.tmux',return_value=screen):
     discover(con,'sock',live,table,{})
    mapping=con.execute('SELECT * FROM codex_sessions').fetchone()
    self.assertEqual((mapping['thread_id'],mapping['pane'],mapping['pid'],mapping['cli_pid'],mapping['origin']),('root','%1','123',456,'resume'))
    data={'session_id':'root','transcript_path':str(rollout)}
    with patch.object(a.sys,'stdin',io.StringIO(json.dumps(data))),patch.object(a.sys,'stdout',io.StringIO()),patch.object(a,'hook_target',return_value=None),patch.object(a,'tmux',return_value='%1 123'):
     a.event_command('codex','UserPromptSubmit')
    row=con.execute('SELECT * FROM panes').fetchone()
    self.assertEqual((row['socket'],row['pane'],row['pid'],row['state'],row['source']),('sock','%1','123','RUNNING','hook'))

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
  self.assertEqual(claude_screen(waiting),('RUNNING','Background work: waiting on 5 subagent',False))
  self.assertIsNone(claude_screen(waiting+'\n────\n❯\n────'))
  self.assertEqual(claude_screen(waiting+'\n'+done)[0],'IDLE')
  self.assertEqual(claude_screen(done)[0],'IDLE')
  for question in ('Which option?', 'Which option？'):
   self.assertEqual(claude_screen(done.replace('Finished the update.',question)),('IDLE','Response ends with a question; review suggested',True))
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

 def test_claude_background_wait_clears_when_a_done_screen_appears(self):
  done='⏺ Waiting on the deploy.\n✻ Sautéed for 7m 58s · done 下午2:46\n────\n❯\n────\n⏵⏵ auto mode on'
  table={123:(1,'zsh'),456:(123,'claude --resume root')}
  with tempfile.TemporaryDirectory() as directory:
   previous=a.STATE;a.STATE=Path(directory)
   try:
    with closing(a.db()) as con:
     live={('%1','123'):{'path':'/project'}}
     a.record(con,'sock','%1','123','claude','RUNNING','Background work: waiting on 1 shell',event_time=1000000000)
     def scan():
      with patch('codex_observer.tmux',return_value=done),patch('codex_observer.time.time',return_value=2000000000):
       observe_claude(con,'sock',live,table)
     before=dict(con.execute('SELECT * FROM panes').fetchone())
     scan()
     row=con.execute('SELECT * FROM panes').fetchone()
     self.assertEqual((row['state'],row['needs_review']),('IDLE',1))
     self.assertGreater(row['revision'],before['revision'])
   finally:a.STATE=previous

 def test_claude_explicit_wait_overrides_working_then_clears_on_completion(self):
  waiting='✻ Waiting for 2 background agents to finish\n'+'\n'.join(['agents panel']*20)
  done='⏺ Finished.\n✻ Sautéed for 3m 12s · done 下午2:46\n────\n❯\n────\n⏵⏵ auto mode on'
  table={123:(1,'zsh'),456:(123,'claude --resume root')}
  with tempfile.TemporaryDirectory() as directory:
   previous=a.STATE;a.STATE=Path(directory)
   try:
    with closing(a.db()) as con:
     live={('%1','123'):{'path':'/project'}}
     a.record(con,'sock','%1','123','claude','RUNNING','Working',event_time=1000000000)
     def scan(screen,now):
      with patch('codex_observer.tmux',return_value=screen),patch('codex_observer.time.time',return_value=now):
       observe_claude(con,'sock',live,table)
     scan(waiting,2000000000)
     row=con.execute('SELECT * FROM panes').fetchone()
     self.assertEqual((row['state'],row['reason'],row['needs_review']),
                      ('RUNNING','Background work: waiting on 2 subagent',0))
     scan(done,2000000003)
     row=con.execute('SELECT * FROM panes').fetchone()
     self.assertEqual((row['state'],row['needs_review']),('IDLE',1))
   finally:a.STATE=previous

 def test_cursor_footer_lifecycle_and_acknowledgement(self):
  def screen(body,active=False):
   return body+'\n\n → Add a follow-up'+('    ctrl+c to stop' if active else '')+'\n\n Auto · 16.6%    Run Everything\n /project · main\n'
  self.assertEqual(cursor_screen(screen('Done.'),'/project'),('IDLE','Response finished; review needed',True))
  for question in ('Pick one?', 'Pick one？'):
   self.assertEqual(cursor_screen(screen(question),'/project'),('IDLE','Response ends with a question; review suggested',True))
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
  self.assert_foreground_repairs_same_pid('/vendor/bin/codex resume')

 def test_new_foreground_repairs_same_pid_native_root_once(self):
  self.assert_foreground_repairs_same_pid('/vendor/bin/codex')

 def assert_foreground_repairs_same_pid(self, cli_args):
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
     a.remember_preview(con,'sock','%1','123',user='Old request',agent='Old response',transcript_path='wrong-path')
     table={123:(1,'zsh'),456:(123,cli_args)}
     live={('%1','123'):{'path':'/project'}}
     screen='GPT medium · /project · My task        Goal achieved (1h 2m)\n← for agents'
     def scan():
      with patch('codex_observer._process_environment',return_value=os.fsencode('HOME='+str(home))+b'\0'),patch('codex_observer.Path.home',return_value=home),patch('codex_observer.process_cwd',return_value='/project'),patch('codex_observer.tmux',return_value=screen):
       discover(con,'sock',live,table,{456:float('inf')})
     scan()
     mapping=con.execute('SELECT * FROM codex_sessions').fetchone()
     self.assertEqual((mapping['thread_id'],mapping['rollout_path'],mapping['origin']),('actual',str(rollout),'resume'))
     row=dict(con.execute('SELECT * FROM panes').fetchone())
     self.assertEqual((row['state'],row['event_key'],row['needs_review']),('RUNNING','actual-turn',0))
     self.assertEqual((row['transcript_path'],row['preview_users'],row['preview_agent'],row['preview_revision']),
                      (str(rollout),'[]','',0))
     self.assertLess(row['last_event_time'],2000000000)
     a.acknowledge(con,row)
     row=dict(con.execute('SELECT * FROM panes').fetchone())
     scan()
     self.assertEqual(dict(con.execute('SELECT * FROM panes').fetchone()),row)
     # Missing/wrong-cwd footers must preserve the current mapping, without
     # falling back to the thread that happened to start this CLI process.
     for unmatched in ('GPT medium · /other · My task\n← for agents',
                       'No current footer'):
      screen=unmatched
      with patch('codex_observer.startup_match') as startup:
       scan()
       startup.assert_not_called()
      self.assertEqual(con.execute('SELECT thread_id FROM codex_sessions').fetchone()[0],'actual')
      self.assertEqual(dict(con.execute('SELECT * FROM panes').fetchone()),row)
     screen='GPT medium · /project · My task\n← for agents'
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

 def test_observe_codex_unmapped_idle_prompt(self):
  table={10:(1,'zsh'),20:(10,'codex')}
  live={('%1','10'):{'pane':'%1','pid':'10','session':'s','window':'w','window_id':'@1','path':'/'}}
  screen='› Ask Codex to do anything\nGPT high · /tmp · demo\n← for agents'
  with tempfile.TemporaryDirectory() as directory:
   previous=a.STATE;a.STATE=Path(directory)
   try:
    with closing(a.db()) as con:
     with patch('codex_observer.native_codex_pids', return_value=[20]), \
          patch('codex_observer.tmux', return_value=screen):
      observe_codex_unmapped(con,'sock',live,table)
     row=con.execute('SELECT agent,state,reason,source FROM panes').fetchone()
     self.assertEqual(tuple(row),('codex','IDLE','Waiting for next task','observer'))
   finally:a.STATE=previous


class PiObserverTests(unittest.TestCase):
 def test_pi_screen_detects_confirm_and_working(self):
  self.assertEqual(pi_screen('Confirm?\n(Y/n)')[0], 'WAIT')
  self.assertEqual(pi_screen('Thinking…\nStreaming tokens')[0], 'RUNNING')
  self.assertIsNone(pi_screen('pi> ready'))

 def test_native_pi_pids_finds_cli(self):
  table={
   1:(0,'zsh'),
   2:(1,'pi --offline'),
   3:(1,'node /x/pi-coding-agent/dist/cli.js'),
   4:(1,'pi-agent daemon'),
   5:(1,'pi-bolt --model nocory/gemini-4-argon --thinking high'),
   6:(5,'pi-bolt --model nocory/gemini-4-argon --thinking high'),
   7:(6,'pi-bolt -p "headless subagent" --mode json'),
  }
  self.assertEqual(sorted(native_pi_pids(1, table)), [2, 3, 5])


class CodexProfileTests(unittest.TestCase):
 def test_target_environment_preserves_spaces_and_distinguishes_missing_from_unreadable(self):
  raw = b'OTHER=CODEX_HOME=/wrong\0CODEX_HOME=/tmp/custom profile\0SECRET=not printed\0'
  with patch('codex_observer._process_environment', return_value=raw):
   self.assertEqual(process_codex_home(123), Path('/tmp/custom profile'))
  with patch.dict(os.environ, {'HOME': '/observer-user', 'CODEX_HOME': '/observer-profile'}), \
       patch('codex_observer._process_environment', return_value=b'HOME=/target-user\0'):
   self.assertEqual(process_codex_home(123), Path('/target-user/.codex'))
  with patch('codex_observer._process_environment', return_value=b'HOME=/target user\0CODEX_HOME=~/profile with spaces\0'):
   self.assertEqual(process_codex_home(123), Path('/target user/profile with spaces'))
  with patch('codex_observer._process_environment', side_effect=PermissionError('denied')):
   with self.assertRaises(PermissionError):
    process_codex_home(123)
  for invalid in (b'CODEX_HOME=\0', b'CODEX_HOME=relative\0',
                  b'CODEX_HOME=/one\0CODEX_HOME=/two\0'):
   with self.subTest(raw=invalid), patch('codex_observer._process_environment', return_value=invalid):
    with self.assertRaises(ValueError):
     process_codex_home(123)

 def test_darwin_process_arguments_are_not_confused_with_environment_entries(self):
  raw = (struct.pack('=i', 3) + b'/Applications/Codex CLI/codex\0\0\0'
         + b'codex\0CODEX_HOME=/argv-decoy\0a title with spaces\0'
         + b'OTHER=value with spaces\0CODEX_HOME=/Users/me/profile with spaces\0\0')
  environment = _darwin_procargs_environment(raw)
  with patch('codex_observer._process_environment', return_value=environment):
   self.assertEqual(process_codex_home(123), Path('/Users/me/profile with spaces'))
  for incomplete in (b'', struct.pack('=i', 2) + b'codex\0\0codex\0'):
   with self.assertRaises(ValueError):
    _darwin_procargs_environment(incomplete)

 def test_linux_reads_the_target_proc_environment(self):
  with patch('codex_observer.sys.platform', 'linux'), \
       patch('codex_observer.Path.read_bytes', autospec=True, return_value=b'CODEX_HOME=/tmp/linux profile\0') as read:
   self.assertEqual(process_codex_home(123), Path('/tmp/linux profile'))
   read.assert_called_once_with(Path('/proc/123/environ'))

 @unittest.skipUnless(sys.platform == 'darwin' or sys.platform.startswith('linux'), 'requires native process environment access')
 def test_live_process_profile_and_home_are_independent_of_observer_environment(self):
  with tempfile.TemporaryDirectory(prefix='codex profile ') as directory:
   home = Path(directory)
   for override in (None, str(home / 'selected profile')):
    environment = os.environ | {'HOME': str(home)}
    environment.pop('CODEX_HOME', None)
    if override is not None:
     environment['CODEX_HOME'] = override
    with subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], env=environment) as target:
     try:
      with patch.dict(os.environ, {'HOME': '/observer-user', 'CODEX_HOME': '/observer-profile'}):
       self.assertEqual(process_codex_home(target.pid), Path(override) if override else home / '.codex')
     finally:
      target.terminate()
      target.wait(timeout=5)

 def make_profile(self, profile, thread, started=1761413573, cwd='/project'):
  profile.mkdir(parents=True)
  rollout = profile / 'root.jsonl'
  meta = {'id': thread, 'originator': 'codex-tui', 'cwd': cwd, 'source': 'vscode',
          'timestamp': datetime.fromtimestamp(started + 6.431, timezone.utc).isoformat()}
  rollout.write_text('\n'.join(json.dumps(entry) for entry in [
   {'type': 'session_meta', 'payload': meta},
   {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'Investigate preview'}},
   {'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': thread + ' completed result'}},
  ]) + '\n')
  with closing(sqlite3.connect(profile / 'state_5.sqlite')) as source, source:
   source.execute('CREATE TABLE threads(id,rollout_path,cwd,agent_path,name,created_at)')
   source.execute('INSERT INTO threads VALUES(?,?,?,?,?,?)',
                  (thread, str(rollout), cwd, None, 'My task', started + 6))
  return rollout

 def test_delayed_discovery_expands_footer_with_target_home_outside_custom_profile(self):
  with tempfile.TemporaryDirectory() as directory:
   root = Path(directory)
   target_home = root / 'target user'
   observer_home = root / 'observer user'
   cwd = str(target_home / 'project')
   profile = root / 'separate configs' / 'selected profile'
   rollout = self.make_profile(profile, 'root', cwd=cwd)
   table = {123: (1, 'zsh'), 456: (123, '/vendor/bin/codex')}
   live = {('%1', '123'): {'path': cwd}}
   screen = 'GPT medium · ~/project · My task · Main [default]\n← for agents'
   environment = os.fsencode('HOME=' + str(target_home)) + b'\0' + os.fsencode('CODEX_HOME=' + str(profile)) + b'\0'
   with patch.object(a, 'STATE', root / 'inbox'), closing(a.db()) as con, \
        patch.dict(os.environ, {'HOME': str(observer_home), 'CODEX_HOME': '/observer-profile'}), \
        patch('codex_observer._process_environment', return_value=environment) as read_environment, \
        patch('codex_observer.process_cwd', return_value=cwd), \
        patch('codex_observer.process_start', return_value=1761413573), \
        patch('codex_observer.tmux', return_value=screen):
    self.assertIsNone(startup_match(profile / 'state_5.sqlite', cwd, 1761413573))
    self.assertIsNone(resumed_match(profile / 'state_5.sqlite', cwd, screen))
    a.record(con, 'sock', '%1', '123', 'codex', 'IDLE', 'Response finished', True, source='observer')
    discover(con, 'sock', live, table, {})
    read_environment.assert_called_once_with(456)
    mapping = con.execute('SELECT thread_id,rollout_path,origin FROM codex_sessions').fetchone()
    self.assertEqual(tuple(mapping), ('root', str(rollout), 'resume'))
    row = dict(con.execute('SELECT * FROM panes').fetchone())
    self.assertEqual(a.pane_conversation_preview(con, row, {'path': cwd}),
                     {'users': ['Investigate preview'], 'agent': 'root completed result'})

 def test_delayed_unmapped_clis_bind_their_own_profile_and_restore_preview(self):
  with tempfile.TemporaryDirectory() as directory:
   home = Path(directory)
   default = self.make_profile(home / '.codex', 'default-root')
   custom_home = home / 'custom profile'
   custom = self.make_profile(custom_home, 'custom-root')
   table = {123: (1, 'zsh'), 456: (123, '/vendor/bin/codex'),
            234: (1, 'zsh'), 789: (234, '/vendor/bin/codex')}
   live = {('%1', '123'): {'path': '/project'}, ('%2', '234'): {'path': '/project'}}
   environment = lambda pid: os.fsencode('CODEX_HOME=' + str(custom_home)) + b'\0' if pid == 456 else os.fsencode('HOME=' + str(home)) + b'\0'
   screen = 'GPT medium · /project · My task · Main [default]\n← for agents'
   with patch.object(a, 'STATE', home / 'inbox'), closing(a.db()) as con, \
        patch('codex_observer.Path.home', return_value=home), \
        patch('codex_observer.process_cwd', return_value='/project'), \
        patch('codex_observer.process_start', return_value=1761413573), \
        patch('codex_observer.tmux', return_value=screen):
    for pane, pid in live:
     a.record(con, 'sock', pane, pid, 'codex', 'IDLE', 'Response finished', True, source='observer')
    with patch('codex_observer._process_environment', side_effect=PermissionError('denied')):
     discover(con, 'sock', live, table, {})
    self.assertEqual(con.execute('SELECT count(*) FROM codex_sessions').fetchone()[0], 0)
    with patch.dict(os.environ, {'CODEX_HOME': '/observer-must-not-control-targets'}), \
         patch('codex_observer._process_environment', side_effect=environment):
     discover(con, 'sock', live, table, {})
    mappings = con.execute('SELECT pane,thread_id,rollout_path,origin FROM codex_sessions ORDER BY pane').fetchall()
    self.assertEqual([tuple(row) for row in mappings], [
     ('%1', 'custom-root', str(custom), 'resume'), ('%2', 'default-root', str(default), 'resume')])
    for row in con.execute('SELECT * FROM panes').fetchall():
     preview = a.pane_conversation_preview(con, dict(row), {'path': '/project'})
     root = 'custom-root' if row['pane'] == '%1' else 'default-root'
     self.assertEqual(preview, {'users': ['Investigate preview'], 'agent': root + ' completed result'})

 def test_native_registration_and_repair_use_the_selected_profile(self):
  with tempfile.TemporaryDirectory() as directory:
   home = Path(directory)
   self.make_profile(home / '.codex', 'same-root')
   custom_home = home / 'custom profile'
   root = self.make_profile(custom_home, 'same-root')
   child = home / 'child.jsonl'
   child.write_text(json.dumps({'type': 'session_meta', 'payload': {
    'id': 'child', 'source': {'subagent': {'parent_thread_id': 'same-root'}}}}) + '\n')
   with patch.object(a, 'STATE', home / 'inbox'), closing(a.db()) as con, \
        patch('attention.Path.home', return_value=home), \
        patch.dict(os.environ, {'CODEX_HOME': str(custom_home)}):
    a.register_codex_session(con, 'sock', '%1', '123', {'session_id': 'same-root'})
    self.assertEqual(con.execute('SELECT rollout_path FROM codex_sessions').fetchone()[0], str(root))
    con.execute('UPDATE codex_sessions SET rollout_path=?,cli_pid=456', (str(child),))
    con.commit()
    mapping = dict(con.execute('SELECT * FROM codex_sessions').fetchone())
    with patch('codex_observer._process_environment', side_effect=PermissionError('denied')):
     self.assertFalse(repair_mapping(con, mapping))
    self.assertEqual(con.execute('SELECT rollout_path FROM codex_sessions').fetchone()[0], str(child))
    with patch('codex_observer._process_environment', return_value=os.fsencode('CODEX_HOME=' + str(custom_home)) + b'\0'):
     self.assertTrue(repair_mapping(con, mapping))
    self.assertEqual(con.execute('SELECT rollout_path FROM codex_sessions').fetchone()[0], str(root))
    with patch.dict(os.environ, {'CODEX_HOME': ''}):
     a.register_codex_session(con, 'sock', '%2', '234', {'session_id': 'same-root'})
    self.assertEqual(con.execute("SELECT rollout_path FROM codex_sessions WHERE pane='%2'").fetchone()[0], '')


if __name__=='__main__':unittest.main(verbosity=2)
