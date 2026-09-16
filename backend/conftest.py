"""Root-directory pytest configuration.

``backend/test_load.py`` and ``backend/test_login.py`` are standalone operator scripts:
they open an HTTP connection to a running server on import and call ``sys.exit()``. They
happen to be named ``test_*.py``, so pytest tries to collect them at the root directory
even though ``testpaths = tests`` is configured, and importing one aborts the whole run
with ``INTERNALERROR: caught unexpected SystemExit`` - which looks like a broken test
suite rather than a misplaced script.

``collect_ignore`` is the supported way to say "not a test module, never import it".
"""

collect_ignore = ["test_load.py", "test_login.py", "test.py"]
