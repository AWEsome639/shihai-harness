pypdf-6.19.0-py3-none-any.whl as PyPI publishes it (sha256 7e5d6e730e7dae87d560a2cee218b852f6498c8be61966f3cd02ead971e48d14),
BSD-3-Clause (LICENSE, the wheel's own). grade.py puts the wheel first on sys.path (Python imports a pure-Python wheel as
a zip), so the judge reads PDF deliverables with pypdf 6.19.0, whatever the task image holds.
