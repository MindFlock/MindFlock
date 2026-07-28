"""Hypothesis configuration shared by every property test.

Several property tests do real per-example filesystem I/O (writing and reading
the state file, building prompt files, etc.). Hypothesis's default per-example
deadline (200 ms) turns a slow disk on CI/WSL into a spurious ``DeadlineExceeded``
failure even though the property itself holds. Register and load a profile with
the deadline disabled so timing never fails a property; the number of examples
and the property assertions are untouched.

This conftest is imported before any ``tests/property`` test module, so the
loaded profile becomes the parent of each test's own ``@settings(...)`` — a
decorator that sets ``max_examples`` but not ``deadline`` inherits
``deadline=None`` from here.
"""

from hypothesis import settings

settings.register_profile("mindflock", deadline=None)
settings.load_profile("mindflock")
