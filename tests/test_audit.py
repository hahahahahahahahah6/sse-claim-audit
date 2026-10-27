import contextlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sse_claim_audit.audit import audit, claims, evidence, main, normalize, pattern, POSITIVE, NEGATIVE

ROOT = Path(__file__).resolve().parents[1]
A = 'from sse_starlette.sse import EventSourceResponse\nresponse = EventSourceResponse(gen)\n'
B = 'def gen():\n    yield f"data: {payload}\\n\\n"\nResponse(gen, mimetype="text/event-stream")\n'
C = 'from sseclient import SSEClient\nclient = SSEClient(url)\nfor e in client.events():\n    print(e)\n'


class AuditTests(unittest.TestCase):
    def fixture(self, doc='sse support', source=None, path='app.py', extra=None, fail_on='HIGH'):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'README.md').write_text(doc)
            if source is not None:
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(source)
            for name, content in (extra or {}).items():
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            result = audit(root, fail_on=fail_on)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = main([str(root), '--json', '--fail-on', fail_on])
            self.assertEqual(json.loads(output.getvalue()), result)
            self.assertEqual(status, 2 if result['verdict'] == 'FAIL' else 0)
            return result

    def good(self, doc, source):
        result = self.fixture(doc, source)
        self.assertEqual(result['verdict'], 'PASS')
        self.assertEqual(result['findings'], [])

    def bad(self, source, doc='sse support', rule='documented-sse-claim-false'):
        result = self.fixture(doc, source)
        self.assertEqual(result['verdict'], 'FAIL')
        self.assertEqual(len(result['findings']), 1)
        self.assertEqual(result['findings'][0]['severity'], 'HIGH')
        self.assertEqual(result['findings'][0]['rule'], rule)

    def test_01_route_a(self):
        self.good('server sent events', A)

    def test_02_route_b_flask(self):
        self.good('sse support', B)

    def test_03_route_b_fastapi(self):
        self.good('sse endpoint', 'def gen():\n    yield "data: hello\\n\\n"\nStreamingResponse(gen, media_type="text/event-stream")')

    def test_04_route_c(self):
        self.good('uses sse', C)

    def test_05_handler(self):
        self.bad('def sse_handler():\n    print("hi")\n    return []')

    def test_06_bare_imports(self):
        for module in ('sse_starlette', 'sse', 'sseclient', 'fastapi_sse'):
            with self.subTest(module=module):
                self.bad('import ' + module, 'supports sse')

    def test_07_plain_chunks(self):
        self.bad('def gen():\n    yield "{}"\nResponse(gen, mimetype="text/event-stream")', 'sse stream')

    def test_08_polling(self):
        self.bad('import requests, time\nwhile True:\n    requests.get(url)\n    time.sleep(1)', 'server sent events')

    def test_09_websocket(self):
        self.bad('import websockets\nwebsockets.serve(handler, "localhost", 8080)')

    def test_10_header(self):
        self.bad('Response("hello", mimetype="text/event-stream")', 'sse endpoint')

    def test_11_framing(self):
        self.bad('def gen():\n    yield "data: hello\\n\\n"')

    def test_12_negative(self):
        self.bad(A, 'no sse support', 'documented-no-sse-claim-false')

    def test_13_no_claim(self):
        self.good('A useful local program.', A)

    def test_14_bare_noun(self):
        self.good('the sse chapter explains the protocol', 'import sse')

    def test_15_polling_claim(self):
        self.good('polls every second for updates', 'requests.get(url)')

    def test_16_websocket_claim(self):
        self.good('websocket server', 'websockets.serve(handler)')

    def test_17_no_source(self):
        self.bad(None, 'supports sse')
        self.good('no sse support', None)

    def test_18_test_only(self):
        for name in ('tests/channel.py', 'test_channel.py', 'channel_test.py'):
            for threshold, verdict in [('HIGH', 'PASS'), ('MEDIUM', 'FAIL')]:
                with self.subTest(name=name, threshold=threshold):
                    result = self.fixture(source=A, path=name, fail_on=threshold)
                    self.assertEqual(result['verdict'], verdict)
                    self.assertEqual(len(result['findings']), 1)
                    self.assertEqual(result['findings'][0]['severity'], 'MEDIUM')

    def test_19_fences_inline(self):
        self.good('```text\nsse support\n```', None)
        self.good('~~~text\nsse support\n~~~', None)
        self.bad(None, '`sse support`')

    def test_20_negation(self):
        self.assertEqual(claims('does not support sse'), (False, True))
        self.good('does not support sse', None)
        self.bad(A, 'does not support sse', 'documented-no-sse-claim-false')

    def test_21_local_class(self):
        self.bad('class EventSourceResponse:\n    pass\nEventSourceResponse(gen)', 'supports sse')

    def test_22_cli(self):
        version = subprocess.run([sys.executable, '-m', 'sse_claim_audit', '--version'], capture_output=True, text=True)
        self.assertEqual((version.returncode, version.stdout.strip()), (0, '0.1.0'))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'README.md').write_text('Local tool.')
            (root / 'app.py').write_text(A)
            (root / 'docs').mkdir()
            (root / 'docs' / 'guide.md').write_text('no sse support')
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main([directory, '--ignore', 'docs/*', '--verbose']), 0)
            self.assertIn('app.py: route A', out.getvalue())
            self.assertEqual(audit(root)['verdict'], 'FAIL')
        for arguments in (['/nonexistent-claim-audit-root'], ['--fail-on', 'LOW'], ['--unknown']):
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(arguments)
            self.assertEqual(error.exception.code, 1)

    def test_33_unreadable_root(self):
        with patch.object(Path, 'iterdir', side_effect=PermissionError('access denied')):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main([str(ROOT)])
            self.assertEqual(error.exception.code, 1)

    def test_23_self_audit(self):
        result = audit(ROOT)
        self.assertEqual(result['verdict'], 'PASS')
        self.assertEqual(result['findings'], [])
        self.assertEqual(result['claims'], {'positive': False, 'negative': False})
        for path in ROOT.rglob('*.md'):
            if path == ROOT / 'README.md' or 'docs' in path.relative_to(ROOT).parts[:-1]:
                text = normalize(path.read_text())
                self.assertIsNone(pattern(POSITIVE + NEGATIVE).search(text), str(path))

    def test_24_env_guard(self):
        original = Path.open
        def guarded(path, *args, **kwargs):
            self.assertNotEqual(path.name, '.env', 'environment file was opened')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', guarded):
            self.assertEqual(audit(ROOT)['verdict'], 'PASS')
        self.assertTrue((ROOT / 'tests/fixtures/.env').is_file())

    def test_25_packaging_and_interpreter(self):
        for directory in (ROOT / 'sse_claim_audit', ROOT / 'tests'):
            for path in directory.rglob('*.py'):
                self.assertIsNone(re.search(r'\b' + 'py' + r'thon\b', path.read_text()), str(path))
        self.assertIsNone(re.search(r'\b' + 'py' + r'thon\b', (ROOT / 'README.md').read_text()))
        project = (ROOT / 'pyproject.toml').read_text()
        self.assertIn('setuptools>=77', project)
        self.assertIn('license = "MIT"', project)
        self.assertIn('requires-' + 'py' + 'thon = ">=3.9"', project)
        self.assertIn('tests', (ROOT / 'MANIFEST.in').read_text())

    def test_26_aliases(self):
        for source in ('from sse_starlette.sse import EventSourceResponse as ESR\nESR(gen)',
                       'import sse_starlette.sse as lib\nlib.EventSourceResponse(gen)',
                       'import sse_starlette.sse\nsse_starlette.sse.EventSourceResponse(gen)',
                       'from fastapi_sse import SseResponse as R\nR(gen)'):
            self.good('supports sse', source)
        self.good('uses sse', 'import sseclient_py as lib\nc = lib.SSEClient(url)\nc.events()')
        self.good('sse support', 'from flask import Response as R\n' + B.replace('Response(', 'R('))

    def test_27_incomplete_client_and_unused_import(self):
        self.bad('from sseclient import SSEClient\nc = SSEClient(url)')
        self.bad('from sse_starlette.sse import EventSourceResponse')
        self.bad('client.events()')
        self.bad('"data: hi"\nResponse(gen, mimetype="text/event-stream")')
        self.bad('from other import EventSourceResponse\nEventSourceResponse(gen)')
        self.bad('@app.get("/events")\ndef route():\n    return []')

    def test_28_exclusions(self):
        for name in ('fixtures/channel.py', 'docs/channel.py', 'examples/channel.py'):
            result = self.fixture(source=A, path=name)
            self.assertEqual(result['evidence'], [])
            self.assertEqual(result['findings'][0]['severity'], 'HIGH')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'README.md').write_text('sse support')
            (root / 'real.txt').write_text(A)
            (root / 'app.py').symlink_to(root / 'real.txt')
            self.assertEqual(audit(root)['evidence'], [])

    def test_29_paragraphs_boundaries(self):
        self.assertEqual(claims('SUPPORTS\nSSE'), (True, False))
        self.assertEqual(claims('supports\n\nSSE'), (False, False))
        self.assertEqual(claims('unsupportedssse supportx'), (False, False))
        self.assertEqual(claims("doesn't support SSE"), (False, True))
        for phrase in POSITIVE:
            self.assertEqual(claims(phrase), (True, False), phrase)
        for phrase in NEGATIVE:
            self.assertEqual(claims(phrase), (False, True), phrase)

    def test_30_same_module_and_two_families(self):
        result = self.fixture(source='def gen():\n    yield "data: hi"', extra={'other.py': 'Response(gen, media_type="text/event-stream")'})
        self.assertEqual(result['verdict'], 'FAIL')
        result = self.fixture('supports sse. Also without sse.', A)
        self.assertEqual(len(result['findings']), 1)
        self.assertEqual(result['findings'][0]['rule'], 'documented-no-sse-claim-false')

    def test_31_source_never_executes(self):
        self.good('supports sse', 'raise RuntimeError("must not execute")\n' + A)
        self.bad('# EventSourceResponse(gen)\n"""data: fake"""')

    def test_32_async_and_yieldfrom(self):
        self.assertEqual(evidence('async def gen():\n    yield "data: hi"\nResponse(gen, content_type="text/event-stream")'), ['B'])
        self.assertEqual(evidence('def gen():\n    yield from "data: hi"\nResponse(gen, content_type="text/event-stream")'), ['B'])


if __name__ == '__main__':
    unittest.main()
