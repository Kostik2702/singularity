"""`mb code sql`: one read-only SELECT over the graph's `v1_*` views
(CODE-GRAPH D1, P5).

The views (store.VIEWS) are the contract; the tables under them are not, and
a query may not name them. Four guards, each covering what the others cannot:

* `mode=ro` (store.connect_ro): SQLite will not write the graph's file.
* `PRAGMA query_only`: a statement that would write fails when it runs.
* an allowlist authorizer, asked while the statement is prepared: SELECT;
  READ of a v1 view's columns, and of a table's column only when it is a read
  that view itself makes (the authorizer's 4th argument names the innermost
  view); FUNCTION from an allowlist of SQLite's own pure functions; RECURSIVE.
  Everything else is refused, and the refusal says what it was. It is the only
  guard against ATTACH and VACUUM INTO: on SQLite 3.37.2 both CREATE a file
  from a `mode=ro`, `query_only` connection (measured). Neither touches the
  graph, but a query must not leave files behind either.
* a progress handler that stops the statement once its time budget is spent,
  so a recursive CTE that never ends costs seconds, not the process.

At most `limit` rows come back. One more is fetched to learn whether there
were more, and a cut answer says so (`has_more`, `truncation_reason`), never
silently (UNMET m).

Edges of the guard, bounded rather than closed:
* the 4th argument names the innermost view OR common table expression, so a
  CTE that borrows a view's name (`WITH v1_symbols AS (...)`) runs in that
  view's context. It is allowed exactly the column reads the real view makes
  -- recorded from the engine's own view definitions, never from the file --
  so it reaches at most that view's join keys (internal ids): never `facts`,
  `deps`, `meta` or any column no view reads.
* a read that takes no column (`count(*)`) arrives with no context at all,
  and names a CTE as if it were a table. It is allowed for a name the file
  does not store (a CTE) and for a table of a view the statement went
  through -- so, with a CTE borrowing a view's name, for that view's tables:
  a row count, no value.
* the budget is looked at between VM steps: a single step that builds one
  huge value (printf with a vast width; SQLite caps a value at 1 GB) runs to
  its end first.
* json_each and json_tree are refused: their constructor asks the authorizer
  for an UPDATE of sqlite_master, which a read-only allowlist does not grant.
"""
from __future__ import annotations

import sqlite3
import time
from functools import lru_cache
from typing import Any

from . import store
from .query import page
from .scope import GraphError

DEFAULT_LIMIT = 100
DEFAULT_BUDGET_SECONDS = 5.0
_TICK_OPS = 10_000              # VM steps between two looks at the clock

# SQLite's own functions that compute on their arguments alone: text,
# numbers, dates, JSON, aggregates, windows. Not here, so refused:
# load_extension; fts3_tokenizer (a pointer in, a pointer out); the FTS and
# R*Tree internals; randomblob and zeroblob (up to a gigabyte in one VM step,
# of no use on a graph); and whatever a later SQLite adds, until reviewed.
_FUNCTION_NAMES = """
    abs avg changes char coalesce count cume_dist current_date current_time current_timestamp date datetime
    dense_rank first_value format glob group_concat hex ifnull iif instr julianday lag last_value lead length
    like likelihood likely lower ltrim max min nth_value ntile nullif percent_rank printf quote random rank
    replace round row_number rtrim sign soundex sqlite_source_id sqlite_version strftime string_agg substr
    substring sum time total total_changes trim typeof unicode unixepoch unlikely upper
    acos acosh asin asinh atan atan2 atanh ceil ceiling cos cosh degrees exp floor ln log log10 log2 mod pi
    pow power radians sin sinh sqrt tan tanh trunc
    json json_array json_array_length json_extract json_group_array json_group_object json_insert
    json_object json_patch json_quote json_remove json_replace json_set json_type json_valid -> ->>
"""
_FUNCTIONS = frozenset(_FUNCTION_NAMES.split())

