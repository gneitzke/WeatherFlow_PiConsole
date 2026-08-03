""" Merge-safety guard.

The fork's whole strategy is to keep taking upstream fixes cleanly. The
CurrentConditions class in main.py is the hot spot: HeadlessConditions and
AlmanacConditions both subclass it, and its __init__ is the method upstream is
most likely to touch. We deliberately restored it byte-for-byte to upstream. This
test fails loudly (with a diff) the moment someone re-diverges it, so a merge
conflict is caught in CI instead of on a git pull months later.

The class is located by AST (robust to it moving within the file), and compared
against the same class in an upstream ref. The ref is resolved from
PICONSOLE_UPSTREAM_REF, then upstream/main, then origin/main; if none exist the
test skips (with a reason) rather than failing — a fresh clone without the
upstream remote should not red the suite.
"""

import ast
import difflib
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _class_source(text, name='CurrentConditions'):
    tree = ast.parse(text)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            lines = text.splitlines()
            return '\n'.join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f'{name} class not found')


def _resolve_upstream_ref():
    candidates = [os.environ.get('PICONSOLE_UPSTREAM_REF'), 'upstream/main', 'origin/main']
    for ref in filter(None, candidates):
        ok = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', '--verify', ref],
                            capture_output=True).returncode == 0
        if ok:
            return ref
    return None


@pytest.mark.guard
def test_currentconditions_identical_to_upstream():
    ref = _resolve_upstream_ref()
    if ref is None:
        pytest.skip('no upstream ref — set PICONSOLE_UPSTREAM_REF or fetch peted-davis/main')

    local = _class_source((ROOT / 'main.py').read_text())
    upstream_text = subprocess.check_output(
        ['git', '-C', str(ROOT), 'show', f'{ref}:main.py'], text=True)
    upstream = _class_source(upstream_text)

    if local != upstream:
        diff = '\n'.join(difflib.unified_diff(
            upstream.splitlines(), local.splitlines(),
            fromfile=f'{ref}:CurrentConditions', tofile='local:CurrentConditions', lineterm=''))
        raise AssertionError(
            'CurrentConditions has diverged from upstream — this reintroduces the '
            'merge-conflict risk the headless refactor removed. Move the change into a '
            'subclass (see panels/headless.py) instead.\n\n' + diff)


@pytest.mark.guard
def test_headless_override_keeps_button_list_empty():
    # The override MUST assign button_list = [] (not a bare pass): the evt_strike
    # auto-switch in observation_parser iterates it unguarded.
    src = (ROOT / 'panels' / 'headless.py').read_text()
    tree = ast.parse(src)
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == 'HeadlessConditions')
    add_panels = next(n for n in cls.body
                      if isinstance(n, ast.FunctionDef) and n.name == 'add_panels')
    assigns_empty_list = any(
        isinstance(node, ast.Assign)
        and isinstance(node.value, ast.List) and not node.value.elts
        and any(isinstance(t, ast.Attribute) and t.attr == 'button_list' for t in node.targets)
        for node in ast.walk(add_panels))
    assert assigns_empty_list, 'add_panels must set self.button_list = [] (see the lightning path)'
