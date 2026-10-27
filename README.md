# sse-claim-audit

Version 0.1.0 audits documented live HTTP push-channel capabilities against
static syntax evidence in local Python source. It runs offline, uses only the
standard library, and never imports or executes audited code.

Install and run with Python 3.9 or newer:

```sh
python3 -m pip install .
sse-claim-audit ./project --json
python3 -m sse_claim_audit ./project --verbose
python3 -m unittest discover
python3 -m build
```

The positional path defaults to the current directory. `--json` emits a JSON
object containing verdict, findings, claims, evidence, test_evidence, and
warnings. `--verbose` lists evidence paths and routes in human output.
`--ignore GLOB` is repeatable and matches paths relative to the root.
`--fail-on HIGH` is the default; `--fail-on MEDIUM` also fails on findings
backed only by test code. `--version` prints the version.

Exit status is 0 for PASS, 2 for a finding at the selected severity threshold,
and 1 for invalid arguments or an inaccessible root. Each claim family
produces at most one finding. Contradicted positive claims are HIGH, or MEDIUM
when evidence exists only in tests. Contradicted negative claims are HIGH.

## Scope and limitations

Only exact, word-bounded phrase templates in the implementation's POSITIVE and
NEGATIVE constants count as claims. Root README.md and Markdown files beneath
any docs path component are inspected. Fenced blocks are excluded; inline code
counts. Matching ignores case, normalizes hyphens and whitespace, expands
negative contractions, and joins lines within each paragraph. Negative spans
are removed before positive matching.

A bare acronym such as SSE, a bare EventSource name, live, real time,
real-time, push, or live updates does not establish a claim. The spelled-out
protocol name is itself a template. Polling, ordinary chunked output,
websocket servers, and webhooks are outside the claim vocabulary.

Evidence requires an imported library response constructor (route A), a
function yielding data-prefixed text together with a matching response media
type in the same module (route B), or an imported client constructor together
with an events method call in the same module (route C). Imports are resolved
per module. Static matching does not prove runtime correctness, connectivity,
reachability, or that two matching expressions actually share a data flow.
Syntax errors are reported as warnings and provide no evidence.

Production evidence excludes tests, fixture, docs, and example directories,
test-named modules, and symlinks. Test evidence is reported separately. Files
named .env are never opened. Comments, docstrings, and unused string literals
do not establish evidence. With no source, positive claims fail and negative
claims pass. Without a recognized claim, the verdict passes regardless of
source evidence.
