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
from unittest.mock import patch

root = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('attention', root/'attention.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)
with tempfile.TemporaryDirectory() as directory, patch.dict(
    os.environ,
    {key: value for key, value in os.environ.items()
     if not key.startswith('TMUX_ATTN_') and key not in ('TMUX', 'TMUX_PANE')} | {'HOME': directory},
    clear=True,
):
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
        for label in ['等待回覆 (1)','待看 (1)','工作進行中 (1)','閒置 (0)','claude']:
            assert label in screen,(label,screen)
        assert '待看 · 未看' not in screen and '待看 · 已看' not in screen,screen
        assert '🙋 1 · 👀 1 · 🏃 1 · 💤 0' in screen,screen
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
            items,_=a.items(con,sock,a.live_panes(sock),True)
            reviewed=next(item for item in items if item['pane']==row['pane'])
            assert row['needs_review'] and a.attention_category(reviewed)=='idle'
        os.write(master,b'q')
        drain(.4)
        assert popup.wait(timeout=3)==0,popup.communicate()
        popup=open_popup()
        screen=drain(1)
        assert '待看 (0)' in screen and '閒置 (1)' in screen,screen
        assert '🙋 1 · 👀 0 · 🏃 1 · 💤 1' in screen,screen
        assert '💤 · 已查看' in screen,screen
        with a.db() as con:
            a.record(con,sock,row['pane'],row['pid'],'claude','IDLE','Fixture IDLE',True,event_time=time.time()-3,event_key='next-turn')
        feedback=drain(1.2)
        # The completion moves from idle back to review when a new result arrives.
        assert '👀' in feedback,feedback
        with a.db() as con:
            row=con.execute("SELECT * FROM panes WHERE state='IDLE'").fetchone()
            assert row['revision']>row['acknowledged']
            items,_=a.items(con,sock,a.live_panes(sock),True)
            unread=next(item for item in items if item['pane']==row['pane'])
            assert a.attention_category(unread)=='review'
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
        popup=open_popup()
        drain(1)
        os.write(master,b'j\r')
        drain(.5)
        assert popup.wait(timeout=3)==0,popup.communicate()
        with a.db() as con:
            row=con.execute("SELECT * FROM panes WHERE state='IDLE'").fetchone()
            assert row['acknowledged']==row['revision']
            assert tmux('display-message','-p','-c',name,'#{pane_id}')==row['pane']
        popup=open_popup()
        screen=drain(1)
        assert '已查看' in screen,screen
        assert '待看 (0)' in screen and '閒置 (1)' in screen,screen
        assert '🙋 1 · 👀 0 · 🏃 1 · 💤 1' in screen,screen
        assert '💤 · 已查看' in screen,screen
        os.write(master,b'q')
        drain(.4)
        assert popup.wait(timeout=3)==0,popup.communicate()
        print('PASS: popup has four groups; acknowledgement moves review to idle; new completion restores review; Enter acknowledges completion; reviewed result stays visible; agent panes preserved.')
    finally:
        if popup and popup.poll() is None:
            popup.terminate()
        subprocess.run(command+['kill-server'],check=False)
        client.wait(timeout=3)
        os.close(master)
        os.close(slave)
