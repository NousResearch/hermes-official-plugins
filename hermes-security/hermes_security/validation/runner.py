"""Bounded disposable-copy harnesses and exact-origin HTTP probes.

A copy plus rlimits is not a hostile-code filesystem sandbox. Without unshare,
proxy variables are best-effort only; callers must expose this limitation.
"""
import functools
import math
import os
from pathlib import Path
try:
    import resource
except ImportError:  # Windows has no POSIX resource-limit API.
    resource = None
import selectors
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from ..canonical import utcnow
from ..errors import PolicyDenied, NotFound
from ..fsutil import is_link
from .policy import check_plan, normalize_origin
from .plans import execution_limits, control_spec
from .receipts import build_receipt

_GRANT_LOCK=threading.RLock()

@functools.lru_cache(maxsize=1)
def network_isolation_available():
    """Probe the actual current user's namespace support once, not just PATH."""
    executable=shutil.which('unshare')
    if not executable: return False
    try:
        return subprocess.run([executable,'-rn','--','true'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=3).returncode==0
    except (OSError,subprocess.TimeoutExpired): return False

def _process_limit():
    if resource is None: return None
    # RLIMIT_NPROC counts the entire real UID's threads, not this harness.
    # Preserve generous room for real test suites without breaking busy hosts.
    if not hasattr(resource,'RLIMIT_NPROC') or os.geteuid()==0: return None
    try:
        threads=sum(len(list((p/'task').iterdir())) for p in Path('/proc').iterdir()
                    if p.name.isdigit() and p.stat().st_uid==os.getuid())
    except OSError:
        return None  # cannot safely establish a per-UID baseline
    _,hard=resource.getrlimit(resource.RLIMIT_NPROC)
    return threads+PROCESS_HEADROOM if hard==resource.RLIM_INFINITY else min(threads+PROCESS_HEADROOM,hard)

# Runaway guards, not a sandbox: sized so real test suites (jest, gradle, cargo)
# fit. No address-space cap: Node, Java and Go reserve large virtual ranges.
PROCESS_HEADROOM=512
FILE_SIZE_CAP=2*1024**3

def _cpu_seconds(timeout):
    # CPU time accrues on every core, so allow wall-time x cores.
    return max(1,math.ceil(timeout))*max(1,os.cpu_count() or 1)

def _limits(timeout,nproc):
    assert resource is not None  # _command refuses unsupported platforms.
    cpu=_cpu_seconds(timeout)
    resource.setrlimit(resource.RLIMIT_CPU,(cpu,cpu+1))
    resource.setrlimit(resource.RLIMIT_FSIZE,(FILE_SIZE_CAP,FILE_SIZE_CAP))
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    if nproc is not None: resource.setrlimit(resource.RLIMIT_NPROC,(nproc,nproc))

def _kill(proc):
    try: os.killpg(proc.pid,signal.SIGKILL)
    except ProcessLookupError: pass

_SECRET_NAME=('TOKEN','SECRET','PASSWORD','PASSWD','API_KEY','APIKEY','ACCESS_KEY','PRIVATE_KEY','CREDENTIAL','SESSION_KEY')

def _test_env(cwd):
    """Run like the user's own shell (PATH, toolchains, HOME) minus credentials."""
    env={k:v for k,v in os.environ.items() if not any(s in k.upper() for s in _SECRET_NAME)}
    env.update(TMPDIR=str(cwd),TEMP=str(cwd),TMP=str(cwd))
    return env

def _command(argv,cwd,timeout,cap,isolated):
    if os.name=='nt':
        return _command_nt(argv,cwd,timeout,cap)
    if resource is None:
        raise PolicyDenied('local validation requires POSIX process limits')
    env=_test_env(cwd)
    if isolated: argv=[shutil.which('unshare'),'-rn','--',*argv]
    nproc=_process_limit()
    proc=subprocess.Popen(argv,cwd=cwd,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                          start_new_session=True,preexec_fn=lambda:_limits(timeout,nproc))
    data=bytearray(); timed=False; truncated=False; deadline=time.monotonic()+timeout
    try:
        with selectors.DefaultSelector() as sel:
            sel.register(proc.stdout,selectors.EVENT_READ)
            while sel.get_map():
                remaining=deadline-time.monotonic()
                if remaining<=0: timed=True;_kill(proc);break
                for key,_ in sel.select(min(remaining,.05)):
                    chunk=os.read(key.fileobj.fileno(),min(65536,cap+1-len(data)))
                    if not chunk: sel.unregister(key.fileobj);break
                    data.extend(chunk)
                    if len(data)>cap: truncated=True;_kill(proc);break
                if truncated: break
        if not timed and not truncated:
            try: proc.wait(timeout=max(.001,deadline-time.monotonic()))
            except subprocess.TimeoutExpired: timed=True
    finally:
        _kill(proc)  # also terminate descendants which survived the leader
        proc.wait();proc.stdout.close()
    return {'output':bytes(data[:cap]).decode('utf-8','replace'),'exitCode':proc.returncode,
            'timeout':timed,'truncated':truncated,'complete':not timed and not truncated and proc.returncode==0}

# Windows equivalents of _limits(): CPU/process caps map onto Job Object limits,
# plus a generous 8 GiB memory ceiling. RLIMIT_FSIZE has no per-file analogue, so
# the job's total write volume is capped at FILE_SIZE_CAP instead.
_WIN_FSIZE_CAP=FILE_SIZE_CAP
_WIN_PROCESS_CAP=PROCESS_HEADROOM

def _command_nt(argv,cwd,timeout,cap):
    from .. import _win32
    from .._winproc import Pump
    env=_test_env(cwd)
    job=_win32.Job(cpu_seconds=_cpu_seconds(timeout),memory_bytes=8*1024**3,max_processes=_WIN_PROCESS_CAP)
    data=bytearray(); state={'truncated':False}; timed=False; deadline=time.monotonic()+timeout; pump=None
    try:
        proc=_win32.spawn_in_job(argv,job,cwd=cwd,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        try:
            def on_chunk(_,chunk):
                data.extend(chunk)
                if len(data)>cap: state['truncated']=True;job.kill();return False
            def on_tick():
                if job.bytes_written()>_WIN_FSIZE_CAP: job.kill();return False
            pump=Pump([proc.stdout],read_size=65536)
            outcome=pump.run(deadline,on_chunk,on_tick)
            if outcome=='timeout': timed=True;job.kill()
            elif outcome=='eof':
                try: proc.wait(timeout=max(.001,deadline-time.monotonic()))
                except subprocess.TimeoutExpired: timed=True
        finally:
            job.kill()  # also terminate descendants which survived the leader
            proc.wait()
            if pump is not None: pump.join()
            proc.stdout.close()
    finally:
        job.close()
    truncated=state['truncated']
    return {'output':bytes(data[:cap]).decode('utf-8','replace'),'exitCode':proc.returncode,
            'timeout':timed,'truncated':truncated,'complete':not timed and not truncated and proc.returncode==0}

def _observations(results,pi,ni,marker):
    controls=[]
    for i in (pi,ni):
        r=results[i]
        controls.append({'ran':True,'matched':marker in r['output'],'complete':r['complete'],
                         **{k:v for k,v in r.items() if k not in {'output','complete'}}})
    p,n=controls
    status='inconclusive' if not p['complete'] or not n['complete'] else ('passed' if p['matched'] and not n['matched'] else 'failed')
    return p,n,status

def _copy_target(root,copy):
    """copytree(symlinks=True) everywhere: links are copied as links, never followed.

    Windows copytree deliberately treats junctions as directories and copies the
    target's contents, so links are skipped there and recreated verbatim.
    """
    if os.name!='nt':
        shutil.copytree(root,copy,symlinks=True); return
    import _winapi
    links=[]
    def ignore(directory,names):
        skipped=[n for n in names if is_link(Path(directory)/n)]
        links.extend(Path(directory)/n for n in skipped)
        return skipped
    shutil.copytree(root,copy,symlinks=True,ignore=ignore)
    for link in links:
        dest=copy/link.relative_to(root)
        target=os.readlink(link)
        if os.lstat(link).st_reparse_tag==0xA0000003:  # IO_REPARSE_TAG_MOUNT_POINT (junction)
            try: _winapi.CreateJunction(_strip_verbatim(target),str(dest))
            except OSError as exc: raise PolicyDenied('cannot reproduce symlink (junction) in disposable copy') from exc
        else:
            try: os.symlink(target,dest,target_is_directory=link.is_dir())
            except OSError as exc: raise PolicyDenied('cannot reproduce symlink in disposable copy') from exc

def _strip_verbatim(path):
    """readlink returns \\\\?\\C:\\x; CreateJunction would nest that prefix and dangle."""
    if path.startswith('\\\\?\\UNC\\'): return '\\\\'+path[8:]
    if path.startswith('\\\\?\\'): return path[4:]
    return path

def _link_destinations(path):
    """Where a link points. Windows resolve() returns a dangling junction's own
    path unchanged, so the stored target is checked as well."""
    yield path.resolve()
    if os.name=='nt':
        target=Path(_strip_verbatim(os.readlink(path)))
        yield Path(os.path.normpath(target if target.is_absolute() else path.parent/target))

def _walk_links(top):
    """Yield every link below top without descending through any link."""
    for directory,dirs,files in os.walk(top,followlinks=False):
        for name in list(dirs):
            if is_link(Path(directory)/name):
                dirs.remove(name); yield Path(directory)/name
        for name in files:
            if is_link(Path(directory)/name): yield Path(directory)/name

def run_local(plan, *, target, workdir_parent, grant=None):
    """Run argv in a fresh copy. Caller must establish explicit user opt-in."""
    check_plan(plan,grant=grant,safety_level=plan.get('level','static'),now=utcnow())
    if plan.get('kind')!='local-command': raise PolicyDenied('run_local requires local-command')
    timeout,cap=execution_limits(plan)
    pi,ni,marker=control_spec(plan,len(plan['commands']))
    root=Path(target['root'] if isinstance(target,dict) else target).resolve(strict=True)
    parent=Path(workdir_parent).resolve(strict=True)
    if parent==root or root in parent.parents: raise PolicyDenied('workdir_parent cannot be inside target')
    started=utcnow(); scratch=Path(tempfile.mkdtemp(prefix='validation-',dir=parent)); copy=scratch/'target'
    results=[]; notes=[]; cleanup={'done':False,'detail':'cleanup not completed'}
    try:
        _copy_target(root,copy)
        for path in _walk_links(copy):
            try: resolved=list(_link_destinations(path))
            except (OSError,RuntimeError) as exc: raise PolicyDenied('invalid copied symlink') from exc
            if not all(r.is_relative_to(copy) for r in resolved): raise PolicyDenied('copied symlink points outside disposable copy')
        isolated=network_isolation_available()
        notes.append('network isolation: unshare -rn' if isolated else 'network isolation: best-effort')
        notes.append('Disposable copy is not filesystem isolation; only explicitly requested trusted harnesses may run.')
        for argv in plan['commands']:
            try: results.append(_command(argv,copy,timeout,cap,isolated))
            except (OSError,subprocess.SubprocessError):
                results.append({'output':'command could not start','complete':False,'exitCode':None,'timeout':False,'truncated':False})
    finally:
        try: shutil.rmtree(scratch)
        except OSError: pass
        cleanup={'done':not scratch.exists(),'detail':'disposable copy removed' if not scratch.exists() else 'disposable copy cleanup failed'}
    positive,negative,status=_observations(results,pi,ni,marker)
    if not cleanup['done']: status='inconclusive'
    return build_receipt(plan,status=status,positive=positive,negative=negative,started_at=started,cleanup=cleanup,
                         output='\n'.join(r['output'] for r in results),notes=notes)

class _Redirects(urllib.request.HTTPRedirectHandler):
    max_redirections=5
    def __init__(self,origin,notes,charge): self.origin,self.notes,self.charge=origin,notes,charge
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        # Follow same-origin redirects (login flows etc.), charging each hop to
        # the grant before it is sent; never leave the authorized origin.
        try: same=normalize_origin(newurl)==self.origin
        except (PolicyDenied,ValueError): same=False
        if not same:
            self.notes.append('cross-origin redirect refused');return None
        if not self.charge():
            self.notes.append('redirect stopped: request budget used up');return None
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def run_http(plan, *, grant, store=None):
    """Probe only authorized origins; charge every attempted request before I/O.

    The lock serializes in-process grants. Multi-process callers must serialize
    dispatch externally: the store contract has no atomic consume operation.
    """
    with _GRANT_LOCK:
        if store is not None:
            try: grant=store.get_grant(grant['grantId'])
            except (KeyError,TypeError,NotFound) as exc: raise PolicyDenied('missing grant') from exc
        check_plan(plan,grant=grant,safety_level=plan.get('level','static'),now=utcnow())
        if plan.get('kind')!='http-probe': raise PolicyDenied('run_http requires http-probe')
        timeout,cap=execution_limits(plan);pi,ni,marker=control_spec(plan,len(plan['requests']),http=True)
        started=utcnow();results=[];notes=[]
        def charge():
            # Count one request against the grant and persist it before dispatch.
            current=store.get_grant(grant['grantId']) if store is not None else grant
            if current.get('revoked') or current.get('revokedAt') or current.get('used',0)>=current.get('maxRequests',0):
                return False
            grant['used']=current.get('used',0)+1
            if store is not None:
                store.grant_validation(grant['scanId'],grant)
                if store.get_grant(grant['grantId'])['used']!=grant['used']: raise PolicyDenied('grant usage persistence failed')
            return True
        for spec in plan['requests']:
            one=dict(plan,requests=[spec])
            check_plan(one,grant=grant,safety_level=plan['level'],now=utcnow())
            if not charge(): raise PolicyDenied('request count exceeds remaining grant maxRequests')
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),_Redirects(normalize_origin(spec['url']),notes,charge))
            body=spec.get('body')
            if body is not None and not isinstance(body,bytes): body=str(body).encode()
            req=urllib.request.Request(spec['url'],data=body,method=spec.get('method','GET'),headers=spec.get('headers',{}))
            response=None
            try:
                try: response=opener.open(req,timeout=timeout)
                except urllib.error.HTTPError as exc: response=exc
                with response:
                    data=response.read(cap+1)
                    code=response.code
                truncated=len(data)>cap
                results.append({'output':data[:cap].decode('utf-8','replace'),'complete':not truncated and not 300<=code<400,'statusCode':code,'truncated':truncated})
            except (OSError,urllib.error.URLError,ValueError):
                results.append({'output':'HTTP request failed','complete':False,'statusCode':None,'truncated':False})
        positive,negative,status=_observations(results,pi,ni,marker)
        return build_receipt(plan,status=status,positive=positive,negative=negative,started_at=started,
                             cleanup={'done':True,'detail':'response handles closed; only declared synthetic non-destructive probes'},
                             output='\n'.join(r['output'] for r in results),notes=notes)
