"""The batch pass that fills the other three languages of a name, run by an operator.

WHY THIS EXISTS
---------------
``names.translate`` is the module's promise that a missing name slot can be filled without
inventing one, and ``tools/translate_names.py`` is its only production caller - so what this
suite holds is the half a registration test cannot see:

1. **dry run writes nothing.** The default has to be safe to run against the live database on
   the way to deciding, and the map on disk is byte-identical afterwards.
2. **apply fills the missing slots and leaves the rest alone.** The canonical ``users.name`` is
   not written - this is a translation pass, not a rename - and a slot the account already had
   is not replaced by the provider's answer.
3. **the provider is a command, and its failures are not fatal.** A command that answers
   nothing, exits non-zero or does not exist leaves the slot empty, so the account stays in the
   work list and the next run retries exactly what is still missing.
4. **only the languages asked for are sent.** ``--language ar`` must not send the same name to a
   third party in three more languages, and must not drop the language it translated from.

The provider in these tests is a script the suite writes itself - a real subprocess, run the
way the tool runs one, so the env-var contract and the stdin contract are exercised rather than
described.
"""

from __future__ import annotations

import sqlite3
import sys
import textwrap
from pathlib import Path

import pytest

from tools import translate_names

#: Answers ``<target>:<text>``, so both halves of what the provider was told are readable in
#: the stored map - the language it was asked for and the exact name it was handed.
ECHO_PROVIDER = """\
import os, sys
text = sys.stdin.read().strip()
sys.stdout.write(os.environ.get("NAME_TARGET", "") + ":" + text)
"""

#: Has nothing for Hindi, which is the case the retry story is about.
REFUSING_PROVIDER = """\
import os, sys
text = sys.stdin.read().strip()
target = os.environ.get("NAME_TARGET", "")
sys.stdout.write("" if target == "hi" else target + ":" + text)
"""

#: Crashes: a provider that is broken rather than empty.
FAILING_PROVIDER = """\
import sys
sys.exit(3)
"""


@pytest.fixture
def provider(tmp_path):
    """Writes a provider script and answers the command that runs it, the way an operator would."""

    def build(source: str, name: str = "provider.py") -> str:
        script = tmp_path / name
        script.write_text(textwrap.dedent(source), encoding="utf-8")
        return f'"{sys.executable}" "{script}"'

    return build


def database(tmp_path, rows: list[tuple[str, str, str, str | None]]) -> Path:
    """A database with just the columns this tool reads and writes."""
    path = tmp_path / "names.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE users (id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL, "
            "name_i18n TEXT)"
        )
        connection.executemany(
            "INSERT INTO users (id, name, role, name_i18n) VALUES (?, ?, ?, ?)", rows
        )
        connection.commit()
    finally:
        connection.close()
    return path


def stored(path: Path, user_id: str = "1") -> tuple[str, str | None]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            "SELECT name, name_i18n FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    finally:
        connection.close()


def test_a_dry_run_shows_what_would_be_filled_and_writes_nothing(tmp_path, provider, capsys):
    """The default has to be safe to run against the live database, which is the whole point."""
    path = database(tmp_path, [("1", "Ahmed Ali", "worker", '{"en": "Ahmed Ali"}')])
    before = stored(path)

    code = translate_names.main(["--db", str(path), "--provider", provider(ECHO_PROVIDER)])

    assert code == 0
    output = capsys.readouterr().out
    assert stored(path) == before, "a dry run changed the row"
    assert "dry run" in output and "nothing was written" in output
    assert "ar:Ahmed Ali" in output, f"the plan does not say what it would write: {output!r}"


