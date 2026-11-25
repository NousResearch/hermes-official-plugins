"""Negative admission and failed-build hygiene for the companion plugin."""
from pathlib import Path
import subprocess

import pytest

from singularity_plugin import provider, singularity_env as s


@pytest.mark.parametrize('digest', ['', 'not-a-sha256', 'a' * 63, 'a' * 65])
def test_invalid_digest_cannot_select_an_artifact_directory(tmp_path, digest):
    with pytest.raises(ValueError, match='invalid SHA-256 image identity'):
        s._resolved_image_identity('docker://example/image@sha256:' + digest,
                                   str(tmp_path / 'missing.sif'), require_immutable=True)


def test_failed_build_removes_partial_output(monkeypatch, tmp_path):
    cache=tmp_path/'cache'
    monkeypatch.setattr(s,'_get_apptainer_cache_dir',lambda:cache)
    monkeypatch.setattr(s,'_singularity_subprocess_env',lambda **kw:{})

    def fail(cmd,**kw):
        Path(cmd[2]).write_bytes(b'partial-output')
        return subprocess.CompletedProcess(cmd,1,'','synthetic failure')

    monkeypatch.setattr(s.subprocess,'run',fail)
    image='docker://example/image:tag'
    assert s._get_or_build_sif(image)==image
    assert not list(cache.rglob('*.building.sif'))


def test_digest_pinned_cache_reuse_is_independent_of_scope_epoch(monkeypatch,tmp_path):
    cache=tmp_path/'cache'
    monkeypatch.setattr(s,'_get_apptainer_cache_dir',lambda:cache)
    monkeypatch.setattr(s,'_singularity_subprocess_env',lambda **kw:{})
    calls=[]

    def build(cmd,**kw):
        calls.append(cmd)
        Path(cmd[2]).write_bytes(b'complete-sif')
        return subprocess.CompletedProcess(cmd,0,'','')

    monkeypatch.setattr(s.subprocess,'run',build)
    image='docker://example/image@sha256:'+'a'*64
    first=s._get_or_build_sif(image,owner_home=tmp_path,policy_generation='process-one')
    second=s._get_or_build_sif(image,owner_home=tmp_path,policy_generation='process-two')
    assert first==second
    assert len(calls)==1


def test_unpatched_core_is_rejected_before_any_probe_or_container_spawn(monkeypatch):
    from agent import secret_scope
    monkeypatch.delattr(secret_scope,'build_profile_env_boundary')
    monkeypatch.setattr(provider,'find_singularity_executable',lambda:'apptainer')
    monkeypatch.setattr(provider.subprocess,'run',lambda *a,**kw:pytest.fail('unadmitted probe'))
    assert provider.SingularityProvider().check_requirements({}) is False
    with pytest.raises(RuntimeError,match='requires the #94878 core boundary API'):
        provider.SingularityProvider().create_environment(cwd='/',timeout=10)
