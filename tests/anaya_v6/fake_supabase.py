"""A minimal, in-memory fake of the small slice of the Supabase/PostgREST
client surface Phase 4C's identity/enquiry/conversation/profile-sync
services actually call (`.table().select/insert/update().eq().order()
.limit().maybe_single().execute()`). Used ONLY by Phase 4C tests, so the
real matching/merge/upsert logic in those services can be exercised
without ever touching the real shared database — `get_supabase_admin_client`
is monkeypatched to return an instance of this, never a real client.
"""

from __future__ import annotations


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, store: dict, table_name: str):
        self.store = store
        self.table_name = table_name
        self.store.setdefault(table_name, [])
        self._filters: list[tuple[str, object]] = []
        self._in_filter: tuple[str, list] | None = None
        self._or_expr: str | None = None
        self._limit: int | None = None
        self._order: tuple[str, bool] | None = None
        self._single = False
        self._op: str | None = None
        self._payload: dict | None = None

    def select(self, _cols="*"):
        self._op = self._op or "select"
        return self

    def insert(self, payload: dict):
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, payload: dict):
        self._op = "update"
        self._payload = payload
        return self

    def eq(self, col: str, val):
        self._filters.append((col, val))
        return self

    def in_(self, col: str, values: list):
        self._in_filter = (col, list(values))
        return self

    def or_(self, expr: str):
        """Only ever called in this codebase with task_manager.list_due_tasks'
        own exact shape: 'resumable_at.is.null,resumable_at.lte.<iso>' — a
        narrow, faithful-enough interpretation of that one real usage, not a
        general PostgREST or-filter parser."""
        self._or_expr = expr
        return self

    def order(self, col: str, desc: bool = False):
        self._order = (col, desc)
        return self

    def limit(self, n: int):
        self._limit = n
        return self

    def maybe_single(self):
        self._single = True
        return self

    def _matches_or_expr(self, row: dict) -> bool:
        if not self._or_expr:
            return True
        for clause in self._or_expr.split(","):
            col, op, val = clause.split(".", 2)
            if op == "is" and val == "null":
                if row.get(col) is None:
                    return True
            elif op == "lte":
                current = row.get(col)
                if current is not None and current <= val:
                    return True
        return False

    def _matching_rows(self) -> list[dict]:
        rows = [r for r in self.store[self.table_name] if all(r.get(c) == v for c, v in self._filters)]
        if self._in_filter:
            col, values = self._in_filter
            rows = [r for r in rows if r.get(col) in values]
        if self._or_expr:
            rows = [r for r in rows if self._matches_or_expr(r)]
        return rows

    def execute(self) -> _Result:
        if self._op == "insert":
            row = dict(self._payload or {})
            self.store[self.table_name].append(row)
            return _Result([row])
        if self._op == "update":
            matched = self._matching_rows()
            for row in matched:
                row.update(self._payload or {})
            return _Result(matched)
        rows = self._matching_rows()
        if self._order:
            col, desc = self._order
            rows = sorted(rows, key=lambda r: r.get(col) or "", reverse=desc)
        if self._limit is not None:
            rows = rows[: self._limit]
        if self._single:
            return _Result(rows[0] if rows else None)
        return _Result(rows)


class FakeSupabaseClient:
    def __init__(self):
        self.tables: dict[str, list[dict]] = {}

    def table(self, name: str) -> _Query:
        return _Query(self.tables, name)

    def seed(self, table_name: str, row: dict) -> dict:
        self.tables.setdefault(table_name, []).append(row)
        return row
