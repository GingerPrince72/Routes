"""Durable, transactionally enforced request-attempt ledger for Revision 9.4.

This is deliberately independent of routes.db.  A transaction is committed before
any potentially billable helper call.  Counts represent attempted requests, even
when the subsequent network call fails.  No automatic retry is performed.
"""

from datetime import datetime, timezone, timedelta
from contextlib import closing, contextmanager
from pathlib import Path
import json
import os
import sqlite3


RECOVERY_MINUTES = 30


class GuardBlocked(RuntimeError):
    """An existing attempt requires explicit repeat permission or is still active."""


class RequestBudgetExceeded(RuntimeError):
    """A request was prevented because the attempt's budget would be exceeded."""


def _utc_now():
    return datetime.now(timezone.utc)


def _time_string():
    return _utc_now().isoformat(timespec='seconds')


class CalculationJournal:
    def __init__(self, path=None):
        configured_path = path or os.getenv('ROUTES_REQUEST_JOURNAL_PATH')
        if configured_path:
            self.path = Path(configured_path).expanduser().resolve()
            if not self.path.parent.is_dir():
                raise OSError(f'Request journal directory does not exist: {self.path.parent}')
        else:
            # Outside the repository, separate from the saved outlet database.
            self.path = (Path.home() / '.local' / 'state' / 'txp_routes' /
                         'revision9_4_request_journal.db').resolve()
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Never allow the request journal to be pointed at the outlet database.
        if self.path.name.casefold() in ('routes.db', 'routes.sqlite', 'routes.sqlite3'):
            raise ValueError('Refusing to use a routes database as the request journal.')
        outlet_db_setting = os.getenv('ROUTES_DB_PATH')
        if outlet_db_setting and self.path == Path(outlet_db_setting).expanduser().resolve():
            raise ValueError('Request journal must not share the outlet database path.')
        with self._transaction() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signature TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN
                    ('in_progress','succeeded','failed','interrupted')),
                started_utc TEXT NOT NULL,
                finished_utc TEXT,
                matrix_limit INTEGER NOT NULL,
                leg_limit INTEGER NOT NULL,
                matrix_count INTEGER NOT NULL DEFAULT 0,
                leg_count INTEGER NOT NULL DEFAULT 0,
                result_json TEXT,
                error_note TEXT
            )''')
            connection.execute('''CREATE INDEX IF NOT EXISTS idx_attempt_signature_id
                ON attempts(signature, id DESC)''')

    def _connect(self):
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA busy_timeout=30000')
        return connection

    @contextmanager
    def _transaction(self):
        """Close SQLite handles deterministically; rollback on raised errors."""
        with closing(self._connect()) as connection:
            with connection:
                yield connection

    @staticmethod
    def _row(row):
        return dict(row) if row is not None else None

    def latest(self, signature):
        with self._transaction() as connection:
            row = connection.execute('''SELECT * FROM attempts
                WHERE signature=? ORDER BY id DESC LIMIT 1''', (signature,)).fetchone()
            return self._row(row)

    def last_success(self, signature):
        with self._transaction() as connection:
            row = connection.execute('''SELECT * FROM attempts
                WHERE signature=? AND state='succeeded'
                ORDER BY id DESC LIMIT 1''', (signature,)).fetchone()
        if row is None:
            return None
        record = dict(row)
        try:
            record['payload'] = json.loads(record['result_json'])
        except (ValueError, TypeError):
            # Fail closed: malformed saved content cannot be treated as a route.
            return None
        return record

    def begin(self, signature, budget, allow_repeat=False):
        """Atomically reserve a calculation before any Google API request."""
        if not isinstance(signature, str) or len(signature) != 64:
            raise ValueError('A valid 64-character calculation signature is required.')
        matrix_limit = int(budget['matrix_requests'])
        leg_limit = int(budget['leg_requests'])
        if matrix_limit != 1 or leg_limit < 0:
            raise ValueError('Invalid Google request budget.')
        with self._transaction() as connection:
            connection.execute('BEGIN IMMEDIATE')
            previous = connection.execute('''SELECT state FROM attempts
                WHERE signature=? ORDER BY id DESC LIMIT 1''', (signature,)).fetchone()
            if previous is not None:
                if previous['state'] == 'in_progress':
                    raise GuardBlocked('This exact calculation is still registered as in progress.')
                if not allow_repeat:
                    raise GuardBlocked(
                        'This calculation was attempted previously. Explicit permission is required to repeat it.'
                    )
            cursor = connection.execute('''INSERT INTO attempts
                (signature,state,started_utc,matrix_limit,leg_limit)
                VALUES (?,'in_progress',?,?,?)''',
                (signature, _time_string(), matrix_limit, leg_limit))
            return cursor.lastrowid

    def note_request(self, attempt_id, kind):
        """Persist an attempted HTTP request *before* invoking the Google helper."""
        if kind not in ('matrix', 'leg'):
            raise ValueError('Unknown request kind.')
        with self._transaction() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM attempts WHERE id=?', (attempt_id,)).fetchone()
            if row is None or row['state'] != 'in_progress':
                raise GuardBlocked('Calculation has no active request reservation.')
            limit = row[f'{kind}_limit']
            count = row[f'{kind}_count']
            if count >= limit:
                raise RequestBudgetExceeded(
                    f'Blocked Google {kind} request: {kind} budget of {limit} was reached.'
                )
            connection.execute(f'UPDATE attempts SET {kind}_count={kind}_count+1 WHERE id=?',
                               (attempt_id,))

    def succeed(self, attempt_id, payload):
        """Store reusable route results; a browser refresh need not recalculate."""
        serialized = json.dumps(payload, ensure_ascii=False, default=str,
                                allow_nan=False, separators=(',', ':'))
        with self._transaction() as connection:
            cursor = connection.execute('''UPDATE attempts
                SET state='succeeded', finished_utc=?, result_json=?, error_note=NULL
                WHERE id=? AND state='in_progress' ''',
                (_time_string(), serialized, attempt_id))
            if cursor.rowcount != 1:
                raise GuardBlocked('Attempt was no longer active; route cannot be marked successful.')

    def fail(self, attempt_id, reason):
        """Keep the failure recorded even if Google already processed requests."""
        # Truncate any upstream message. Never record API keys or payloads.
        note = str(reason).replace('\n', ' ')[:300]
        with self._transaction() as connection:
            connection.execute('''UPDATE attempts
                SET state='failed', finished_utc=?, error_note=?
                WHERE id=? AND state='in_progress' ''',
                (_time_string(), note, attempt_id))

    @staticmethod
    def is_stale(row):
        if not row or row.get('state') != 'in_progress':
            return False
        try:
            started = datetime.fromisoformat(row['started_utc'])
            return _utc_now() - started >= timedelta(minutes=RECOVERY_MINUTES)
        except (ValueError, TypeError, KeyError):
            return False

    def interrupt_if_stale(self, attempt_id):
        """Explicit recovery after 30 minutes; never silently release a lock."""
        with self._transaction() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM attempts WHERE id=?', (attempt_id,)).fetchone()
            if not self.is_stale(self._row(row)):
                raise GuardBlocked('Attempt is active or too recent to mark interrupted.')
            cursor = connection.execute('''UPDATE attempts
                SET state='interrupted', finished_utc=?, error_note=?
                WHERE id=? AND state='in_progress' ''',
                (_time_string(), 'Manually marked interrupted after recovery delay', attempt_id))
            if cursor.rowcount != 1:
                raise GuardBlocked('Attempt was changed by another session.')
