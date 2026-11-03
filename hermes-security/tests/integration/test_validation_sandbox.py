import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import hashlib
import threading
import time
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
from tests.support import make_link
from hermes_security.errors import PolicyDenied
from hermes_security.validation.policy import mint_grant
from hermes_security.validation.runner import run_local,run_http,network_isolation_available

class Store:
    def __init__(self): self.grants={}
    def grant_validation(self,scan_id,grant): self.grants[grant['grantId']]=deepcopy(grant);return deepcopy(grant)
    def get_grant(self,grant_id): return deepcopy(self.grants[grant_id])
    def revoke_grant(self,grant_id): self.grants.pop(grant_id)

def local_plan(commands):
    return {'candidateId':'c','kind':'local-command','level':'local-safe','commands':commands,
            'positiveControl':{'commandIndex':0,'expectedMarker':'VULN-MARKER'},'negativeControl':{'commandIndex':1},
            'cleanup':{'strategy':'remove-copy'},'timeoutS':2,'maxOutputBytes':4096}

def digest(root):
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}

def test_disposable_paired_controls(tmp_path):
    target=tmp_path/'source';target.mkdir()
    (target/'harness.py').write_text("import sys\nprint('VULN-MARKER' if '../' in sys.argv[1] else 'SAFE')\nopen('generated','w').write('disposable')\n")
    p=local_plan([[sys.executable,'harness.py','../'],[sys.executable,'harness.py','benign']])
    before=digest(target)
    r=run_local(p,target=target,workdir_parent=tmp_path)
    assert r['status']=='passed' and r['cleanup']['done'] and digest(target)==before
    assert any('network isolation:' in n for n in r['notes'])
    assert not list(tmp_path.glob('validation-*'))

@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX fork//proc variant; see test_timeout_kills_job_tree')
def test_timeout_kills_process_group(tmp_path):
    import os
    target=tmp_path/'source';target.mkdir()
    code='import os,time; pid=os.fork(); print("PID="+str(os.getpid()),flush=True); time.sleep(10)'
    p=local_plan([[sys.executable,'-c',code],[sys.executable,'-c','print("SAFE")']]);p['timeoutS']=.2
    r=run_local(p,target=target,workdir_parent=tmp_path)
    pids=[int(line[4:]) for line in r['outputExcerpt'].splitlines() if line.startswith('PID=')]
    assert len(pids)==2 and r['positive']['timeout']
    for pid in pids:
        status=Path(f'/proc/{pid}/stat')
        # Reparented dead children may remain zombies briefly until init reaps them.
        assert not status.exists() or status.read_text().split()[2]=='Z'

