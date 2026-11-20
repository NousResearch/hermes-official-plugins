"""Directory enumeration failures must not discard readable sibling projects."""
import errno
import os
from pathlib import Path

import pytest

from hermes_security.errors import TargetError
from hermes_security.orchestration import standard
from hermes_security.orchestration.coverage import build_coverage
from hermes_security.target import build_inventory, resolve_target


@pytest.mark.parametrize('error', [errno.EACCES, errno.EPERM])
def test_unreadable_subdirectory_is_reported_and_readable_siblings_continue(tmp_path, monkeypatch, error):
    blocked = tmp_path / 'middle' / 'postgres-data'
    blocked.mkdir(parents=True)
    (tmp_path / 'before.py').write_text('before = True\n')
    nested = tmp_path / 'z-project' / 'src'
    nested.mkdir(parents=True)
    (nested / 'app.py').write_text('after = True\n')
    real_scandir = os.scandir

    def scandir(path):
        if Path(path) == blocked:
            raise PermissionError(error, 'Permission denied', str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, 'scandir', scandir)
    inventory = build_inventory(resolve_target(str(tmp_path)))

    assert [f['path'] for f in inventory['files']] == ['before.py', 'z-project/src/app.py']
    assert inventory['excluded'] == [{'path': 'middle/postgres-data', 'reason': 'unreadable'}]
    assert not inventory['truncated']


@pytest.mark.parametrize('child,error', [(False, errno.EACCES), (True, errno.EIO)])
def test_root_permission_and_other_io_errors_still_fail_closed(tmp_path, monkeypatch, child, error):
    blocked = tmp_path / 'child' if child else tmp_path
    if child:
        blocked.mkdir()
    real_scandir = os.scandir

    def scandir(path):
        if Path(path) == blocked:
            raise OSError(error, 'Enumeration failed', str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, 'scandir', scandir)
    with pytest.raises(TargetError, match='Unable to enumerate target directory'):
        build_inventory(resolve_target(str(tmp_path)))


def test_unreadable_subtree_keeps_otherwise_closed_coverage_partial():
    excluded = [{'path': 'database/data', 'reason': 'unreadable'}]
    scan = {'scan_id': 'scan_directory_fixture', 'mode': 'deep',
            'options': {'surfaces': [], 'excluded': excluded, 'truncated': False}}
    units = [{'unit': 'file:app.py', 'state': 'reviewed', 'note': 'Fixture review.'}]
    units += [{'unit': 'lane:' + lane['id'], 'state': 'reviewed', 'note': 'Fixture review.'}
              for lane in standard.lanes()]

    coverage = build_coverage(scan, [{'path': 'app.py', 'lines': 1}], units, [], [], [])

    assert coverage['files']['reviewed'] == 1
    assert coverage['files']['excluded'] == excluded
    assert coverage['completeness'] == 'partial'
    assert coverage['gaps'] == ['Excluded database/data: unreadable']