# The authorizer's action codes (sqlite3.h), to say what was refused.
_ACTIONS = {getattr(sqlite3, f"SQLITE_{n}"): n.replace("_", " ") for n in (
    "CREATE_INDEX", "CREATE_TABLE", "CREATE_TEMP_INDEX", "CREATE_TEMP_TABLE", "CREATE_TEMP_TRIGGER",
    "CREATE_TEMP_VIEW", "CREATE_TRIGGER", "CREATE_VIEW", "DELETE", "DROP_INDEX", "DROP_TABLE",
    "DROP_TEMP_INDEX", "DROP_TEMP_TABLE", "DROP_TEMP_TRIGGER", "DROP_TEMP_VIEW", "DROP_TRIGGER", "DROP_VIEW",
    "INSERT", "PRAGMA", "READ", "SELECT", "TRANSACTION", "UPDATE", "ATTACH", "DETACH", "ALTER_TABLE",
    "REINDEX", "ANALYZE", "CREATE_VTABLE", "DROP_VTABLE", "FUNCTION", "SAVEPOINT", "RECURSIVE")}

_WHAT_IS_ALLOWED = "`mb code sql` runs one SELECT over the v1_* views (`mb code schema` lists them)"


class SqlRefused(GraphError):
    """The SQL was not run, or was stopped; the message says why."""


@lru_cache(maxsize=1)
def view_reads() -> dict[str, frozenset[tuple[str, str]]]:
    """{view: {(table, column)}}: every column read each v1 view makes, join
    keys included. Recorded by preparing the engine's own view definitions on
    an empty in-memory graph -- never from the file, whose views could say
    anything -- so it follows the definitions without a second list to keep."""
    con = sqlite3.connect(":memory:")
    reads: set[tuple[str, str, str]] = set()

    def record(action: int, arg1: str | None, arg2: str | None, _db: str | None, inner: str | None) -> int:
        if action == sqlite3.SQLITE_READ and inner and arg2:
            reads.add((inner.lower(), (arg1 or "").lower(), arg2.lower()))
        return sqlite3.SQLITE_OK

    try:
        con.executescript(store.DDL + store.VIEW_DDL)
        con.set_authorizer(record)
        for name in store.VIEWS:
            con.execute(f"SELECT * FROM {name} LIMIT 0").fetchall()
    finally:
        con.close()
    return {name: frozenset((t, c) for v, t, c in reads if v == name) for name in store.VIEWS}


class _Guard:
    """The authorizer. `denied` keeps the first refusal: SQLite itself says
    only "not authorized" (or, for a table-valued function, "vtable
    constructor failed"), which does not tell the user what to change."""

    def __init__(self, stored: set[str]) -> None:
        self.reads = view_reads()
        self.tables = {v: {t for t, _ in cols} for v, cols in self.reads.items()}
        self.stored = stored                # every table and view in the file, lower case
        self.seen: set[str] = set()         # the views this statement went through
        self.denied: str | None = None

    def __call__(self, action: int, arg1: str | None, arg2: str | None, _db: str | None,
                 inner: str | None) -> int:
        inner = (inner or "").lower()
        if inner in self.reads:
            self.seen.add(inner)
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in _FUNCTIONS:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ and self._may_read((arg1 or "").lower(), (arg2 or "").lower(), inner):
            return sqlite3.SQLITE_OK
        if self.denied is None:
            self.denied = _describe(action, arg1, arg2)
        return sqlite3.SQLITE_DENY

    def _may_read(self, table: str, column: str, inner: str) -> bool:
        if table in self.reads:                         # a v1 view's own column
            return True
        if inner in self.reads:                         # a table, read by the view itself
            return (table, column) in self.reads[inner]
        if column:
            return False
        # No column taken (`count(*)`): SQLite gives no context, and names a
        # CTE as if it were a table -- for a recursive one before its body.
        # A name the file does not store is such a CTE, whose own reads are
        # asked for one by one; a stored table only behind a view it went through.
        if table in self.stored or table.startswith("sqlite_"):
            return any(table in self.tables[v] for v in self.seen)
        return True


