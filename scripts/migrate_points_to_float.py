"""Миграция: points_awarded и score_total — поддержка дробных (0.5).
Запуск: python scripts/migrate_points_to_float.py
"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "quest_platform.db")


def migrate():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        # station_visits: points_awarded INTEGER -> REAL
        conn.execute(
            """CREATE TABLE station_visits_new (
                id INTEGER PRIMARY KEY,
                event_id INTEGER NOT NULL,
                team_id INTEGER NOT NULL,
                station_id INTEGER NOT NULL,
                state VARCHAR(20) DEFAULT 'arrived',
                started_at DATETIME,
                ended_at DATETIME,
                points_awarded REAL DEFAULT 0,
                host_notes TEXT,
                host_rating INTEGER,
                created_at DATETIME,
                FOREIGN KEY (event_id) REFERENCES events(id),
                FOREIGN KEY (team_id) REFERENCES teams(id),
                FOREIGN KEY (station_id) REFERENCES stations(id)
            )"""
        )
        conn.execute(
            """INSERT INTO station_visits_new
            SELECT id, event_id, team_id, station_id, state, started_at, ended_at,
                   CAST(points_awarded AS REAL), host_notes, host_rating, created_at
            FROM station_visits"""
        )
        conn.execute("DROP TABLE station_visits")
        conn.execute("ALTER TABLE station_visits_new RENAME TO station_visits")

        # teams: score_total INTEGER -> REAL
        conn.execute(
            """CREATE TABLE teams_new (
                id INTEGER PRIMARY KEY,
                event_id INTEGER NOT NULL,
                name VARCHAR(100) NOT NULL,
                status VARCHAR(50) DEFAULT 'active',
                score_total REAL DEFAULT 0,
                current_state VARCHAR(20) DEFAULT 'free_roam',
                current_station_id INTEGER,
                team_progress JSON,
                qr_token VARCHAR(255),
                created_at DATETIME,
                UNIQUE(event_id, name),
                FOREIGN KEY (event_id) REFERENCES events(id),
                FOREIGN KEY (current_station_id) REFERENCES stations(id)
            )"""
        )
        conn.execute(
            """INSERT INTO teams_new
            SELECT id, event_id, name, status, CAST(score_total AS REAL), current_state,
                   current_station_id, team_progress, qr_token, created_at
            FROM teams"""
        )
        conn.execute("DROP TABLE teams")
        conn.execute("ALTER TABLE teams_new RENAME TO teams")

        conn.commit()
        print("Миграция выполнена: points_awarded и score_total теперь REAL.")
    except Exception as e:
        conn.rollback()
        print(f"Ошибка миграции: {e}")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.close()


if __name__ == "__main__":
    migrate()
