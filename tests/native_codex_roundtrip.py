import json,os,shlex,sqlite3,subprocess,tempfile,threading,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_POST(self):
  self.rfile.read(int(self.headers.get('Content-Length','0')))
  response={'id':'resp_attn_local','object':'response','created_at':int(time.time()),'status':'completed','model':'attn-local','output':[],'usage':{'input_tokens':1,'output_tokens':1,'total_tokens':2}}
  message={'id':'msg_attn_local','type':'message','status':'completed','role':'assistant','content':[]}
  part={'type':'output_text','text':'','annotations':[]}
  events=[('response.created',{'response':response}),('response.output_item.added',{'output_index':0,'item':message}),('response.content_part.added',{'output_index':0,'item_id':message['id'],'content_index':0,'part':part}),('response.output_text.delta',{'output_index':0,'item_id':message['id'],'content_index':0,'delta':'Local validation complete.'})]
  part=part|{'text':'Local validation complete.'};message=message|{'content':[part]}
  events += [('response.output_text.done',{'output_index':0,'item_id':message['id'],'content_index':0,'text':part['text']}),('response.content_part.done',{'output_index':0,'item_id':message['id'],'content_index':0,'part':part}),('response.output_item.done',{'output_index':0,'item':message}),('response.completed',{'response':response|{'output':[message]}})]
  body=''.join('event: '+name+'\ndata: '+json.dumps({'type':name,'sequence_number':i}|data)+'\n\n' for i,(name,data) in enumerate(events)).encode()
  self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
with tempfile.TemporaryDirectory(prefix='attn-codex-roundtrip-') as directory:
 label='attn-native-'+str(os.getpid());tmux=['tmux','-L',label]
 args=['env','TMUX_ATTN_STATE_DIR='+directory,'codex','--no-alt-screen','-c','model_provider="attn-local"','-c','model="attn-local"','-c','model_providers.attn-local.name="Local validation"','-c',f'model_providers.attn-local.base_url="http://127.0.0.1:{server.server_port}"','-c','model_providers.attn-local.wire_api="responses"']
 subprocess.run(tmux+['-f','/dev/null','new-session','-d','-s','validation','-x','180','-y','45','-c',str(Path(__file__).resolve().parents[1]),shlex.join(args)],check=True)
 try:
  time.sleep(1)
  sock=subprocess.check_output(tmux+['display-message','-p','#{socket_path}'],text=True).strip()
  subprocess.run(tmux+['send-keys','-t','validation','Validate the local attention integration.','Enter'],check=True)
  time.sleep(.5)
  subprocess.run(tmux+['send-keys','-t','validation','Enter'],check=True)
  found=False
  for _ in range(100):
   for path in [Path(directory)/'inbox.sqlite3',Path.home()/'.local/state/tmux-attn/inbox.sqlite3']:
    if not path.exists(): continue
    with sqlite3.connect(path) as con:
     row=con.execute('SELECT agent,state,needs_review,revision FROM panes WHERE socket=? ORDER BY updated DESC LIMIT 1',(sock,)).fetchone()
    if row and row[1]=='IDLE' and row[2]:
     print('Codex native full roundtrip:',row);found=True;break
   if found: break
   time.sleep(.1)
  if not found:
   print(subprocess.check_output(tmux+['capture-pane','-p','-t','validation'],text=True))
   raise RuntimeError('No native Codex completed event received')
 finally:
  subprocess.run(tmux+['kill-server'],check=False)
  server.shutdown()
