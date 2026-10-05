"""claude-multi test package.

Importing it arms the always-on live-gateway connect tripwire
(``tests/_tripwire.py``) for the whole test process: any connect
to 127.0.0.1/::1/localhost on 8317 or 8316 is refused and recorded. The
helper is imported as the top-level ``_tripwire`` (like ``_catalog``) so the
package and the tests share one module and one hit record.
"""

import os as _os
import sys as _sys

# A packaged parent must never redirect source-suite assets.
_os.environ.pop("CLAUDE_MULTI_ASSETS", None)

_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
if not any(
    _os.path.abspath(entry or _os.curdir) == _TESTS_DIR for entry in _sys.path
):
    _sys.path.append(_TESTS_DIR)

import _tripwire  # noqa: E402

_tripwire.install()
