"""Path policy: instruction-looking files are untrusted data, never instructions."""
import fnmatch
from pathlib import PurePosixPath

VENDORED = {'node_modules', 'vendor', 'vendors', 'third_party', 'third-party', '.venv', 'venv', '__pycache__'}
INSTRUCTIONS = {'agents.md', 'claude.md', 'security.md', '.cursorrules', '.windsurfrules', 'gemini.md', 'copilot-instructions.md'}


def is_untrusted_instruction_file(path):
    p = PurePosixPath(path)
    return p.name.lower() in INSTRUCTIONS or '.cursor/rules/' in str(p) or '.github/instructions/' in str(p)


def matches_scope(path, scope):
    def match(pattern):
        if fnmatch.fnmatchcase(path, pattern): return True
        # A plain name (no wildcard) is a folder: "tools" or "tools/" covers everything under it.
        if not any(ch in pattern for ch in '*?[') and path.startswith(pattern.rstrip('/') + '/'): return True
        # **/ matches zero directory components as well as one or more.
        if '**/' in pattern:
            before, after = pattern.split('**/', 1)
            return match(before + after)
        return False
    return not scope or any(match(pattern) for pattern in scope)


def exclusion_reason(path, scope):
    parts = PurePosixPath(path).parts
    if '.git' in parts: return 'git_metadata'
    if any(part in VENDORED for part in parts): return 'vendored'
    if not matches_scope(path, scope): return 'out_of_scope'
    return None


def classify(path, data):
    p = PurePosixPath(path); name = p.name.lower(); suffix = p.suffix.lower()
    if b'\x00' in data: return None, 'binary'
    if name in {'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 'poetry.lock', 'uv.lock', 'cargo.lock', 'gemfile.lock', 'composer.lock', 'go.sum'} or suffix == '.lock': return None, 'lock'
    if name.startswith('dockerfile') or name in {'containerfile', 'docker-compose.yml', 'docker-compose.yaml', 'compose.yml', 'compose.yaml'} or suffix in {'.tf', '.tfvars'} or path.startswith('.github/workflows/') or (suffix in {'.yaml', '.yml'} and (b'apiVersion:' in data and b'kind:' in data)):
        return ('hcl' if suffix in {'.tf', '.tfvars'} else 'yaml' if suffix in {'.yaml', '.yml'} else 'dockerfile'), 'iac'
    languages = {'.py':'python', '.js':'javascript', '.jsx':'javascript', '.ts':'typescript', '.tsx':'typescript', '.go':'go', '.rs':'rust', '.java':'java', '.kt':'kotlin', '.swift':'swift', '.c':'c', '.h':'c', '.cpp':'cpp', '.cs':'csharp', '.rb':'ruby', '.php':'php', '.sh':'shell', '.sql':'sql', '.vue':'vue', '.html':'html', '.css':'css'}
    if suffix in languages: return languages[suffix], 'source'
    if suffix in {'.md', '.rst', '.txt'} or name in {'license', 'notice', 'readme'}: return None, 'doc'
    if suffix in {'.json', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.xml', '.conf'} or name.startswith('.env') or name in {'.gitignore', '.cursorrules', 'makefile'}: return None, 'config'
    return None, 'other'
