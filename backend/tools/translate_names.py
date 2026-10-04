#!/usr/bin/env python
"""Fill the missing languages of every stored name, through a translator you choose.

WHY THIS EXISTS
---------------
A name is stored once per language (``users.name_i18n``, see ``names.py``) and the applicant
types it in the language they are reading. The other three slots are then *empty*, and an
administrator can fill them one box at a time on the Credentials tab - which is the right
tool for one correction and the wrong one for a roster of two hundred. This is the batch
half: it walks the accounts, asks the provider for each missing language, and writes back
the map.

WHY THE PROVIDER IS A COMMAND AND NOT A CLIENT
----------------------------------------------
``names.translate`` takes a ``translate_one`` callable, and this tool is its only production
caller - so this is the only place a provider is configured. That provider is a *command*,
given with ``--provider`` (or ``NAME_TRANSLATOR`` in the environment), rather than an SDK
this repository depends on:

* which service a deployment uses is the deployment's decision, and every one of them has a
  different client library, key and quota;
* a name is personal data, and an operator pointing this at a provider is making a decision
  about where workers' names are sent - which is worth being explicit about, on a command
  line, rather than hidden in a dependency;
* the command can be anything: a CLI the operator already has installed, a ``curl`` to an
  internal service, or a script of their own.

The command is run once per name and language. It receives the name on stdin, and the
languages in ``NAME_TEXT``, ``NAME_SOURCE`` and ``NAME_TARGET`` (the text is in both places
because some tools want an argument and some want stdin). It answers the translation on
stdout; an empty answer, a non-zero exit or a timeout leaves that slot empty - the same
"never invent a name" rule the module states, applied to a provider that is down.

Usage
-----
    # what would be filled, and through which command (no writes)
    python backend/tools/translate_names.py --provider 'trans -brief -s en -t "$NAME_TARGET"'

    # do it, for one account
    python backend/tools/translate_names.py --provider '...' --id 601 --apply

    # only the Arabic slot, across the roster
    python backend/tools/translate_names.py --provider '...' --language ar

    # a roster whose names are Arabic and whose maps predate this column
    python backend/tools/translate_names.py --provider '...' --source ar --apply

Dry-run is the default, and ``--apply`` is what writes. Nothing here touches ``users.name``:
the canonical name is what every screen already reads, and a batch translation pass is not a
rename. The tool writes the column directly, like the other operator tools, so nothing lands
in ``audit_log``; who was translated and when is not a decision anybody has had to ask about
yet, and inventing a trail row nobody reads would be worse than saying so here.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

#: The backend directory, so ``names`` imports when this is run from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import names  # noqa: E402 - after the path insert above, which is what makes it importable

#: How long one provider call may take. A name is a few dozen bytes; anything slower than this
#: is a provider that is not answering, and the roster must not wait behind it one name at a time.
TIMEOUT_SECONDS = 20.0


def provider_call(command: str, text: str, source: str, target: str) -> str | None:
    """One translation, or ``None`` when the command has none to give.

    The command is run through the shell because it is written for one - ``trans -t "$target"``
    is a shell word, not a program name - and everything it needs is in its environment as well
    as on stdin. A failure of any kind (refused, crashed, timed out, empty answer) is answered
    with ``None`` rather than an exception: the caller is a batch over a roster, and one name a
    provider cannot handle must not stop the other hundred.
    """
    environment = {
        **os.environ,
        "NAME_TEXT": text,
        "NAME_SOURCE": source,
        "NAME_TARGET": target,
    }
    try:
        completed = subprocess.run(
            command,
            shell=True,
            input=text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=TIMEOUT_SECONDS,
            env=environment,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    answer = " ".join((completed.stdout or "").split())
    return answer or None


def accounts(conn: sqlite3.Connection, wanted: list[str], languages: list[str]) -> list[sqlite3.Row]:
    """The accounts whose map is missing a language this run is asked to fill.

    The filter is the work, not the roster: an account with every requested slot filled is not
    sent to a provider at all, and an account whose provider answered *nothing* stays in the
    list - so a second run retries exactly what is still missing instead of reporting it done.
    """
    query = "SELECT id, name, role, name_i18n FROM users"
    parameters: list[str] = []
    if wanted:
        query += " WHERE id IN (%s)" % ",".join("?" * len(wanted))
        parameters.extend(wanted)
    query += " ORDER BY CAST(id AS INTEGER)"
    rows = conn.execute(query, parameters).fetchall()
    return [
        row for row in rows
        if [language for language in names.missing(row["name_i18n"]) if language in languages]
    ]


def plan(rows: list[sqlite3.Row], languages: list[str], command: str, source: str) -> list[dict]:
    """What each account's map would become, without writing anything.

    ``names.translate`` fills from the first language the map carries. An account with *no* map
    at all - every account created before the column existed - still has its canonical
    ``users.name``, and a provider has to be told what language that is; ``source`` is that
    answer, and it is the operator's to give rather than something to guess here.
    """
    wanted = set(languages)
    asked: list[str] = []

    def translate_one(text: str, source_language: str, target: str) -> str | None:
        # Only the languages this run was asked for: a provider call for a slot that is about to
        # be dropped is a name sent to a third party for nothing.
        if target not in wanted:
            return None
        asked.append(target)
        return provider_call(command, text, source_language, target)

    planned: list[dict] = []
    for row in rows:
        before = names.parse(row["name_i18n"])
        after = names.translate(before or {source: row["name"]}, translate_one=translate_one)
        # The slots that were already there are kept whatever the run asked for: a pass that
        # fills the Arabic slot must not drop the English one it translated from.
        after = {
            language: value for language, value in after.items()
            if language in before or language in wanted
        }
        if after != before:
            planned.append({"id": str(row["id"]), "name": row["name"], "before": before, "after": after})
    return planned


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fill the missing languages of every stored name, through a translator command.",
    )
    parser.add_argument("--db", default=None, help="the database file (default: the configured one)")
    parser.add_argument("--provider", default=os.environ.get("NAME_TRANSLATOR", ""),
                        help="the command that translates one name (default: $NAME_TRANSLATOR)")
    parser.add_argument("--id", action="append", default=[],
                        help="one account id to translate; repeat for several (default: all)")
    parser.add_argument("--language", action="append", default=[],
                        choices=list(names.LANGUAGES),
                        help="a language to fill; repeat for several (default: all four)")
    parser.add_argument("--source", default="en", choices=list(names.LANGUAGES),
                        help="the language of an account's canonical name, for accounts with no map "
                             "yet (default: en, the language the console's own form is written in)")
    parser.add_argument("--apply", action="store_true", help="write the maps (default: dry run)")
    parser.add_argument("--json", action="store_true", help="print the plan as JSON")
    args = parser.parse_args(argv)

    if not args.provider:
        print(
            "no provider: pass --provider, or set NAME_TRANSLATOR.\n"
            "The command is run once per name and language, receives the name on stdin and in\n"
            "$NAME_TEXT, and answers the translation on stdout. Nothing was read or written.",
            file=sys.stderr,
        )
        return 2

    if args.db:
        database = Path(args.db).expanduser()
    else:
        from config import settings  # imported here so --db needs no configuration at all

        database = Path(settings.database_path)

    languages = args.language or list(names.LANGUAGES)
    connection = sqlite3.connect(str(database), timeout=30.0)
    # Rows are indexed by column name, as the application's own ``db()`` hands them out: the
    # reads below are written the way every other read in this repository is.
    connection.row_factory = sqlite3.Row
    try:
        rows = accounts(connection, args.id, languages)
        planned = plan(rows, languages, args.provider, args.source)
        if args.json:
            print(json.dumps(planned, ensure_ascii=False, indent=2))
        else:
            for entry in planned:
                for language in names.LANGUAGES:
                    before, after = entry["before"].get(language, ""), entry["after"].get(language, "")
                    if before != after:
                        print(f"{entry['id']}  {language}  {after!r}")
            print(f"{len(planned)} of {len(rows)} account(s) missing a requested language gained one")
        if not args.apply:
            print("dry run: nothing was written. Pass --apply to write these maps.")
            return 0
        with connection:  # one transaction: a crash halfway leaves the roster as it was
            for entry in planned:
                connection.execute(
                    "UPDATE users SET name_i18n = ? WHERE id = ?",
                    (names.serialise(entry["after"]), entry["id"]),
                )
        print(f"wrote {len(planned)} account(s).")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