@pytest.mark.skipif(sys.platform != 'win32', reason='Windows Job Object variant of test_timeout_kills_process_group')
def test_timeout_kills_job_tree(tmp_path):
    import ctypes
    target=tmp_path/'source';target.mkdir()
    # Parent spawns a grandchild, both report PIDs, then both sleep past the timeout.
    code=('import subprocess,sys,os,time;'
          'c=subprocess.Popen([sys.executable,"-c","import os,time;print(\\"PID=\\"+str(os.getpid()),flush=True);time.sleep(30)"]);'
          'print("PID="+str(os.getpid()),flush=True);time.sleep(30)')
    p=local_plan([[sys.executable,'-c',code],[sys.executable,'-c','print("SAFE")']]);p['timeoutS']=2
    r=run_local(p,target=target,workdir_parent=tmp_path)
    pids=[int(line[4:]) for line in r['outputExcerpt'].splitlines() if line.startswith('PID=')]
    assert len(pids)==2 and r['positive']['timeout']
    kernel32=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel32.OpenProcess.restype=ctypes.c_void_p
    kernel32.OpenProcess.argtypes=[ctypes.c_ulong,ctypes.c_int,ctypes.c_ulong]
    kernel32.GetExitCodeProcess.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_ulong)]
    kernel32.CloseHandle.argtypes=[ctypes.c_void_p]
    for pid in pids:
        h=kernel32.OpenProcess(0x1000,False,pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h: continue  # already gone
        code_=ctypes.c_ulong()
        try:
            assert kernel32.GetExitCodeProcess(h,ctypes.byref(code_))
            assert code_.value!=259, f'pid {pid} still running'  # STILL_ACTIVE
        finally: kernel32.CloseHandle(h)

@pytest.mark.skipif(sys.platform != 'win32', reason='junctions are Windows-only')
@pytest.mark.parametrize('form',['dangling','malformed'])
def test_hostile_junction_escape_refused(tmp_path,form):
    # A malformed junction (doubled \\?\ prefix) makes resolve() return the link's
    # own path; the stored target must be checked too, failing closed.
    import _winapi
    target=tmp_path/'source';target.mkdir()
    gone=tmp_path/'outside';gone.mkdir()
    _winapi.CreateJunction(('\\\\?\\' if form=='malformed' else '')+str(gone),str(target/'escape'))
    if form=='dangling': gone.rmdir()
    else: assert (target/'escape').resolve()==target/'escape'  # the resolve() blind spot
    p=local_plan([[sys.executable,'-c','print(1)']]*2)
    with pytest.raises(PolicyDenied,match='symlink'): run_local(p,target=target,workdir_parent=tmp_path)
    assert not list(tmp_path.glob('validation-*'))

def test_symlink_refused(tmp_path):
    target=tmp_path/'source';target.mkdir();make_link(target/'escape',tmp_path,directory=True)
    p=local_plan([[sys.executable,'-c','print(1)']]*2)
    with pytest.raises(PolicyDenied,match='symlink'): run_local(p,target=target,workdir_parent=tmp_path)
    assert not list(tmp_path.glob('validation-*'))

def test_timeout_output_and_redaction(tmp_path):
    target=tmp_path/'source';target.mkdir()
    p=local_plan([[sys.executable,'-c','import time;time.sleep(10)'],[sys.executable,'-c','print("SAFE")']]);p['timeoutS']=.15
    started=time.monotonic();r=run_local(p,target=target,workdir_parent=tmp_path)
    assert time.monotonic()-started<3 and r['positive']['timeout'] and r['status']=='inconclusive' and r['cleanup']['done']
    p=local_plan([[sys.executable,'-c','print("X"*100000)'],[sys.executable,'-c','print("password=never-record-me")']]);p['maxOutputBytes']=256
    r=run_local(p,target=target,workdir_parent=tmp_path)
    assert r['positive']['truncated'] and len(r['outputExcerpt'])<=4000 and 'never-record-me' not in str(r)

@pytest.fixture
def servers():
    hits=[]
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append((self.server.server_port,self.path))
            if self.path=='/redirect':
                self.send_response(302);self.send_header('Location',f'http://127.0.0.1:{other.server_port}/off');self.end_headers()
            else:
                self.send_response(200);self.end_headers();self.wfile.write(b'VULN-MARKER' if self.path=='/positive' else b'SAFE')
        def log_message(self,*args): pass
    other=ThreadingHTTPServer(('127.0.0.1',0),Handler);main=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threads=[threading.Thread(target=s.serve_forever,daemon=True) for s in [main,other]]
    for t in threads:t.start()
    yield main,other,hits
    for s in [main,other]:s.shutdown();s.server_close()
    for t in threads:t.join()

def test_http_origin_cap_and_redirect(servers):
    main,other,hits=servers;origin=f'http://127.0.0.1:{main.server_port}'
    s=Store();g=mint_grant(s,'scan',origins=[origin],actions=['http-probe'],expires_in_s=60,max_requests=2)
    p={'scanId':'scan','candidateId':'c','level':'active-authorized','kind':'http-probe',
       'requests':[{'url':origin+'/positive'},{'url':origin+'/negative'}],
       'positiveControl':{'requestIndex':0,'expectedMarker':'VULN-MARKER'},'negativeControl':{'requestIndex':1},'cleanup':{'strategy':'none-needed'}}
    r=run_http(p,grant=g,store=s)
    assert r['status']=='passed' and s.get_grant(g['grantId'])['used']==2
    with pytest.raises(PolicyDenied,match='remaining'):run_http(p,grant=g,store=s)
    g=mint_grant(s,'scan',origins=[origin],actions=['http-probe'],expires_in_s=60,max_requests=2)
    p['requests'][0]['url']=origin+'/redirect'
    r=run_http(p,grant=g,store=s)
    assert r['status']=='inconclusive' and 'cross-origin redirect refused' in r['notes']
    assert not any(port==other.server_port for port,path in hits)
