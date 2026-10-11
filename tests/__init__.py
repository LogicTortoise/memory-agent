"""memory-agent test package.

A namespace package would also work, but a regular ``__init__`` keeps
``tests._support`` (test-only helpers) resolvable from any test
under ``tests/unit/``, ``tests/integration/``, ``tests/docs/``,
``tests/e2e/`` etc. without needing a per-directory ``conftest.py``
to add the project root to ``sys.path``.
"""
