"""Real SGR mouse input through an attached disposable tmux client."""
import fcntl
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
import attention as a
ROOT=Path(__file__).resolve().parent.parent
with tempfile.TemporaryDirectory() as directory:
    home=Path(directory)
    command=['tmux','-L','attn-click-'+str(os.getpid())]
    def tmux(*args):return subprocess.check_output(command+list(args),text=True).strip()
    subprocess.run(command+['-f','/dev/null','new-session','-d','-s','a','-x','120','-y','35','/bin/sh'],check=True)
    master,slave=pty.openpty()
    fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',35,120,0,0))
    client=subprocess.Popen(command+['attach-session','-t','a'],stdin=slave,stdout=slave,stderr=slave,env=os.environ|{'TERM':'xterm-256color'})
    try:
        def drain(seconds=.2):
            end=time.monotonic()+seconds
            while time.monotonic()<end:
                if select.select([master],[],[],.02)[0]:os.read(master,65536)
        drain(.4)
        name=tmux('list-clients','-F','#{client_name}')
        sock=tmux('display-message','-p','#{socket_path}')
        pane_a=tmux('display-message','-p','#{pane_id}')
        pane_b=tmux('new-session','-d','-s','b','-P','-F','#{pane_id}','/bin/sh')
        targets=[pane_a,pane_b]
        for state in ['review','running','idle']:
            if state=='running':
                targets.append(tmux('split-window','-d','-t',targets[-1],'-P','-F','#{pane_id}','/bin/sh'))
            else:
                targets.append(tmux('new-window','-d','-t','b:','-P','-F','#{pane_id}','/bin/sh'))
        tmux('bind-key','-T','root','MouseDown1Control0','display-message','original-control')
        tmux('bind-key','-T','root','DoubleClick1Control0','display-message','original-double')
        tmux('bind-key','-T','prefix','a','display-message','original-prefix')
        prior=tmux('list-keys','-T','root')
        original_status=next(line for line in prior.splitlines() if ' MouseDown1Status ' in line)
        tmux('set-option','-g','mouse','off')
        env=os.environ|{'HOME':directory,'TMUX':f'{sock},{tmux("display-message","-p","#{pid}")},0','TMUX_PANE':pane_a}
        env.pop('TMUX_ATTN_STATE_DIR',None)
        import json
        result=json.loads(subprocess.check_output([sys.executable,str(ROOT/'install.py'),'--enable-codex-hooks'],env=env,text=True))
        assert 'original-control' in (Path(result['backup'])/'key-root-MouseDown1Control0.conf').read_text()
        assert 'original-prefix' in (Path(result['backup'])/'key-a.conf').read_text()
        assert original_status in tmux('list-keys','-T','root')
        a.STATE=home/'.local/state/tmux-attn'
        with a.db() as con:
            for pane,state,review in zip(targets,['WAIT','WAIT','IDLE','RUNNING','IDLE'],[False,False,True,False,False]):
                pid=tmux('display-message','-p','-t',pane,'#{pane_pid}')
                a.record(con,sock,pane,pid,'fixture',state,'Fixture',review,event_time=time.time()-5)
            before=[dict(row) for row in con.execute('SELECT * FROM panes ORDER BY pane')]
        # Keep installed bindings and actual generated ranges. Remove clock only so
        # mouse coordinates are independent of local status customization.
        app=home/'.local/share/tmux-attn/attention.py'
        import shlex
        launch=shlex.join(['env','TMUX_ATTN_STATE_DIR='+str(a.STATE),sys.executable,str(app),'status-clickable'])
        tmux('set-option','-g','status-right',f'#({launch})')
        tmux('set-option','-g','status-left','')
        tmux('set-option','-gw','window-status-format','')
        tmux('set-option','-gw','window-status-current-format','')
        tmux('refresh-client','-S','-t',name)
        drain(1.2)
        def active():return tmux('display-message','-p','-c',name,'#{pane_id}')
        def click(x,expected):
            os.write(master,f'\x1b[<0;{x};35M\x1b[<0;{x};35m'.encode())
            end=time.monotonic()+3
            while active()!=expected and time.monotonic()<end:drain(.05)
            assert active()==expected,(x,expected,active())
            drain(.05)
        # Visible text occupies 25 cells, right-aligned at columns 96..120.
        click(97,pane_a)
        click(97,pane_b)
        click(97,pane_a)
        click(97,pane_b)
        click(104,targets[2])
        click(111,targets[3])
        click(118,targets[4])
        click(97,pane_a)  # independent category cursor survives other categories
        with a.db() as con:
            assert before==[dict(row) for row in con.execute('SELECT * FROM panes ORDER BY pane')]
            con.execute("UPDATE panes SET state='IDLE',needs_review=0 WHERE state='WAIT'")
            con.commit()
        drain(2.2)
        click(97,pane_a)  # zero count leaves current pane unchanged
        subprocess.run([sys.executable,str(ROOT/'rollback.py'),result['backup']],env=env,check=True,capture_output=True)
        assert tmux('show-option','-gv','mouse')=='off'
        assert 'original-control' in tmux('list-keys','-T','root')
        assert 'original-double' in tmux('list-keys','-T','root')
        assert 'original-prefix' in tmux('list-keys','-T','prefix')
        print('PASS: real mouse clicks, four groups, cross-session/pane, wraparound, rapid clicks, zero count, unchanged acknowledgement and rollback')
    finally:
        client.terminate()
        try:client.wait(timeout=3)
        except subprocess.TimeoutExpired:client.kill();client.wait()
        os.close(master);os.close(slave)
        subprocess.run(command+['kill-server'],capture_output=True)
