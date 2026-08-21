from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

import pandas as pd


SCHEMA = """
CREATE TABLE IF NOT EXISTS template_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS products (
    template_version_id INTEGER NOT NULL,
    groupcode TEXT NOT NULL,
    product_id TEXT NOT NULL,
    category TEXT,
    groupname TEXT,
    product_name TEXT,
    note TEXT,
    PRIMARY KEY (template_version_id, groupcode, product_id),
    FOREIGN KEY (template_version_id) REFERENCES template_versions(id)
);
CREATE TABLE IF NOT EXISTS import_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    source_path TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    file_hash TEXT NOT NULL UNIQUE,
    business_date TEXT,
    mode TEXT,
    status TEXT NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sales_dates (
    business_date TEXT PRIMARY KEY,
    import_id INTEGER NOT NULL,
    FOREIGN KEY (import_id) REFERENCES import_logs(id)
);
CREATE TABLE IF NOT EXISTS sales_daily (
    business_date TEXT NOT NULL,
    groupcode TEXT NOT NULL,
    product_id TEXT NOT NULL,
    quantity REAL NOT NULL,
    import_id INTEGER NOT NULL,
    PRIMARY KEY (business_date, groupcode, product_id),
    FOREIGN KEY (import_id) REFERENCES import_logs(id)
);
CREATE TABLE IF NOT EXISTS sales_negative_keys (
    business_date TEXT NOT NULL,
    groupcode TEXT NOT NULL,
    product_id TEXT NOT NULL,
    import_id INTEGER NOT NULL,
    PRIMARY KEY (business_date, groupcode, product_id),
    FOREIGN KEY (import_id) REFERENCES import_logs(id)
);
CREATE TABLE IF NOT EXISTS inventory_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    warehouse TEXT NOT NULL,
    business_date TEXT NOT NULL,
    status TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    codes_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (warehouse, business_date)
);
CREATE TABLE IF NOT EXISTS beijing_inventory (
    snapshot_id INTEGER NOT NULL,
    groupcode TEXT NOT NULL,
    product_id TEXT NOT NULL,
    beijing_available REAL,
    PRIMARY KEY (snapshot_id, groupcode, product_id),
    FOREIGN KEY (snapshot_id) REFERENCES inventory_snapshots(id)
);
CREATE TABLE IF NOT EXISTS xingwang_inventory (
    snapshot_id INTEGER NOT NULL,
    groupcode TEXT NOT NULL,
    product_id TEXT NOT NULL,
    xingwang_available REAL,
    in_transit REAL,
    source_sales90 REAL,
    source_sales30 REAL,
    PRIMARY KEY (snapshot_id, groupcode, product_id),
    FOREIGN KEY (snapshot_id) REFERENCES inventory_snapshots(id)
);
CREATE TABLE IF NOT EXISTS tracking_meta (
    snapshot_date TEXT PRIMARY KEY,
    template_version_id INTEGER NOT NULL,
    beijing_snapshot_id INTEGER,
    xingwang_snapshot_id INTEGER,
    status TEXT NOT NULL,
    calculated_at TEXT NOT NULL,
    threshold_growth REAL NOT NULL,
    threshold_moh30 REAL NOT NULL,
    threshold_moh90 REAL NOT NULL,
    beijing_codes_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tracking_results (
    snapshot_date TEXT NOT NULL,
    groupcode TEXT NOT NULL,
    product_id TEXT NOT NULL,
    category TEXT,
    groupname TEXT,
    product_name TEXT,
    note TEXT,
    sales REAL,
    sales_status TEXT,
    previous_sales REAL,
    growth REAL,
    growth_status TEXT,
    beijing_available REAL,
    xingwang_available REAL,
    in_transit REAL,
    stock_total REAL,
    sales30 REAL,
    sales30_status TEXT,
    sales90 REAL,
    sales90_status TEXT,
    moh30 REAL,
    moh90 REAL,
    quality_labels_json TEXT NOT NULL,
    alert_labels_json TEXT NOT NULL,
    inventory_status TEXT NOT NULL,
    snapshot_status TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, groupcode, product_id)
);
"""


def _text_date(value: date) -> str:
    return value.isoformat()


