import os,shlex,sqlite3,subprocess,tempfile,time
from pathlib import Path
with tempfile.TemporaryDirectory(prefix='attn-pi-') as directory:
 label='attn-pi-validation-'+str(os.getpid())
 tmux=['tmux','-L',label]
 command=shlex.join(['env','TMUX_ATTN_STATE_DIR='+directory,'pi','--offline','--no-extensions','-e',str(Path.home()/'.pi/agent/extensions/tmux-attention.ts'),'-e',str(Path(__file__).resolve().parent/'fixtures/pi-confirm.ts'),'--no-skills','--no-prompt-templates','--no-context-files'])
 subprocess.run(tmux+['-f','/dev/null','new-session','-d','-s','validation','-x','180','-y','45',command],check=True)
 try:
  def state():
   path=Path(directory)/'inbox.sqlite3'
   if not path.exists(): return None
   with sqlite3.connect(path) as con:
    row=con.execute('SELECT state FROM panes ORDER BY updated DESC LIMIT 1').fetchone()
    return row[0] if row else None
  def wait(expected):
   for _ in range(100):
    if state()==expected: print('Pi native event state:',expected); return
    time.sleep(.1)
   raise RuntimeError('Pi state expected '+expected+', got '+str(state()))
  wait('IDLE')
  subprocess.run(tmux+['send-keys','-t','validation','/attncheck','Enter'],check=True)
  wait('WAIT')
  subprocess.run(tmux+['send-keys','-t','validation','Enter'],check=True)
  wait('IDLE')
 finally:
  subprocess.run(tmux+['kill-server'],check=False)
