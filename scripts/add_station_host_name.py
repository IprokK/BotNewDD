"""Добавить колонку name в station_hosts."""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "quest_platform.db")


def migrate():
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute("PRAGMA table_info(station_hosts)")
        cols = [row[1] for row in cur.fetchall()]
        if "name" not in cols:
            conn.execute("ALTER TABLE station_hosts ADD COLUMN name VARCHAR(100)")
            conn.commit()
            print("Колонка name добавлена в station_hosts.")
        else:
            print("Колонка name уже существует.")
    except Exception as e:
        print(f"Ошибка: {e}")
    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