def _sql_value(value: object) -> object:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self.connection.execute("BEGIN")
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def close(self) -> None:
        self.connection.close()

    def has_import_hash(self, file_hash: str) -> bool:
        row = self.connection.execute("SELECT 1 FROM import_logs WHERE file_hash = ?", (file_hash,)).fetchone()
        return row is not None

    def insert_import_log(
        self,
        *,
        kind: str,
        source_path: str,
        stored_path: str,
        file_hash: str,
        business_date: date | None,
        mode: str,
        status: str,
        report_json: str,
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO import_logs
                (kind, source_path, stored_path, file_hash, business_date, mode, status, report_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                kind,
                source_path,
                stored_path,
                file_hash,
                _text_date(business_date) if business_date else None,
                mode,
                status,
                report_json,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        return int(cursor.lastrowid)

    def insert_template(self, frame: pd.DataFrame, *, source_hash: str, stored_path: str) -> int:
        self.connection.execute("UPDATE template_versions SET is_active = 0")
        cursor = self.connection.execute(
            "INSERT INTO template_versions (created_at, source_hash, stored_path, is_active) VALUES (?, ?, ?, 1)",
            (datetime.now().isoformat(timespec="seconds"), source_hash, stored_path),
        )
        version_id = int(cursor.lastrowid)
        for _, row in frame.iterrows():
            self.connection.execute(
                """
                INSERT INTO products
                    (template_version_id, groupcode, product_id, category, groupname, product_name, note)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    str(row["groupcode"]),
                    str(row["product_id"]),
                    _sql_value(row.get("category")),
                    _sql_value(row.get("groupname")),
                    _sql_value(row.get("product_name")),
                    _sql_value(row.get("note")),
                ),
            )
        return version_id

    def active_template(self) -> tuple[int, pd.DataFrame] | None:
        version = self.connection.execute(
            "SELECT id FROM template_versions WHERE is_active = 1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if version is None:
            return None
        rows = self.connection.execute(
            "SELECT category, groupcode, groupname, product_id, product_name, note FROM products WHERE template_version_id = ?",
            (version["id"],),
        ).fetchall()
        return int(version["id"]), pd.DataFrame.from_records([dict(row) for row in rows])

    def template_by_id(self, version_id: int) -> pd.DataFrame:
        rows = self.connection.execute(
            "SELECT category, groupcode, groupname, product_id, product_name, note FROM products WHERE template_version_id = ?",
            (version_id,),
        ).fetchall()
        return pd.DataFrame.from_records([dict(row) for row in rows])

    def existing_sales_dates(self) -> set[date]:
        rows = self.connection.execute("SELECT business_date FROM sales_dates").fetchall()
        return {date.fromisoformat(row["business_date"]) for row in rows}

    def insert_sales(
        self,
        frame: pd.DataFrame,
        imported_dates: tuple[date, ...],
        *,
        import_id: int,
        replace: bool,
        negative_keys: list[tuple[date, str, str]] | None = None,
    ) -> None:
        dates = tuple(_text_date(value) for value in imported_dates)
        if replace and dates:
            placeholders = ",".join("?" for _ in dates)
            self.connection.execute(f"DELETE FROM sales_daily WHERE business_date IN ({placeholders})", dates)
            self.connection.execute(f"DELETE FROM sales_negative_keys WHERE business_date IN ({placeholders})", dates)
            self.connection.execute(f"DELETE FROM sales_dates WHERE business_date IN ({placeholders})", dates)
        for business_date in imported_dates:
            self.connection.execute(
                "INSERT OR REPLACE INTO sales_dates (business_date, import_id) VALUES (?, ?)",
                (_text_date(business_date), import_id),
            )
        for _, row in frame.iterrows():
            self.connection.execute(
                """
                INSERT OR REPLACE INTO sales_daily
                    (business_date, groupcode, product_id, quantity, import_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    _text_date(row["business_date"]),
                    str(row["groupcode"]),
                    str(row["product_id"]),
                    float(row["quantity"]),
                    import_id,
                ),
            )
        for business_date, groupcode, product_id in negative_keys or []:
            self.connection.execute(
                "INSERT OR REPLACE INTO sales_negative_keys (business_date, groupcode, product_id, import_id) VALUES (?, ?, ?, ?)",
                (_text_date(business_date), groupcode, product_id, import_id),
            )

    def inventory_snapshot_id(self, warehouse: str, business_date: date) -> int | None:
        row = self.connection.execute(
            "SELECT id FROM inventory_snapshots WHERE warehouse = ? AND business_date = ?",
            (warehouse, _text_date(business_date)),
        ).fetchone()
        return int(row["id"]) if row else None

    def upsert_inventory(
        self,
        kind: str,
        business_date: date,
        frame: pd.DataFrame,
        *,
        source_hash: str,
        stored_path: str,
        codes: tuple[str, ...],
        import_id: int,
    ) -> int:
        warehouse = kind
        self.connection.execute(
            """
            INSERT INTO inventory_snapshots
                (warehouse, business_date, status, source_hash, stored_path, codes_json, created_at)
            VALUES (?, ?, 'complete', ?, ?, ?, ?)
            ON CONFLICT(warehouse, business_date) DO UPDATE SET
                status = excluded.status,
                source_hash = excluded.source_hash,
                stored_path = excluded.stored_path,
                codes_json = excluded.codes_json,
                created_at = excluded.created_at
            """,
            (
                warehouse,
                _text_date(business_date),
                source_hash,
                stored_path,
                json.dumps(codes, ensure_ascii=False),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        snapshot_id = self.inventory_snapshot_id(warehouse, business_date)
        assert snapshot_id is not None
        detail_table = "beijing_inventory" if kind == "beijing" else "xingwang_inventory"
        self.connection.execute(f"DELETE FROM {detail_table} WHERE snapshot_id = ?", (snapshot_id,))
        for _, row in frame.iterrows():
            if kind == "beijing":
                self.connection.execute(
                    "INSERT INTO beijing_inventory (snapshot_id, groupcode, product_id, beijing_available) VALUES (?, ?, ?, ?)",
                    (snapshot_id, str(row["groupcode"]), str(row["product_id"]), _sql_value(row.get("beijing_available"))),
                )
            else:
                self.connection.execute(
                    """
                    INSERT INTO xingwang_inventory
                        (snapshot_id, groupcode, product_id, xingwang_available, in_transit, source_sales90, source_sales30)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        str(row["groupcode"]),
                        str(row["product_id"]),
                        _sql_value(row.get("xingwang_available")),
                        _sql_value(row.get("in_transit")),
                        _sql_value(row.get("source_sales90")),
                        _sql_value(row.get("source_sales30")),
                    ),
                )
        return snapshot_id

    def load_sales(self) -> pd.DataFrame:
        rows = self.connection.execute("SELECT business_date, groupcode, product_id, quantity FROM sales_daily").fetchall()
        if not rows:
            return pd.DataFrame(columns=["business_date", "groupcode", "product_id", "quantity"])
        frame = pd.DataFrame.from_records([dict(row) for row in rows])
        frame["business_date"] = frame["business_date"].map(date.fromisoformat)
        return frame

    def load_negative_sales_keys(self) -> set[tuple[str, str]]:
        rows = self.connection.execute("SELECT DISTINCT groupcode, product_id FROM sales_negative_keys").fetchall()
        return {(str(row["groupcode"]), str(row["product_id"])) for row in rows}

    def load_inventory(self, kind: str, business_date: date) -> pd.DataFrame:
        snapshot_id = self.inventory_snapshot_id(kind, business_date)
        if snapshot_id is None:
            if kind == "beijing":
                return pd.DataFrame(columns=["groupcode", "product_id", "beijing_available"])
            return pd.DataFrame(columns=["groupcode", "product_id", "xingwang_available", "in_transit", "source_sales90", "source_sales30"])
        table = "beijing_inventory" if kind == "beijing" else "xingwang_inventory"
        rows = self.connection.execute(f"SELECT * FROM {table} WHERE snapshot_id = ?", (snapshot_id,)).fetchall()
        return pd.DataFrame.from_records([dict(row) for row in rows]).drop(columns=["snapshot_id"], errors="ignore")

    def save_snapshot(
        self,
        snapshot_date: date,
        frame: pd.DataFrame,
        *,
        template_version_id: int,
        beijing_snapshot_id: int | None,
        xingwang_snapshot_id: int | None,
        status: str,
        threshold_growth: float,
        threshold_moh30: float,
        threshold_moh90: float,
        beijing_codes: tuple[str, ...],
    ) -> None:
        snapshot_text = _text_date(snapshot_date)
        self.connection.execute("DELETE FROM tracking_results WHERE snapshot_date = ?", (snapshot_text,))
        self.connection.execute("DELETE FROM tracking_meta WHERE snapshot_date = ?", (snapshot_text,))
        self.connection.execute(
            """
            INSERT INTO tracking_meta
                (snapshot_date, template_version_id, beijing_snapshot_id, xingwang_snapshot_id, status,
                 calculated_at, threshold_growth, threshold_moh30, threshold_moh90, beijing_codes_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_text,
                template_version_id,
                beijing_snapshot_id,
                xingwang_snapshot_id,
                status,
                datetime.now().isoformat(timespec="seconds"),
                threshold_growth,
                threshold_moh30,
                threshold_moh90,
                json.dumps(beijing_codes, ensure_ascii=False),
            ),
        )
        for _, row in frame.iterrows():
            self.connection.execute(
                """
                INSERT INTO tracking_results
                (snapshot_date, groupcode, product_id, category, groupname, product_name, note,
                 sales, sales_status, previous_sales, growth, growth_status, beijing_available,
                 xingwang_available, in_transit, stock_total, sales30, sales30_status, sales90,
                 sales90_status, moh30, moh90, quality_labels_json, alert_labels_json,
                 inventory_status, snapshot_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_text,
                    str(row["groupcode"]),
                    str(row["product_id"]),
                    _sql_value(row.get("category")),
                    _sql_value(row.get("groupname")),
                    _sql_value(row.get("product_name")),
                    _sql_value(row.get("note")),
                    _sql_value(row.get("sales")),
                    _sql_value(row.get("sales_status")),
                    _sql_value(row.get("previous_sales")),
                    _sql_value(row.get("growth")),
                    _sql_value(row.get("growth_status")),
                    _sql_value(row.get("beijing_available")),
                    _sql_value(row.get("xingwang_available")),
                    _sql_value(row.get("in_transit")),
                    _sql_value(row.get("stock_total")),
                    _sql_value(row.get("sales30")),
                    _sql_value(row.get("sales30_status")),
                    _sql_value(row.get("sales90")),
                    _sql_value(row.get("sales90_status")),
                    _sql_value(row.get("moh30")),
                    _sql_value(row.get("moh90")),
                    json.dumps(row.get("quality_labels", []), ensure_ascii=False),
                    json.dumps(row.get("alert_labels", []), ensure_ascii=False),
                    str(row.get("inventory_status", "")),
                    str(row.get("snapshot_status", status)),
                ),
            )

    def load_snapshot(self, snapshot_date: date) -> pd.DataFrame:
        rows = self.connection.execute(
            "SELECT * FROM tracking_results WHERE snapshot_date = ? ORDER BY groupcode, product_id",
            (_text_date(snapshot_date),),
        ).fetchall()
        if not rows:
            return pd.DataFrame()
        frame = pd.DataFrame.from_records([dict(row) for row in rows])
        for column in ("quality_labels_json", "alert_labels_json"):
            target = column.removesuffix("_json")
            frame[target] = frame[column].map(lambda value: json.loads(value or "[]"))
            frame = frame.drop(columns=[column])
        return frame

    def import_logs(self) -> list[dict[str, object]]:
        rows = self.connection.execute(
            "SELECT id, kind, source_path, stored_path, file_hash, business_date, mode, status, created_at FROM import_logs ORDER BY id"
        ).fetchall()
        return [dict(row) for row in rows]

    def snapshot_meta(self, snapshot_date: date) -> dict[str, object] | None:
        row = self.connection.execute(
            "SELECT * FROM tracking_meta WHERE snapshot_date = ?",
            (_text_date(snapshot_date),),
        ).fetchone()
        return dict(row) if row else None

    def history_for_product(self, groupcode: str, product_id: str) -> pd.DataFrame:
        rows = self.connection.execute(
            """
            SELECT snapshot_date, sales, stock_total, in_transit, moh30, moh90
            FROM tracking_results
            WHERE groupcode = ? AND product_id = ?
            ORDER BY snapshot_date
            """,
            (groupcode, product_id),
        ).fetchall()
        return pd.DataFrame.from_records([dict(row) for row in rows])
