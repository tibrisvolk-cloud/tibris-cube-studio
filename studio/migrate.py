"""Copy a legacy database using SQLite backup; never mutate the source."""

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .store import Store


def migrate(source, destination, legacy_utc_offset=0):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_file():
        raise ValueError("Исходная база не найдена.")
    if source == destination or destination.exists():
        raise ValueError("Укажите новый, ещё не существующий файл назначения.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation protects existing data even if another process creates the path concurrently.
    with destination.open("xb"):
        pass
    src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    store = Store(destination)
    old_zone = timezone(timedelta(hours=legacy_utc_offset))
    with store.db(True) as c:
        for r in c.execute(
            "SELECT user_id,riddle_id,first_attempt_time FROM user_riddle_attempts"
        ).fetchall():
            value = r["first_attempt_time"]
            if not value:
                continue
            try:
                float(value)
                continue
            except ValueError:
                dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=old_zone)
            c.execute(
                "UPDATE user_riddle_attempts SET first_attempt_time=? WHERE user_id=? AND riddle_id=?",
                (str(dt.timestamp()), r["user_id"], r["riddle_id"]),
            )
    return store


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("source")
    p.add_argument("destination")
    p.add_argument(
        "--legacy-utc-offset",
        type=float,
        default=0,
        help="UTC offset of the OLD server's naive timestamps (e.g. 3 for Moscow; default 0)",
    )
    args = p.parse_args()
    try:
        migrate(args.source, args.destination, args.legacy_utc_offset)
    except (ValueError, sqlite3.Error) as e:
        raise SystemExit(str(e)) from None
    print("База скопирована и обновлена. Исходный файл не изменялся.")


if __name__ == "__main__":
    main()
