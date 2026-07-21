from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app import database  # noqa: E402


def main() -> None:
    url = database.engine.url.render_as_string(hide_password=True)
    print(f"Running schema migration for {url}")
    try:
        database.init_db()
    except SQLAlchemyError as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print("Database schema is up to date.")


if __name__ == "__main__":
    main()
