"""Static documentation and syntax inspection; target files are never executed."""
import argparse
import ast
import fnmatch
import json
from pathlib import Path
import re

from . import __version__

POSITIVE = (
    'server sent events support', 'server sent events endpoint',
    'server sent events endpoints', 'server sent events stream',
    'server sent events streams', 'server sent events connection',
    'server sent events connections', 'server sent events api',
    'server sent events', 'server sent event', 'eventsource support',
    'sse support', 'sse endpoints', 'sse endpoint', 'sse streams',
    'sse stream', 'sse streaming', 'supports sse', 'support for sse',
    'uses sse', 'using sse', 'over sse', 'via sse', 'sse based', 'sse powered',
)
NEGATIVE = (
    'no server sent events support', 'does not support server sent events',
    'does not use server sent events', 'does not support sse',
    'does not use sse', 'server sent events not supported', 'sse not supported',
    'without server sent events', 'without sse', 'no server sent events',
    'no sse support', 'no sse',
)


def pattern(phrases):
    return re.compile(r'\b(?:' + '|'.join(re.escape(p) for p in
                      sorted(phrases, key=len, reverse=True)) + r')\b')


def normalize(text):
    return re.sub(r'\s+', ' ', text.lower().replace("n't", ' not').replace('-', ' '))


def claims(text):
    paragraphs = []
    lines = []
    fence = None
    for line in text.splitlines():
        marker = re.match(r'^\s{0,3}(`{3,}|~{3,})', line)
        if fence:
            if re.match(r'^\s{0,3}' + re.escape(fence[0]) + '{' + str(len(fence)) + r',}\s*$', line):
                fence = None
            continue
        if marker:
            fence = marker.group(1)
            paragraphs.append(' '.join(lines))
            lines = []
        elif not line.strip():
            paragraphs.append(' '.join(lines))
            lines = []
        else:
            lines.append(line)
    paragraphs.append(' '.join(lines))
    positive = negative = False
    for paragraph in paragraphs:
        text = normalize(paragraph)
        negative |= bool(pattern(NEGATIVE).search(text))
        text = pattern(NEGATIVE).sub(' ', text)
        positive |= bool(pattern(POSITIVE).search(text))
    return positive, negative


def evidence(source):
    tree = ast.parse(source)
    imports = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports[alias.asname or alias.name.split('.')[0]] = alias.name if alias.asname else alias.name.split('.')[0]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                imports[alias.asname or alias.name] = node.module + '.' + alias.name

    def resolved(node):
        if isinstance(node, ast.Name):
            return imports.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return resolved(node.value) + '.' + node.attr
        return ''

    def imported_call(node, modules, names):
        name = resolved(node.func)
        base = node.func
        while isinstance(base, ast.Attribute):
            base = base.value
        return (isinstance(base, ast.Name) and base.id in imports and
                name.split('.')[0] in modules and name.split('.')[-1] in names)

    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    routes = []
    if any(imported_call(n, {'sse_starlette', 'sse', 'fastapi_sse'},
                         {'EventSourceResponse', 'SseResponse'}) for n in calls):
        routes.append('A')
    framed = False
    for function in ast.walk(tree):
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(function):
                if isinstance(node, (ast.Yield, ast.YieldFrom)):
                    value = node.value
                    if isinstance(value, ast.JoinedStr):
                        value = value.values[0] if value.values else None
                    if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value.startswith('data:'):
                        framed = True
    response = any(resolved(n.func).split('.')[-1] in {'Response', 'StreamingResponse'} and
                   any(k.arg in {'mimetype', 'content_type', 'media_type'} and
                       isinstance(k.value, ast.Constant) and k.value.value == 'text/event-stream'
                       for k in n.keywords) for n in calls)
    if framed and response:
        routes.append('B')
    if (any(imported_call(n, {'sseclient', 'sseclient_py'}, {'SSEClient'}) for n in calls) and
            any(isinstance(n.func, ast.Attribute) and n.func.attr == 'events' for n in calls)):
        routes.append('C')
    return routes


def audit(root, ignores=(), fail_on='HIGH'):
    root = Path(root)
    if not root.is_dir():
        raise ValueError('root must be a readable directory')
    # Force a root read: recursive globbing can suppress access errors.
    next(root.iterdir(), None)
    positive = negative = False
    production = []
    test_evidence = []
    warnings = []
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        parts = relative.parts
        if path.is_symlink() or any((root.joinpath(*parts[:i])).is_symlink() for i in range(1, len(parts))):
            continue
        name = relative.as_posix()
        if any(fnmatch.fnmatchcase(name, glob) for glob in ignores):
            continue
        if not path.is_file():
            continue
        is_doc = name == 'README.md' or ('docs' in parts[:-1] and path.suffix.lower() == '.md')
        if is_doc:
            p, n = claims(path.read_text(encoding='utf-8'))
            positive |= p
            negative |= n
        if path.suffix != '.py':
            continue
        if any(p in {'fixtures', 'docs', 'examples'} for p in parts[:-1]):
            continue
        testing = 'tests' in parts[:-1] or path.name.startswith('test_') or path.name.endswith('_test.py')
        try:
            routes = evidence(path.read_text(encoding='utf-8'))
        except (SyntaxError, UnicodeError) as exc:
            warnings.append(name + ': ' + str(exc))
            continue
        for route in routes:
            (test_evidence if testing else production).append({'path': name, 'route': route})
    findings = []
    if positive and not production:
        findings.append({'rule': 'documented-sse-claim-false',
                         'severity': 'MEDIUM' if test_evidence else 'HIGH',
                         'message': 'Documented live HTTP push-channel capability lacks production evidence.'})
    if negative and production:
        findings.append({'rule': 'documented-no-sse-claim-false', 'severity': 'HIGH',
                         'message': 'Documented absence conflicts with production push-channel evidence.'})
    failed = any(f['severity'] == 'HIGH' or fail_on == 'MEDIUM' for f in findings)
    return {'verdict': 'FAIL' if failed else 'PASS', 'findings': findings,
            'claims': {'positive': positive, 'negative': negative},
            'evidence': production, 'test_evidence': test_evidence, 'warnings': warnings}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage()
        self.exit(1, 'error: ' + message + '\n')


def main(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument('root', nargs='?', default='.')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('--ignore', action='append', default=[])
    parser.add_argument('--fail-on', choices=['HIGH', 'MEDIUM'], default='HIGH')
    parser.add_argument('--version', action='version', version=__version__)
    args = parser.parse_args(argv)
    try:
        result = audit(args.root, args.ignore, args.fail_on)
    except (OSError, ValueError) as exc:
        parser.exit(1, 'error: ' + str(exc) + '\n')
    if args.json:
        print(json.dumps(result))
    else:
        print(result['verdict'])
        for finding in result['findings']:
            print('{severity} {rule}: {message}'.format(**finding))
        if args.verbose:
            for item in result['evidence'] + result['test_evidence']:
                print('Evidence {path}: route {route}'.format(**item))
            for warning in result['warnings']:
                print('Warning: ' + warning)
    return 2 if result['verdict'] == 'FAIL' else 0