def test_apply_fills_the_missing_slots_and_leaves_the_canonical_name_alone(tmp_path, provider):
    """Three languages added, one untouched, and ``users.name`` not a rename target."""
    path = database(tmp_path, [("1", "Ahmed Ali", "worker", '{"en": "Ahmed Ali"}')])

    code = translate_names.main(
        ["--db", str(path), "--provider", provider(ECHO_PROVIDER), "--apply"]
    )

    assert code == 0
    name, name_i18n = stored(path)
    assert name == "Ahmed Ali", "the batch pass renamed somebody"
    assert name_i18n == '{"ar": "ar:Ahmed Ali", "en": "Ahmed Ali", "hi": "hi:Ahmed Ali", "ur": "ur:Ahmed Ali"}'


def test_an_account_with_no_map_at_all_is_translated_from_the_source_language(tmp_path, provider):
    """Every account created before the column existed is in this state, which is why it matters."""
    path = database(tmp_path, [("7", "محمد أحمد", "worker", None)])

    code = translate_names.main([
        "--db", str(path), "--provider", provider(ECHO_PROVIDER), "--source", "ar", "--apply",
    ])

    assert code == 0
    _, name_i18n = stored(path, "7")
    assert name_i18n == (
        '{"ar": "محمد أحمد", "en": "en:محمد أحمد", "hi": "hi:محمد أحمد", "ur": "ur:محمد أحمد"}'
    ), "the source slot was overwritten by a translation of itself"


def test_a_provider_with_nothing_for_a_language_leaves_the_slot_and_retries_next_run(
    tmp_path, provider
):
    """A slot a provider cannot fill stays missing, so the next run picks it up again."""
    path = database(tmp_path, [("1", "Ahmed Ali", "worker", '{"en": "Ahmed Ali"}')])
    refusing = provider(REFUSING_PROVIDER, "refusing.py")

    assert translate_names.main(["--db", str(path), "--provider", refusing, "--apply"]) == 0
    _, name_i18n = stored(path)
    assert '"hi"' not in name_i18n and '"ar"' in name_i18n, name_i18n

    # The retry: a provider that answers this time fills exactly the slot that was left. The
    # other three keep the first run's answers - the retry is not a second full pass.
    assert translate_names.main([
        "--db", str(path), "--provider", provider(ECHO_PROVIDER), "--language", "hi", "--apply",
    ]) == 0
    _, name_i18n = stored(path)
    assert name_i18n == (
        '{"ar": "ar:Ahmed Ali", "en": "Ahmed Ali", "hi": "hi:Ahmed Ali", "ur": "ur:Ahmed Ali"}'
    )


def test_a_broken_provider_costs_nothing(tmp_path, provider):
    """A crash or a command that does not exist is an empty answer, not a lost map."""
    path = database(tmp_path, [("1", "Ahmed Ali", "worker", '{"en": "Ahmed Ali"}')])
    before = stored(path)

    assert translate_names.main([
        "--db", str(path), "--provider", provider(FAILING_PROVIDER, "broken.py"), "--apply",
    ]) == 0
    assert stored(path) == before

    assert translate_names.main([
        "--db", str(path), "--provider", "definitely-not-a-translator-9f2c", "--apply",
    ]) == 0
    assert stored(path) == before


def test_one_language_is_one_language_sent(tmp_path, provider):
    """--language ar sends the name once, and keeps the slot it translated from."""
    path = database(tmp_path, [("1", "Ahmed Ali", "worker", '{"en": "Ahmed Ali"}')])

    assert translate_names.main([
        "--db", str(path), "--provider", provider(ECHO_PROVIDER), "--language", "ar", "--apply",
    ]) == 0
    _, name_i18n = stored(path)
    assert name_i18n == '{"ar": "ar:Ahmed Ali", "en": "Ahmed Ali"}', name_i18n


def test_a_run_with_no_provider_changes_nothing(tmp_path, monkeypatch, capsys):
    """The refusal is before the database is opened: no provider means no reads either."""
    path = database(tmp_path, [("1", "Ahmed Ali", "worker", '{"en": "Ahmed Ali"}')])
    monkeypatch.delenv("NAME_TRANSLATOR", raising=False)
    before = stored(path)

    assert translate_names.main(["--db", str(path)]) == 2
    assert stored(path) == before
    assert "no provider" in capsys.readouterr().err
