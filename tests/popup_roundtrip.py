"""Exercise the real popup through a disposable attached tmux client."""
import fcntl
import importlib.util
import os
from pathlib import Path
import pty
import select
import shlex
import struct
import subprocess
import sys
import tempfile
import termios
import time

root = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('attention', root/'attention.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)
with tempfile.TemporaryDirectory() as directory:
    command = ['tmux', '-L', 'attn-popup-'+str(os.getpid())]
    def tmux(*args):
        return subprocess.check_output(command+list(args),text=True).strip()
    subprocess.run(command+['-f','/dev/null','new-session','-d','-s','fixture','-x','160','-y','45','/bin/sh'],check=True)
    master, slave = pty.openpty()
    fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',45,160,0,0))
    client = subprocess.Popen(command+['attach-session','-t','fixture'],stdin=slave,stdout=slave,stderr=slave,env=os.environ|{'TERM':'xterm-256color'})
    popup = None
    try:
        def drain(seconds):
            output=b''
            end=time.monotonic()+seconds
            while time.monotonic()<end:
                if select.select([master],[],[],.05)[0]:
                    output+=os.read(master,65536)
            return output.decode(errors='replace')
        drain(.5)
        name=tmux('list-clients','-F','#{client_name}')
        sock=tmux('display-message','-p','#{socket_path}')
        wait_pane=tmux('display-message','-p','#{pane_id}')
        a.STATE=Path(directory)
        con=a.db()
        for state in ['WAIT','IDLE','RUNNING']:
            pane=wait_pane if state=='WAIT' else tmux('new-window','-d','-P','-F','#{pane_id}','/bin/sh')
            pid=tmux('display-message','-p','-t',pane,'#{pane_pid}')
            a.record(con,sock,pane,pid,'claude',state,'Fixture '+state,state=='IDLE',event_time=time.time()-4)
        con.close()
        launch=shlex.join(['env','TMUX_ATTN_STATE_DIR='+directory,sys.executable,str(root/'attention.py'),'inbox',name])
        def open_popup():
            return subprocess.Popen(command+['display-popup','-E','-w','90%','-h','85%','-t',name,launch],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        popup=open_popup()
        screen=drain(1.5)
        for label in ['等待回覆','閒置','運行中','claude']:
            assert label in screen,(label,screen)
        os.write(master,b'd')
        feedback=drain(.5)
        assert '已查看' in feedback,feedback
        with a.db() as con:
            row=con.execute('SELECT * FROM panes WHERE pane=?',(wait_pane,)).fetchone()
            assert row['acknowledged']==row['revision'] and row['state']=='WAIT'
        os.write(master,b'jd')
        feedback=drain(.5)
        assert '已查看' in feedback,feedback
        with a.db() as con:
            row=con.execute("SELECT * FROM panes WHERE state='IDLE'").fetchone()
            assert row['acknowledged']==row['revision']
            a.record(con,sock,row['pane'],row['pid'],'claude','IDLE','Fixture IDLE',True,event_time=time.time()-3,event_key='next-turn')
        feedback=drain(1.2)
        # Curses emits only the changed suffix of the existing IDLE label.
        assert '• · claude' in feedback,feedback
        with a.db() as con:
            row=con.execute("SELECT * FROM panes WHERE state='IDLE'").fetchone()
            assert row['revision']>row['acknowledged']
        os.write(master,b'q')
        drain(.4)
        assert popup.wait(timeout=3)==0,popup.communicate()
        assert len(tmux('list-panes','-a','-F','#{pane_id}').splitlines())==3
        other=tmux('list-panes','-a','-F','#{pane_id}').splitlines()[-1]
        tmux('switch-client','-c',name,'-t',other)
        popup=open_popup()
        drain(1)
        os.write(master,b'\r')
        drain(.4)
        assert popup.wait(timeout=3)==0,popup.communicate()
        assert tmux('display-message','-p','-c',name,'#{pane_id}')==wait_pane
        print('Popup: three groups; d updates DB and reviewed labels; new completion restores unread dot; q closes; Enter jumps; agent panes preserved.')
    finally:
        if popup and popup.poll() is None:
            popup.terminate()
        subprocess.run(command+['kill-server'],check=False)
        client.wait(timeout=3)
        os.close(master)
        os.close(slave)
