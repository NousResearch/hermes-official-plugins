"""Planner 4: what a source file imports and what it does, read from its text."""
from hermes_security.target import links


def ext(path, text):
    return links.extract(path, text.encode())


def test_python_imports_keep_relative_levels_and_imported_names():
    found = ext('src/pkg/a.py', 'import os.path\nfrom . import b\nfrom ..core.util import (helper,  # why\n'
                                '    other as o)\nfrom pkg.web import routes  # noqa\n')
    assert set(found['imports']) == {'os.path', '.', '.b', '..core.util', '..core.util.helper', '..core.util.other',
                                     'pkg.web', 'pkg.web.routes'}


def test_javascript_imports_requires_and_reexports():
    found = ext('web/app.ts', "import x from './a'\nimport { y } from \"../lib/b.js\"\nconst z = require('./c')\n"
                              "export * from './d'\nconst m = await import('./e')\nimport './side'\n")
    assert set(found['imports']) == {'./a', '../lib/b.js', './c', './d', './e', './side'}


def test_ruby_requires_and_go_imports():
    ruby = ext('lib/zip.rb', "require 'zip/entry'\nrequire_relative 'zip/file'\nautoload :Foo, 'zip/foo'\n")
    assert set(ruby['imports']) == {'zip/entry', './zip/file', 'zip/foo'}
    go = ext('cmd/main.go', 'package main\n\nimport (\n\t"fmt"\n\tsrv "example.com/app/internal/server"\n)\n\n'
                            'import "example.com/app/auth"\n')
    assert set(go['imports']) == {'fmt', 'example.com/app/internal/server', 'example.com/app/auth'}


def test_facets_count_input_apis_checks_and_file_paths():
    found = ext('jose/jws.py', 'class JWS:\n    def deserialize_json(self, obj, key):\n        self._verify(obj)\n\n'
                               '    def _verify(self, obj):\n        pass\n\n\n'
                               'def load_file(name):\n    return open(os.path.join(ROOT, name)).read()\n')
    assert found['facets'] == {'api': 2, 'checks': 1, 'parse': 1, 'path': 2}
    assert ext('x.py', 'import zipfile\nzipfile.ZipFile(f).extractall(d)\n')['facets']['archive'] >= 1


def test_files_in_other_languages_have_no_links():
    assert links.extract('README.md', b'# import os') == {'imports': [], 'facets': {}}


def test_go_files_record_what_they_use_from_each_package_and_what_they_define():
    found = ext('cmd/main.go', 'package main\n\nimport (\n\t"fmt"\n\tsrv "example.com/app/internal/server"\n'
                               '\t_ "example.com/app/driver"\n\t"example.com/app/auth"\n)\n\n'
                               'func main() {\n\tfmt.Println(auth.Verify(t), srv.New())\n}\n')
    assert found['imports'] == ['fmt', 'example.com/app/internal/server', 'example.com/app/auth']
    assert found['uses'] == {'fmt': ['Println'], 'example.com/app/internal/server': ['New'],
                             'example.com/app/auth': ['Verify']}
    lib = ext('auth/auth.go', 'package auth\n\ntype Claims struct{}\n\nconst (\n\tMaxAge = 10\n\tminAge = 1\n)\n\n'
                              'func Verify(t string) bool { return true }\n\nfunc (c *Claims) Valid() bool { return true }\n')
    assert lib['defines'] == ['Claims', 'MaxAge', 'Verify']
