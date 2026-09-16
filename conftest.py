"""Root-directory pytest configuration.

``test_load.py`` is a standalone operator script, not a test module: it talks to
a server the developer started by hand and calls ``exit()`` at import time when
the sample photo is missing. Because its name matches pytest's collection
pattern, a bare ``pytest`` run tries to import it and aborts the whole session
with ``INTERNALERROR: caught unexpected SystemExit`` - which reads like a broken
suite rather than a misplaced script. ``collect_ignore`` is the supported way to
say "never collect this".
"""

collect_ignore = ["test_load.py"]