def _describe(action: int, arg1: str | None, arg2: str | None) -> str:
    if action == sqlite3.SQLITE_READ:
        return f"reading {arg1}.{arg2}" if arg2 else f"reading {arg1}"
    if action == sqlite3.SQLITE_FUNCTION:
        return f"the function {arg2}()"
    if action == sqlite3.SQLITE_UPDATE and arg1 == "sqlite_master":
        return "a table-valued function such as json_each (it asks to update sqlite_master); use json_extract"
    return " ".join(str(a) for a in (_ACTIONS.get(action, f"action {action}"), arg1, arg2) if a)


def _cell(value: Any) -> Any:
    """A BLOB as its SQL literal: JSON has no bytes, and no view holds one."""
    return f"x'{value.hex()}'" if isinstance(value, bytes) else value


def run(con: sqlite3.Connection, text: str, *, limit: int = DEFAULT_LIMIT,
        budget: float = DEFAULT_BUDGET_SECONDS) -> dict[str, Any]:
    """Answer the one SELECT in `text` from the graph `con` reads (opened by
    store.connect_ro): {sql, views_version, columns, rows: a page}.

    `con` is left guarded for good -- Python 3.10's sqlite3 cannot take an
    authorizer off again -- so it must be closed after, not reused. Raises
    SqlRefused: another views version, a refused action, the budget spent,
    or SQLite's own error."""
    if limit < 1:
        raise SqlRefused(f"--limit must be at least 1, not {limit}")
    if budget <= 0:
        raise SqlRefused(f"the time budget must be positive, not {budget:g}")
    if not text.strip():
        raise SqlRefused("no SQL: pass it with --sql-file or on stdin")
    have = store.read_meta(con).get("views_version")
    if have != store.VIEWS_VERSION:
        raise SqlRefused(f"this graph's SQL views are version {have} and this engine reads "
                         f"v{store.VIEWS_VERSION}_*: rebuild the graph with `mb code build`")
    stored = {r[0].lower() for r in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
    con.execute("PRAGMA query_only = 1")
    guard = _Guard(stored)
    con.set_authorizer(guard)
    deadline = time.monotonic() + budget
    spent = False

    def tick() -> int:
        nonlocal spent
        spent = time.monotonic() > deadline
        return int(spent)

    con.set_progress_handler(tick, _TICK_OPS)
    try:
        cur = con.execute(text)
        if cur.description is None:
            raise SqlRefused(f"the SQL returns no rows: {_WHAT_IS_ALLOWED}")
        columns = [d[0] for d in cur.description]
        rows = cur.fetchmany(limit + 1)
    except sqlite3.Warning:                     # a second statement after the first; none of it ran
        raise SqlRefused(f"one statement at a time: {_WHAT_IS_ALLOWED}") from None
    except sqlite3.Error as exc:
        if guard.denied:
            raise SqlRefused(f"refused: {guard.denied}. {_WHAT_IS_ALLOWED}") from None
        if spent:
            raise SqlRefused(f"stopped after its {budget:g} s budget; narrow the query "
                             "or give it more time with --timeout") from None
        raise SqlRefused(f"SQLite: {exc}") from None
    finally:
        con.set_progress_handler(None, 0)
    items = [[_cell(v) for v in row] for row in rows[:limit]]
    result = page(items, len(rows), 0, limit)
    if result["has_more"]:
        # how many more is not known without running the query to its end, and
        # there is no --offset: the SQL pages itself (LIMIT ... OFFSET ...)
        result.update(total=None, next_offset=None)
    return {"sql": text, "views_version": store.VIEWS_VERSION, "columns": columns, "rows": result}
