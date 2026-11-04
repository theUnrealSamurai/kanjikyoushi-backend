"""Move the users' data from the live database into a freshly built one.

The corpus is rebuilt from a snapshot of the live database (build_corpus --replace), so the words keep their ids and
everything a learner owns still points at the right word. This copies every table that is not corpus into the new
database, so it can replace the live file. The live database is only ever opened read-only.
"""
import sqlite3
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, connection, transaction

from type.models import Learner


# The corpus (rebuilt, not copied), plus Django's own bookkeeping: both databases are migrated, and permission and
# content type ids belong to the database that made them.
SKIPPED = {
    "type_sentence", "type_sentence_words", "type_word", "type_kanaword", "type_passage", "type_passagesentence",
    "type_bookmark", "django_migrations", "django_content_type", "auth_permission",
}
CHUNK = 5000
SEQUENCE = "sqlite_sequence"

# Tables that point at rows of tables that are not copied, and what each id means there. Ids must mean the same thing
# in both databases, or users would silently get other people's permissions.
WORDS = ("SELECT word_id FROM type_userword UNION SELECT word_id FROM type_reviewlog",
         "SELECT id, text, reading FROM type_word")
PERMISSIONS = ("SELECT permission_id FROM auth_user_user_permissions UNION SELECT permission_id FROM auth_group_permissions",
               "SELECT p.id, p.codename, c.app_label, c.model FROM auth_permission p JOIN django_content_type c ON c.id = p.content_type_id")
CONTENT_TYPES = ("SELECT content_type_id FROM django_admin_log WHERE content_type_id IS NOT NULL",
                 "SELECT id, app_label, model FROM django_content_type")


def tables(db):
    """The names of a database's own tables, in alphabetical order."""
    rows = db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
    return [name for (name,) in rows]


def columns(db, table):
    return [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]


def meaning(db, ids_sql, rows_sql):
    """{id: the rest of its row} for the rows of `rows_sql` whose id is in `ids_sql`."""
    return {row[0]: row[1:] for row in db.execute(f"SELECT * FROM ({rows_sql}) WHERE id IN ({ids_sql})")}


class Command(BaseCommand):
    help = ("Copy the users' data (every table except the corpus) from the live database into this one, which was built "
            "from a snapshot of it. Checks that the word ids still mean the same words, and rolls back if not.")

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="source", type=Path, required=True, metavar="PATH",
                            help="the live SQLite database (opened read-only, never written)")
        parser.add_argument("--dry-run", action="store_true", help="do everything and check it, then roll back")

    def handle(self, source, dry_run, **options):
        if connection.vendor != "sqlite":
            raise CommandError("adopt_user_data only works with SQLite")
        if not source.is_file():
            raise CommandError(f"{source} does not exist")
        target = Path(connection.settings_dict["NAME"])
        if target.exists() and target.samefile(source):
            raise CommandError("--from is the database this command writes to; point it at the live database")

        live = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
        try:
            live.execute("BEGIN")  # one consistent read of every table
            problems = self.adopt(live, dry_run)
        finally:
            live.close()
        if problems:
            raise CommandError("Rolled back, nothing was changed:\n  " + "\n  ".join(problems))
        self.stdout.write(self.style.SUCCESS("Checked only, nothing was written (--dry-run)." if dry_run else "Done."))

    def adopt(self, live, dry_run):
        """Copy, check and commit (or roll back, with a list of problems). Returns the problems."""
        problems = []
        with transaction.atomic():
            with connection.cursor() as cursor:
                db = cursor.connection  # the sqlite3 connection, so the copy is not slowed by the cursor wrapper
                mine, theirs = tables(db), tables(live)
                for name in sorted(set(theirs) - set(mine) - SKIPPED):
                    self.stdout.write(self.style.WARNING(f"Not copied: {name} is only in the live database."))
                for name in sorted(set(mine) - set(theirs) - SKIPPED):
                    self.stdout.write(f"Left as it is: {name} is not in the live database.")
                copied = [name for name in mine if name in theirs and name not in SKIPPED]

                for name in copied:
                    problems += self.copy(db, live, name)
                Learner.objects.update(current={}, recent=[])  # sentence ids changed: what was on screen is gone

                if not problems:
                    problems = self.verify(db, live)
                try:
                    connection.check_constraints()  # a row pointing at nothing, e.g. a word the live database never had
                except IntegrityError as error:
                    problems.append(f"foreign key: {error}")
            if problems or dry_run:
                transaction.set_rollback(True)
        return problems

    def copy(self, db, live, table):
        """Replace the table's rows with the live database's, on the columns both have. Returns problems."""
        wanted = [name for name in columns(db, table) if name in columns(live, table)]
        names = ", ".join(f'"{name}"' for name in wanted)
        db.execute(f'DELETE FROM "{table}"')
        rows = live.execute(f'SELECT {names} FROM "{table}"')
        while batch := rows.fetchmany(CHUNK):
            db.executemany(f'INSERT INTO "{table}" ({names}) VALUES ({", ".join("?" * len(wanted))})', batch)

        # AUTOINCREMENT remembers the highest id ever used, even of deleted rows: keep that, so no id is handed out twice.
        sequence = live.execute(f"SELECT seq FROM {SEQUENCE} WHERE name = ?", [table]).fetchone()
        if sequence:
            db.execute(f"DELETE FROM {SEQUENCE} WHERE name = ? AND seq < ?", [table, sequence[0]])
            if not db.execute(f"SELECT 1 FROM {SEQUENCE} WHERE name = ?", [table]).fetchone():
                db.execute(f"INSERT INTO {SEQUENCE} (name, seq) VALUES (?, ?)", [table, sequence[0]])
        return []

    def verify(self, db, live):
        """Problems found in the copy: row counts that differ, and ids that mean something else here than there."""
        problems, theirs = [], tables(live)
        for name in tables(db):
            if name in SKIPPED or name not in theirs:
                continue
            there, here = (conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for conn in (live, db))
            self.stdout.write(f"  {name}: {here} rows" + ("" if here == there else f" (live has {there})"))
            if here != there:
                problems.append(f"{name}: {here} rows copied, {there} in the live database")

        for what, (ids, rows) in (("word", WORDS), ("permission", PERMISSIONS), ("content type", CONTENT_TYPES)):
            try:
                old, new = meaning(live, ids, rows), meaning(db, ids, rows)
            except sqlite3.OperationalError as error:  # a table the old database never had
                problems.append(f"could not check {what} ids: {error}")
                continue
            used = {row[0] for row in live.execute(ids)}
            for id in sorted(used):
                if old.get(id) != new.get(id):
                    problems.append(f"{what} id {id} is {old.get(id)} in the live database but {new.get(id)} here")
        return problems
