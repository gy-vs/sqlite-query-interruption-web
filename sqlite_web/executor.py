import base64
import json
from dataclasses import dataclass, field

from peewee import DatabaseError
from peewee import sqlite3


@dataclass
class Result:
    kind: str  # 'rows', 'affected', 'error', 'stopped'
    statement: str = ''
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    keys: list = None  # Encoded row keys.
    has_next: bool = False
    affected: int = -1
    error: str = ''


class ExecutionStopped(Exception):
    """Raised inside a worker when sqlite reports an interrupt requested by
    this execution. Distinct from ordinary OperationalErrors so a stopped
    query is never presented as a SQL error."""
    pass


# SQLITE_INTERRUPT's extended result code. Older pythons lack the constants,
# so the message check is the fallback.
def is_interrupt_error(exc):
    code = getattr(exc, 'sqlite_errorcode', None)
    if code is not None and (code & 0xff) == 9:
        return True
    name = getattr(exc, 'sqlite_errorname', None)
    if name and 'INTERRUPT' in name:
        return True
    return str(exc).strip().lower() == 'interrupted'


def wrap(sql, ordering=None, limit=None, offset=0, select='*'):
    # The one place user sql gets wrapped in a subselect. The \n before
    # the closing paren terminates any trailing "--..." comment.
    wrapped = 'SELECT %s FROM (\n%s\n) AS _' % (
        select, sql.rstrip('; \t\r\n'))
    if ordering:
        wrapped += ' ORDER BY %d %s' % (abs(ordering),
                                        'DESC' if ordering < 0 else 'ASC')
    if limit is not None:
        wrapped += ' LIMIT %d OFFSET %d' % (limit, offset)
    return wrapped


def run_one(dataset, sql, page=1, page_size=50, ordering=None,
            stop_event=None):
    # The query box allows whatever kinds of query/ies. We wrap the user query
    # to provide ordering + pagination, but cannot wrap DDL or DML statements.
    # Rather than try to parse the user SQL, attempt to wrap + execute (this
    # only works for SELECTs), and on failure fall-back to unwrapped.
    page = max(page, 1)

    def stopped(exc):
        # An interrupt during the probe query is only ours if the user
        # asked to stop; otherwise it is an ordinary failure.
        return stop_event is not None and stop_event.is_set() and \
            is_interrupt_error(exc)

    try:
        # Fetch page_size + 1 rows so a "next" page can be detected.
        cursor = dataset.query(wrap(sql, ordering, page_size + 1,
                                    (page - 1) * page_size))
        paged = True
    except DatabaseError as exc:
        if stopped(exc):
            return Result('stopped', sql)
        try:
            if stop_event is not None and stop_event.is_set():
                # Never start the unwrapped statement after a stop request:
                # the write may have been waiting on a lock that the
                # interrupt released.
                return Result('stopped', sql)
            cursor = dataset.query(sql)
            paged = False
        except Exception as exc:
            if stopped(exc):
                return Result('stopped', sql)
            return Result('error', sql, error=str(exc))
    except Exception as exc:
        if stopped(exc):
            return Result('stopped', sql)
        raise

    if cursor.description is None:
        return Result('affected', sql, affected=cursor.rowcount)

    columns = [d[0] for d in cursor.description]
    try:
        rows = cursor.fetchall()
    except Exception as exc:
        if stopped(exc):
            return Result('stopped', sql)
        raise
    return Result(
        'rows',
        sql,
        columns=columns,
        rows=rows[:page_size] if paged else rows,
        has_next=paged and len(rows) > page_size)


def split_statements(script):
    stmts, buf = [], ''
    for ch in script:
        buf += ch
        if ch == ';' and sqlite3.complete_statement(buf):
            if buf.strip():
                stmts.append(buf.strip())
            buf = ''
    if buf.strip():
        stmts.append(buf.strip())
    return stmts


def run_script(dataset, statements, page_size=50, stop_event=None):
    # Allow running multiple statements from the query box.
    results = []
    for stmt in statements:
        result = run_one(dataset, stmt, page_size=page_size,
                         stop_event=stop_event)
        results.append(result)
        if result.kind == 'error' or result.kind == 'stopped':
            break
    return results


def is_read(dataset, sql, stop_event=None):
    try:
        dataset.query(wrap(sql, limit=0))
        return True
    except DatabaseError as exc:
        if stop_event is not None and stop_event.is_set() and \
                is_interrupt_error(exc):
            raise ExecutionStopped(str(exc))
        return False


def _enc(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {'b64': base64.b64encode(bytes(value)).decode()}
    if value is None or isinstance(value, (int, float, str, bool)):
        return value
    return str(value)  # date, Decimal, etc. fall back to their text form.

def _dec(value):
    if isinstance(value, dict) and 'b64' in value:
        return base64.b64decode(value['b64'])
    return value


def key_encode(values):
    val_json = json.dumps([_enc(v) for v in values])
    return base64.urlsafe_b64encode(val_json.encode()).decode()

def key_decode(token):
    decoded = base64.urlsafe_b64decode(token.encode())
    return [_dec(v) for v in json.loads(decoded)]
