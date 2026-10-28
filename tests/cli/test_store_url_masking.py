# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""No operator-facing string names the store with its password still in it.

A store URL is a credential when the store is a server. Commands that report
which store they consulted, or refuse to open one, print that URL, and those
strings land in terminal scrollback, CI logs, and stored machine-mode JSON.

Two channels carry the same secret: the URL itself, and an exception's own
message, which is separate code and unaffected by masking the field beside it.

The end-to-end tests run the real CLI in a subprocess and read stdout and
stderr separately, so "it did not appear" says where. Each also asserts the
masked form is present, to distinguish a command that ran and masked from one
that never reached the store — an early probe of this fix mistyped the
invocation, got a clean password check from commands that all answered "no
such command", and read as a pass.

A store URL with no scheme is read as a filesystem path, so a credential in
one puts a password in a real path — evading a masker that only parses URLs.
That shape reaches a store setting through the ``./`` spelling, or as a bare
``user:secret@host/db`` whose secret holds a ``/``, a ``:`` or whitespace,
since only one without them is refused before it resolves. The masker does
not match such a secret either when it holds a ``/`` or whitespace, and keeps
the part before its last ``:``. It is still tested against the bare form
directly, since it is handed strings built by drivers and older logs, not only
strings that passed our own validation.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

import lionagi.state.db as db_mod

PASSWORD = "hunter2-correct-horse"
MASKED = "dbuser:***@"

SERVER_URL = f"postgresql://dbuser:{PASSWORD}@127.0.0.1:59999/lionagi"
SCHEMELESS_URL = f"dbuser:{PASSWORD}@127.0.0.1/x"
# The same shape as a store setting. `./` is what says "this really is a path"
# to the check that otherwise refuses a scheme-less credential outright, and it
# changes nothing about what lands on disk: the credential is still in the name
# of a real file, which is the leak these arms are about.
CREDENTIALED_PATH = f"./{SCHEMELESS_URL}"


def _set_url(monkeypatch, url: str) -> None:
    """Settings are frozen, so redirect the whole object the module reads."""
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": url})
    )


def _assert_masked(text: str, where: str) -> None:
    assert PASSWORD not in text, f"the password reached {where}: {text!r}"
    assert PASSWORD[:6] not in text, f"part of the password reached {where}: {text!r}"
    assert MASKED in text, (
        f"{where} does not name the store at all, so it asserts nothing: {text!r}"
    )


# the masker itself


@pytest.mark.parametrize(
    ("url", "masked"),
    [
        (SERVER_URL, "postgresql://dbuser:***@127.0.0.1:59999/lionagi"),
        # Unencoded `/`, `:`, `@`, `?` and `#` in a password: urlparse finds no
        # password in the first or the last, and a scan that stops at any of them
        # would keep part of it.
        (
            "postgresql+asyncpg://dbuser:pa/ss@db.example/lionagi",
            "postgresql+asyncpg://dbuser:***@db.example/lionagi",
        ),
        ("postgresql://u:pa:ss@h/db", "postgresql://u:***@h/db"),
        ("postgresql://u:pa@ss@h/db", "postgresql://u:***@h/db"),
        ("postgresql://u:pa?ss#x@h/db", "postgresql://u:***@h/db"),
        # a user name holding an `@`, which SQLAlchemy reads as the user's
        ("postgresql://alice@corp:pa/ss@h/db", "postgresql://alice@corp:***@h/db"),
        ("postgresql://u:pw123@h:notaport/db", "postgresql://u:***@h:notaport/db"),
        (SCHEMELESS_URL, "dbuser:***@127.0.0.1/x"),
    ],
)
def test_a_password_holding_url_delimiters_is_masked_whole(url, masked):
    from lionagi.state.engine import mask_credentials, mask_db_url

    assert mask_db_url(url) == masked
    assert mask_credentials(f"could not open {url}: refused") == f"could not open {masked}: refused"


def test_a_url_given_alone_is_masked_to_its_last_at_whitespace_included():
    # in prose a space ends the password, so only the store URL itself can take it whole
    from lionagi.state.engine import mask_db_url

    assert mask_db_url("postgresql://u:pa ss@h/db") == "postgresql://u:***@h/db"
    assert mask_db_url("postgresql://alice@corp:pa ss/x@h/db") == "postgresql://alice@corp:***@h/db"
    assert mask_db_url("postgresql://u:pa@ss ok@h/db") == "postgresql://u:***@h/db"
    assert mask_db_url("postgresql://my user:pa ss@h/db") == "postgresql://my user:***@h/db"
    assert mask_db_url("postgresql://u:pa\nss@h/db") == "postgresql://u:***@h/db"
    assert mask_db_url("oracle+cx_oracle://u:pa ss@h/db") == "oracle+cx_oracle://u:***@h/db"
    assert mask_db_url("_pg://u:pa ss@h/db") == "_pg://u:***@h/db"
    assert mask_db_url("postgresql://u:***@h/db") == "postgresql://u:***@h/db"


def test_a_password_given_as_a_query_parameter_is_masked():
    from lionagi.state.engine import mask_credentials, mask_db_url

    url = "postgresql+asyncpg://u@h/db?sslmode=require&password=pa/ss@x&target=primary"
    masked = "postgresql+asyncpg://u@h/db?sslmode=require&password=***&target=primary"
    assert mask_db_url(url) == masked and mask_db_url(masked) == masked
    assert mask_credentials(f"could not open {url}: refused") == f"could not open {masked}: refused"
    assert mask_db_url("postgresql://u@h/db?password=pw123") == "postgresql://u@h/db?password=***"
    assert mask_db_url("postgresql://u@h/db?PASSWORD=pw123") == "postgresql://u@h/db?PASSWORD=***"
    assert mask_db_url("postgresql://u@h/db?PASSWORD=pa ss") == "postgresql://u@h/db?PASSWORD=***"
    # SQLAlchemy decodes the key, so a percent-encoded one is the same parameter
    for key in ("%70assword", "%50ASSWORD", "pass%77ord"):
        assert mask_db_url(f"postgresql://u@h/db?{key}=pa/ss") == f"postgresql://u@h/db?{key}=***"
    # a key standing inside another parameter's value is masked too
    assert (
        mask_db_url("postgresql://u@h/db?x=a?password=pw") == "postgresql://u@h/db?x=a?password=***"
    )
    # urlsplit drops a tab or line break wherever it stands, so asyncpg reads these keys as `password`
    for key in ("\rpassword", "pass\nword", "password\t", "\tpass\r\nword"):
        assert mask_db_url(f"postgresql://u@h/db?{key}=pw2") == f"postgresql://u@h/db?{key}=***"
    assert (
        mask_credentials("could not open postgresql://u@h/db?x=1&\rpassword=pw2 (refused)")
        == "could not open postgresql://u@h/db?x=1&\rpassword=*** (refused)"
    )
    # a C0 control character or space before the scheme, which urlsplit strips, is passed over
    for lead in (" ", "\t", "\n", "\r", "\x0b", "\x0c", "\x00", "\x01", "\x1f"):
        assert mask_db_url(f"{lead}postgresql://u:pa ss@h/db") == f"{lead}postgresql://u:***@h/db"
    # and so is a tab or line break inside the scheme or its `://`, which urlsplit removes
    for url in (
        "postg\nresql://u:pa ss@h/db",
        "postgresql:\r//u:pa ss@h/db",
        "postgresql:/\t/u:pa ss@h/db",
    ):
        assert mask_db_url(url) == url.replace("pa ss", "***")
    assert (
        mask_credentials("could not open postgresql://u@h/db?PASSWORD=pw123 (refused)")
        == "could not open postgresql://u@h/db?PASSWORD=*** (refused)"
    )
    # any name SQLAlchemy reads as a dialect, `_` and digits included
    for scheme in ("pg_1", "_1"):
        assert (
            mask_credentials(f"could not open {scheme}://u:pa/ss@h/db")
            == f"could not open {scheme}://u:***@h/db"
        )


@pytest.mark.parametrize("dialect", ["postgresql", "postgresql+asyncpg"])
def test_an_at_in_a_query_password_takes_the_host_with_the_users_password(dialect):
    # SQLAlchemy ends the user's password at the host's `@`; a parser that splits the user
    # from the host at the last `@` reads it on to the value's `@`, so the host goes too
    from lionagi.state.engine import mask_credentials, mask_db_url

    url = f"{dialect}://u:pw@h/db?password=ab@cdefgh"
    masked = f"{dialect}://u:***"
    assert mask_db_url(url) == masked and mask_db_url(masked) == masked
    assert (
        mask_credentials(f"could not open {url} (refused)") == f"could not open {masked} (refused)"
    )


@pytest.mark.parametrize("marker", ["?password=", "&password=", "?PASSWORD="])
def test_a_password_holding_a_query_password_key_is_masked_with_what_follows(marker):
    # SQLAlchemy reads the key as part of the user's password, which ends at the first `@`;
    # read as a query parameter its value runs on past that `@`, so both spans go
    from lionagi.state.engine import mask_credentials, mask_db_url

    url = f"postgresql://u:SECRET{marker}b@h/db?password=c"
    masked = "postgresql://u:***"
    assert mask_db_url(url) == masked and mask_db_url(masked) == masked
    assert (
        mask_credentials(f"could not open {url} (refused)") == f"could not open {masked} (refused)"
    )
    # a `/` the text scan stops at, or a space in a URL given alone: the URL readings still
    # reach the password
    assert (
        mask_credentials(f"could not open postgresql://u:SE/CRET{marker}b@h/db (refused)")
        == "could not open postgresql://u:*** (refused)"
    )
    assert mask_db_url(f"postgresql://u:SE CRET{marker}b@h/db") == "postgresql://u:***"
    # and where the string does not open with the scheme: quoted, bracketed, or no scheme at all
    for lead, end in (('"', '"'), ("<", ">")):
        quoted = f"{lead}postgresql://u:SE/CRET{marker}b@h/db{end}"  # a `/` the text scan stops at
        assert mask_db_url(quoted) == f"{lead}postgresql://u:***"
    bare = f"/tmp/x/dbuser:SECRET{marker}b@127.0.0.1/x"
    assert mask_db_url(bare) == "/tmp/x/dbuser:***"
    assert mask_credentials(f"open {bare} failed") == "open /tmp/x/dbuser:*** failed"


@pytest.mark.parametrize(
    ("text", "masked"),
    [
        # the first value takes the credential's `u:`, and the credential holds the second key
        ("open x?password=u:&password=SECRET@h failed", "open x?password=*** failed"),
        (
            "open postgresql://h/db?password=pa:ss&x=1&password=q@r failed",
            "open postgresql://h/db?password=*** failed",
        ),
        # the text scan stops at the value's `/`; read with the value blanked it reaches the `@`
        ("open a.b:SECRET?password=x/y&@h failed", "open a.b:***@h failed"),
    ],
)
def test_a_query_password_overlapping_a_credential_is_masked_with_it(text, masked):
    from lionagi.state.engine import mask_credentials

    assert mask_credentials(text) == masked


def test_a_password_key_between_the_users_password_and_the_host_is_masked_with_both():
    # SQLAlchemy reads the password as `p` and the host as `ss&password=q@h`; urlparse, which
    # ends the authority at the `/`, reads the password as `p@ss&password=q`
    from lionagi.state.engine import mask_credentials, mask_db_url

    url = "postgresql://u:p@ss&password=q@h/db"
    assert mask_db_url(url) == "postgresql://u:***"
    assert (
        mask_credentials(f"could not open {url} (refused)")
        == "could not open postgresql://u:*** (refused)"
    )


@pytest.mark.performance
@pytest.mark.parametrize(
    "unit",
    [
        "?password=",
        "&password=",
        "?x=",
        "a",
        "a.",
        "-a://",
        "a://u:",
        "a://u:p@h ",
        "x@",
        "u:p@",
        "a:b!",
        "password=",
        " password='",
        ' password="\\',
        " password=\\",
        "password",
        "a-",
        "password=''x",
        "password='a'\"b",
        " password=a'",
        " password=a$'",
        ' password=a"',
        " password=a'b c",
        " password=$'\\",
        'password=a"',
        " password='a\\'b",
        # a key inside every value, each value opening a quote the last one closes
        "password='",
        'password="',
        "password=$'",
        "password=\\'",
        "&PGPASSWORD='",
        # and each value opening a part the next one opens inside, or one read to the
        # end of the text
        'password="$(',
        "password=${",
        'password="${',
        "password=$( (",
        "password=<(",
        "password=(",
        'password="${ ',
        "password=${PW:-<(",
        "password=${PW:-{",
        # values that end well before the next space, of which there is none
        "password='a';",
        "password=''x;",
    ],
)
def test_masking_time_grows_with_the_length_of_the_text(unit):
    # each reading scans the text once and looks positions up, and a value starting inside
    # another, outside its quotes and escapes, is not read again, so a long or repetitive
    # message cannot stall a log call:
    # 200,000 characters of these take a fraction of a second. The bound is wall-clock
    # time, so the test runs in the serial, uninstrumented performance lane: under
    # coverage tracing the same reading takes several times as long
    from lionagi.state.engine import mask_credentials, mask_db_url

    text = "postgresql://u@h/db" + unit * (200_000 // len(unit))
    for mask in (mask_credentials, mask_db_url):
        start = time.perf_counter()
        mask(text)
        assert time.perf_counter() - start < 5, (mask.__name__, unit)


def test_values_opened_inside_each_other_are_masked_as_one_value():
    # the same text as the timed test below, short enough to run in every suite
    from lionagi.state.engine import mask_credentials, mask_db_url

    text = "password=${" * 1_000 + "}" * 1_000
    for mask in (mask_credentials, mask_db_url):
        assert mask(text) == "password=***", mask.__name__


@pytest.mark.performance
def test_values_opened_inside_each_other_and_closed_are_not_read_again():
    # each value starts inside the one before it and, past its own `}`, comes to text
    # the outer value's reading has passed: it ends where that reading did instead of
    # reading on, so 500,000 characters take about a second, not half a minute
    from lionagi.state.engine import mask_credentials, mask_db_url

    pairs = 500_000 // len("password=${}")
    text = "password=${" * pairs + "}" * pairs
    for mask in (mask_credentials, mask_db_url):
        start = time.perf_counter()
        assert mask(text) == "password=***", mask.__name__
        assert time.perf_counter() - start < 5, mask.__name__


def test_a_query_password_ending_where_the_users_password_begins_is_masked_with_it():
    # SQLAlchemy reads the user as `x?password=u` and the password as `&pw`
    from lionagi.state.engine import mask_db_url

    assert mask_db_url("postgresql://x?password=u:&pw@h/db") == "postgresql://x?password=***@h/db"


@pytest.mark.parametrize(
    "url",
    ["postgresql+asyncpg://u@h/db?password=pa#ss", "postgresql+asyncpg://u@h/db?password=pa ss"],
)
def test_a_query_password_is_masked_to_the_next_ampersand(url):
    # SQLAlchemy reads the value to the next `&`, a `#` or a space included
    from lionagi.state.engine import mask_credentials, mask_db_url

    masked = "postgresql+asyncpg://u@h/db?password=***"
    assert mask_db_url(url) == masked
    assert mask_db_url(f"{url}&x=1") == f"{masked}&x=1"
    if " " not in url:
        assert (
            mask_credentials(f"could not open {url} (refused)")
            == f"could not open {masked} (refused)"
        )


def test_a_url_with_no_scheme_is_masked_too():
    """The shape ``urlparse`` cannot decompose.

    Refused as a store setting, it still reaches ``mask_credentials`` from
    every other direction: a driver quoting what it was handed, an older log
    line, a path spelled with the ``./`` that makes it acceptable.
    ``mask_db_url`` ends with the same passes.
    """
    from lionagi.state.engine import mask_db_url

    _assert_masked(mask_db_url(SCHEMELESS_URL), "mask_db_url of a scheme-less URL")


def test_credentials_are_masked_inside_prose():
    from lionagi.state.engine import mask_credentials

    message = f"state.db not found at /var/lib/{SCHEMELESS_URL} — nothing was created"
    _assert_masked(mask_credentials(message), "mask_credentials of a message")
    # whitespace ends a URL in prose, so a later `@` is not where its password ends
    assert (
        mask_credentials("open postgresql://u:pw@h/db for alice@example.com")
        == "open postgresql://u:***@h/db for alice@example.com"
    )


@pytest.mark.parametrize(
    ("text", "masked"),
    [
        # a keyword/value connection string, as libpq takes one
        (
            "could not connect: host=db user=u password=pw123 dbname=x",
            "could not connect: host=db user=u password=*** dbname=x",
        ),
        (
            "could not connect: password = pw123 host=db",
            "could not connect: password = *** host=db",
        ),
        ("could not connect: PASSWORD=pw123", "could not connect: PASSWORD=***"),
        # a key ending in `password`: an environment variable or a flag
        ("PGPASSWORD=pw123 psql -h db", "PGPASSWORD=*** psql -h db"),
        ("started with --db-password=pw123", "started with --db-password=***"),
        # libpq's reading: quoted, to the first `'` a backslash does not escape;
        # unquoted, to the first whitespace a backslash does not escape
        ("host=db password='a\\'b c' dbname=x", "host=db password=*** dbname=x"),
        ("host=db password=pa\\ ss dbname=x", "host=db password=*** dbname=x"),
        ("connect(password=pw123) failed", "connect(password=*** failed"),
        # a shell's: a quoted part joins the word, a `'..'` part has no escapes, and
        # a `".."` or `$'..'` part ends at a closing quote no backslash escapes
        ("PGPASSWORD=ab'cd ef'gh psql", "PGPASSWORD=*** psql"),
        ('PGPASSWORD=QZ"XK YY" psql', "PGPASSWORD=*** psql"),
        ("PGPASSWORD='a\\'b'c d' psql", "PGPASSWORD=*** psql"),
        ("PGPASSWORD=$'ab cd' psql", "PGPASSWORD=*** psql"),
        ("PGPASSWORD=$'a\\'b c' psql", "PGPASSWORD=*** psql"),
        ("PGPASSWORD=x$'a\\'b c' psql", "PGPASSWORD=*** psql"),
        # a shell without `$'..'` quoting reads `$` as a character, then `'\\'` and `' y'`
        ("PGPASSWORD=$'\\'' y' psql", "PGPASSWORD=*** psql"),
        ("PGPASSWORD='ab'cd psql -h db", "PGPASSWORD=*** psql -h db"),
        ("password=''cd psql", "password=*** psql"),
        ('connect(host="db", password="pa ss")', 'connect(host="db", password=***'),
        ("password=it's and the dog's bowl", "password=*** bowl"),
        # a quote never closed runs to the end of the text, and so does a `\'` that
        # a shell reads as the closing quote, which makes the last `'` an opening one
        ("host=db password='pa ss dbname=x", "host=db password=***"),
        ("host=db password='pa ss\\'x' dbname=x", "host=db password=***"),
        # a shell does not split on a carriage return, form feed or vertical tab
        ("PGPASSWORD=ab\rcd psql", "PGPASSWORD=*** psql"),
        ("PGPASSWORD=ab\x0ccd\x0bef psql", "PGPASSWORD=*** psql"),
        ("PGPASSWORD='ab'c\rd psql", "PGPASSWORD=*** psql"),
        # past the longest reading the mask goes on to the next space, tab or line feed
        ("PGPASSWORD='ab'; psql", "PGPASSWORD=*** psql"),
        ("PGPASSWORD='ab',x psql", "PGPASSWORD=*** psql"),
        ("connect(password='ab', host='h')", "connect(password=*** host='h')"),
        ("[password='ab'] {password='ab'}", "[password=*** {password=***"),
        ('connect(password="ab"cd) failed', "connect(password=*** failed"),
        ("password=abc\\", "password=***"),
        # whitespace after the `=` is passed over, as libpq passes it over
        ("password= pw123", "password= ***"),
        # the readings are ASCII, so a no-break or other Unicode space is part of
        # the value
        ("host=db password=pa\xa0ss dbname=x", "host=db password=*** dbname=x"),
        ("host=db password=pa\x1fss\x85x dbname=x", "host=db password=*** dbname=x"),
        ("password=\xa0pw123 dbname=x", "password=*** dbname=x"),
        # a key standing inside another key's value is read as well
        ("password=ab;password='c d' x", "password=*** x"),
        ("PGPASSWORD=x;PGPASSWORD=y;PGPASSWORD='c d' z", "PGPASSWORD=*** z"),
        # from its own start, even inside a quoted part: a shell reads `x' tail` as `x`
        # and a quote never closed
        ("password='password=x' tail", "password=***"),
        # a Python string literal is read none of these ways: Python joins `'a' 'b c'` into
        # one string, and only `'a'` is masked; it keeps the backslash in `r'a\'b c'`, which
        # is masked only up to its space
        ("connect(password='a' 'b c')", "connect(password=*** 'b c')"),
        ("connect(password=r'a\\'b c')", "connect(password=*** c')"),
        # a backtick part runs to the next backtick a backslash does not escape
        ("PGPASSWORD=`cat pwfile` psql", "PGPASSWORD=*** psql"),
        ("PGPASSWORD=`echo a\\` b` psql", "PGPASSWORD=*** psql"),
        # where a `$(` or `$[` part ends only a shell's command grammar finds, so it runs
        # to the end of the text, whatever it holds
        ("PGPASSWORD=$(cat pwfile) psql", "PGPASSWORD=***"),
        ("PGPASSWORD=$(echo ')' \\) \"x)\" b)c psql", "PGPASSWORD=***"),
        ("PGPASSWORD=$(echo `x)` b) psql", "PGPASSWORD=***"),
        ("PGPASSWORD=$(echo a b", "PGPASSWORD=***"),
        ("PGPASSWORD=$(echo a)'b c' psql", "PGPASSWORD=***"),
        ("PGPASSWORD=$(printf $'\\') tail", "PGPASSWORD=***"),
        ("PGPASSWORD=$(echo $(date) b) psql", "PGPASSWORD=***"),
        ("PGPASSWORD=$( (echo a b) ) psql", "PGPASSWORD=***"),
        ("PGPASSWORD=$((1 + 2)) psql", "PGPASSWORD=***"),
        ("PGPASSWORD=$(case x in x) echo hunter2;; esac) psql", "PGPASSWORD=***"),
        ("PGPASSWORD=$(true # )\necho hunter2) psql", "PGPASSWORD=***"),
        ("PGPASSWORD=$[ 1234 + 1 ] psql", "PGPASSWORD=***"),
        # and so do bash's `${ ..; }` and `${| ..; }`, a `<(` or `>(` part, and in the
        # word itself whatever an unquoted `(` opens: zsh's `=(..)`, a glob's `@(..)`
        ("PGPASSWORD=${ echo hunter 2; } psql", "PGPASSWORD=***"),
        ("PGPASSWORD=${| REPLY=hunter; } psql", "PGPASSWORD=***"),
        ("--db-password=<(echo hunter 2) psql", "--db-password=***"),
        ("PGPASSWORD=a>(cat) b", "PGPASSWORD=***"),
        ("PGPASSWORD=${PW:-<(echo } hunter2)} psql", "PGPASSWORD=***"),
        ("PGPASSWORD==(echo hunter 2) psql", "PGPASSWORD=***"),
        ("PGPASSWORD=@(hunter 2) psql", "PGPASSWORD=***"),
        # and bash reads a `<(` in a `${..}` part inside a `".."` part as one too
        ('PGPASSWORD="${PW:-<(a}" b', "PGPASSWORD=***"),
        ('PGPASSWORD="${PW:-<(echo a}" hunter 2)" x)}" psql', "PGPASSWORD=***"),
        # a `(` in a `${..}` part, and a `<(` in a `".."` part, are plain characters
        ("PGPASSWORD=${PW:-(a} b", "PGPASSWORD=*** b"),
        ('PGPASSWORD="<(echo a" b', "PGPASSWORD=*** b"),
        # a `".."` part reads a `$(`, `$[` or `${ ` inside it the same way, a `${` and a
        # backtick part as parts, and a `'` or `$'` as plain characters
        ('PGPASSWORD="$(echo "hunter 2")" psql', "PGPASSWORD=***"),
        ('PGPASSWORD="$[ 1234 + 1 ]" psql', "PGPASSWORD=***"),
        ('PGPASSWORD="${ echo hunter 2; }" psql', "PGPASSWORD=***"),
        ('PGPASSWORD="a`echo "b c"`d" psql', "PGPASSWORD=*** psql"),
        ("PGPASSWORD=\"a'b\" c' psql", "PGPASSWORD=*** c' psql"),
        ("PGPASSWORD=\"$'a\" b' psql", "PGPASSWORD=*** b' psql"),
        # a `${` part runs to the `}` that closes it, neither escaped nor quoted
        ("PGPASSWORD=${PW:-a b} psql", "PGPASSWORD=*** psql"),
        ("PGPASSWORD=${PW:-'a}b c'} psql", "PGPASSWORD=*** psql"),
        ('PGPASSWORD="${PW:-"a } b"}" psql', "PGPASSWORD=*** psql"),
        ("PGPASSWORD=${PW:-\\} b} psql", "PGPASSWORD=*** psql"),
        ("PGPASSWORD=${PW:-a b", "PGPASSWORD=***"),
        # found when its `${` starts on the last character of a 64-character search window
        ("PGPASSWORD=" + "a" * 63 + "${PW:-a b} c psql", "PGPASSWORD=*** c psql"),
        # zsh reads an unquoted `{` in it as opening a part to its own `}`, where bash
        # and dash end at the first `}`: zsh's word is the longer
        ("PGPASSWORD=${PW:-{a} hunter 2} psql", "PGPASSWORD=*** psql"),
        # inside a `".."` part bash reads a `'` in it as a quote, where other shells
        # read a plain character: here bash's word is the longer
        ('PGPASSWORD="${PW:-\'a}"b c\'}" psql', "PGPASSWORD=*** psql"),
        # and here the other shells', whose `"` inside opens a quote never closed
        ('PGPASSWORD="${PW:-\'"\'}" b" psql', "PGPASSWORD=***"),
    ],
)
def test_a_password_written_as_a_keyword_is_masked(text, masked):
    from lionagi.state.engine import mask_credentials, mask_db_url

    assert mask_credentials(text) == masked
    assert mask_db_url(text) == masked
    assert mask_credentials(masked) == masked


@pytest.mark.parametrize("operator", list("|&;)<>"))
def test_a_shell_word_ends_at_an_unquoted_operator(operator):
    # what follows the operator is another word, here a quoted command or file name; only
    # libpq's reading, which runs to whitespace, takes in its first part
    from lionagi.state.engine import mask_credentials, mask_db_url

    text = f"PGPASSWORD=pw{operator}'a b' c"
    assert mask_credentials(text) == "PGPASSWORD=*** b' c"
    assert mask_db_url(text) == "PGPASSWORD=*** b' c"


def test_an_unquoted_parenthesis_runs_to_the_end_of_the_text():
    # in a word an unquoted `(` is a syntax error or opens a part whose end only a shell's
    # command grammar finds, so what follows it is masked too
    from lionagi.state.engine import mask_credentials, mask_db_url

    text = "PGPASSWORD=pw('a b' c"
    assert mask_credentials(text) == "PGPASSWORD=***"
    assert mask_db_url(text) == "PGPASSWORD=***"


def test_a_key_after_an_ampersand_is_read_as_a_shell_reads_it_too():
    # after a `&` the key is a query parameter or follows a shell's `&`, so it is read as a
    # shell reads it as well, to the next `&` too, where a query parameter's value ends
    from lionagi.state.engine import mask_credentials, mask_db_url

    assert mask_credentials("PGPASSWORD=x&PGPASSWORD='c d' psql") == "PGPASSWORD=*** psql"
    # a URL given alone has a query value masked to the next `&`, whatever it holds
    assert mask_db_url("PGPASSWORD=x&PGPASSWORD='c d' psql") == "PGPASSWORD=***"
    text = "open x?a=1&password='p w'&b=2 failed"
    for mask in (mask_credentials, mask_db_url):
        assert mask(text) == "open x?a=1&password=***&b=2 failed"
        assert mask(mask(text)) == mask(text)
    # but the shell's reading goes no further than the next `&`: a quote never closed
    # does not take in the parameters after it, and a value holding a quoted or escaped
    # `&` is masked only up to that `&`
    url = "postgresql://u@h/db?x=1&password=it's&sslmode=require"
    assert mask_db_url(url) == "postgresql://u@h/db?x=1&password=***&sslmode=require"
    assert mask_credentials(f"open {url} failed") == (
        "open postgresql://u@h/db?x=1&password=***&sslmode=require failed"
    )
    for mask in (mask_credentials, mask_db_url):
        assert mask("x&password='a&b'&c=d") == "x&password=***&b'&c=d"
        assert mask("x&password=a\\&b c") == "x&password=***&b c"


@pytest.mark.parametrize(
    "untouched",
    [
        "the password was rejected",
        "passwords=3",
        "password_file=/etc/pw host=db",
        "password=",
        "password=''",
        'password=""',
        "connect(password='')",
        "postgresql://u@h/db?password=&sslmode=require",
    ],
)
def test_a_password_keyword_with_no_value_or_a_longer_key_is_left_alone(untouched):
    from lionagi.state.engine import mask_credentials, mask_db_url

    assert mask_credentials(untouched) == untouched
    assert mask_db_url(untouched) == untouched


def test_a_query_password_keeps_its_own_end():
    # after `?` or `&` the key is a query parameter, whose value ends at the next `&`
    from lionagi.state.engine import mask_credentials

    assert (
        mask_credentials("open postgresql://u@h/db?password=pw&sslmode=require failed")
        == "open postgresql://u@h/db?password=***&sslmode=require failed"
    )


def test_a_query_key_ending_in_password_is_masked_as_the_keyword_is():
    from lionagi.state.engine import mask_credentials, mask_db_url

    url = "postgresql://u@h/db?db_password=pw123&sslmode=require"
    assert mask_db_url(url) == "postgresql://u@h/db?db_password=***&sslmode=require"
    assert mask_credentials(f"open {url} failed") == (
        "open postgresql://u@h/db?db_password=***&sslmode=require failed"
    )
    # a longer key is not a password
    hint = "postgresql://u@h/db?password_hint=x&sslmode=require"
    assert mask_db_url(hint) == hint
    # in prose the value ends at ASCII whitespace, so a no-break space stays inside it
    assert mask_credentials("open postgresql://u@h/db?db_password=ab\xa0cd failed") == (
        "open postgresql://u@h/db?db_password=*** failed"
    )


def test_masking_is_idempotent():
    """The sinks compose these, so masking an already-masked store URL must leave it as it is."""
    from lionagi.state.engine import mask_credentials, mask_db_url

    once = mask_db_url(SERVER_URL)
    assert mask_credentials(once) == once
    assert mask_credentials(mask_credentials(once)) == once


@pytest.mark.parametrize(
    "untouched",
    [
        "sqlite+aiosqlite:////var/lib/state.db",
        "postgresql://dbuser@127.0.0.1/lionagi",
        "[Errno 61] Connect call failed ('127.0.0.1', 59999)",
        "mail alice@example.com about the 12:30 window",
    ],
)
def test_strings_with_no_credential_are_returned_unchanged(untouched):
    """Over-masking would corrupt paths and messages, so the must-not-match arm
    matters as much as the must-match one."""
    from lionagi.state.engine import mask_credentials, mask_db_url

    assert mask_credentials(untouched) == untouched
    assert mask_db_url(untouched) == untouched


# per sink


def test_the_absent_store_refusal_names_a_masked_store(monkeypatch):
    from lionagi.cli.machine import state_db_absent

    _set_url(monkeypatch, SERVER_URL)
    _assert_masked(state_db_absent()["detail"], "the not-found detail")


def _raise_quoting_the_store(monkeypatch) -> None:
    """Make the open fail with a message that quotes the store, credential and all.

    The exception a closed port produces does not name the store, so asserting
    a password's absence from it asserts nothing: the string could not contain
    it under any condition. What is under test here is the sink's handling of a
    message that *does* carry the URL, so the message is supplied rather than
    hoped for. Our own store raises exactly this shape when a read-only open
    finds no file, and a driver that quotes its connection string is the case
    we do not control at all.
    """
    from lionagi.state.db import StateDB

    async def failing_open(self) -> None:
        raise RuntimeError(f"could not open {SERVER_URL}")

    monkeypatch.setattr(StateDB, "open", failing_open)


@pytest.mark.anyio
async def test_the_readonly_seam_masks_the_store_it_names(monkeypatch):
    from lionagi.cli.machine import readonly_state_db

    _set_url(monkeypatch, SERVER_URL)
    async with readonly_state_db() as (db, why):
        assert db is None, "this store cannot open, so the refusal is the thing under test"
        _assert_masked(why["detail"], "the seam's detail")


@pytest.mark.anyio
async def test_the_availability_wrapper_keeps_its_four_keys(monkeypatch):
    """Its key set is a published contract with its own test enumerating it.

    Pinned again from this side because the obvious home for an exception's
    message is a fifth key here, and the cost of putting one there is not
    visible from the code that would add it.
    """
    from lionagi.cli.machine import readonly_state_db

    _set_url(monkeypatch, SERVER_URL)
    async with readonly_state_db() as (db, why):
        assert set(why) == {"available", "value", "reason_code", "detail"}


@pytest.mark.anyio
async def test_the_writable_guard_masks_its_refusal(monkeypatch):
    from lionagi.cli.dispatch import _writable_state_db
    from lionagi.cli.machine import MachineError

    _set_url(monkeypatch, SERVER_URL)
    with pytest.raises(MachineError) as caught:
        async with _writable_state_db():
            pass

    _assert_masked(str(caught.value), "the write refusal's message")
    assert (caught.value.detail or {}).get("cause"), (
        f"the refusal carries no message, so the discriminator is not there: {caught.value.detail}"
    )
    # A refusal is an exception, not the availability wrapper, so its detail is
    # a free-form dict and carrying the message here costs no contract.


@pytest.mark.anyio
async def test_the_writable_guard_masks_a_message_that_quotes_the_store(monkeypatch):
    from lionagi.cli.dispatch import _writable_state_db
    from lionagi.cli.machine import MachineError

    _set_url(monkeypatch, SERVER_URL)
    _raise_quoting_the_store(monkeypatch)
    with pytest.raises(MachineError) as caught:
        async with _writable_state_db():
            pass

    _assert_masked((caught.value.detail or {}).get("cause", ""), "the write refusal's cause")


@pytest.mark.anyio
async def test_a_read_only_open_with_no_file_masks_the_path_it_names(monkeypatch, tmp_path):
    """The producer of the message that started this: a store URL with no
    scheme is read as a path, so the refusal names a path with a password in
    it."""
    from lionagi.state.db import StateDB

    monkeypatch.chdir(tmp_path)
    _set_url(monkeypatch, CREDENTIALED_PATH)
    with pytest.raises(FileNotFoundError) as caught:
        await StateDB(readonly=True).open()

    _assert_masked(str(caught.value), "the read-only open refusal")


def test_the_size_report_masks_a_server_url(monkeypatch):
    from lionagi.cli.state import _db_sizes

    _set_url(monkeypatch, SERVER_URL)
    sizes = _db_sizes()
    assert sizes["is_file"] is False
    _assert_masked(sizes["path"], "the size report's path")


def test_the_size_report_masks_a_credential_that_became_a_file_path(monkeypatch, tmp_path):
    """The branch that looks like it cannot carry a secret, and does.

    ``is_file`` is true here, so nothing about this answer suggests a
    credential is in it. That is exactly why masking only the server branch
    left the leak in place.
    """
    from lionagi.cli.state import _db_sizes

    monkeypatch.chdir(tmp_path)
    _set_url(monkeypatch, CREDENTIALED_PATH)
    sizes = _db_sizes()
    assert sizes["is_file"] is True, "this arm is meant to take the file branch"
    _assert_masked(sizes["path"], "the size report's path")


def test_a_newer_schema_refusal_names_a_masked_store(monkeypatch):
    """Raised on an ordinary open of a store a later release wrote, which is a
    routine upgrade-order mistake rather than a rare one."""
    from lionagi.state.db import SchemaTooNewError, StateDB

    _set_url(monkeypatch, SERVER_URL)
    db = StateDB()
    with pytest.raises(SchemaTooNewError) as caught:
        db._raise_if_schema_too_new("999999")

    _assert_masked(str(caught.value), "the schema-too-new refusal")


@pytest.mark.anyio
async def test_the_writable_guard_masks_the_store_it_reports_absent(monkeypatch, tmp_path):
    """The absent branch, which a credentialed path reaches whenever the file
    it names does not exist."""
    from lionagi.cli.dispatch import _writable_state_db
    from lionagi.cli.machine import MachineError

    monkeypatch.chdir(tmp_path)
    _set_url(monkeypatch, CREDENTIALED_PATH)
    with pytest.raises(MachineError) as caught:
        async with _writable_state_db():
            pass

    assert caught.value.kind == "not_found", f"this arm wanted the absent branch: {caught.value}"
    _assert_masked(str(caught.value), "the absent-store refusal")


def test_the_machine_dispatcher_masks_a_crash_that_quotes_the_store(monkeypatch, capfd):
    """The last sink before the envelope, and the only one that prints a message
    from code we do not own. A driver that quotes its connection string has
    nowhere else to be caught.

    ``capfd``, not ``capsys``: this dispatcher reserves stdout at the file
    descriptor level, so the envelope never passes through the object
    ``capsys`` replaces and the read comes back empty."""
    from lionagi.cli import machine

    def crash(argv):
        raise RuntimeError(f"could not reach {SERVER_URL}")

    monkeypatch.setattr(machine, "_run_machine_command", crash)
    assert machine.dispatch_machine(["handshake"]) == 0

    envelope = json.loads(capfd.readouterr().out)
    assert envelope["error"]["kind"] == "internal"
    _assert_masked(envelope["error"]["message"], "the crash envelope's message")


def test_the_lifecycle_read_masks_a_message_that_quotes_the_store(monkeypatch):
    from lionagi.cli.machine import lifecycle_data

    _set_url(monkeypatch, SERVER_URL)
    _raise_quoting_the_store(monkeypatch)
    why = lifecycle_data("run-that-does-not-matter")["lifecycle"]

    assert why["available"] is False
    _assert_masked(why["detail"], "the lifecycle detail")


def test_the_lifecycle_absent_answer_masks_the_path_it_names(monkeypatch, tmp_path):
    """``state_db_file()`` is a path, and a store URL with no scheme is how a
    password gets into one."""
    from lionagi.cli.machine import lifecycle_data

    monkeypatch.chdir(tmp_path)
    _set_url(monkeypatch, CREDENTIALED_PATH)
    why = lifecycle_data("run-that-does-not-matter")["lifecycle"]

    assert why["reason_code"] == "not_found", f"this arm wanted the absent branch: {why}"
    _assert_masked(why["detail"], "the lifecycle absent detail")


@pytest.mark.anyio
async def test_the_monitor_table_masks_a_message_that_quotes_the_store(monkeypatch):
    from lionagi.cli.monitor import _MonitorReadError, _run_table

    _set_url(monkeypatch, SERVER_URL)
    _raise_quoting_the_store(monkeypatch)
    with pytest.raises(_MonitorReadError) as caught:
        await _run_table(since=None, entity_type=None, project=None)
    _assert_masked(str(caught.value), "the monitor table's error line")


@pytest.mark.anyio
async def test_the_monitor_detail_masks_a_message_that_quotes_the_store(monkeypatch):
    from lionagi.cli.monitor import _MonitorReadError, _run_detail

    _set_url(monkeypatch, SERVER_URL)
    _raise_quoting_the_store(monkeypatch)
    with pytest.raises(_MonitorReadError) as caught:
        await _run_detail("some-entity")
    _assert_masked(str(caught.value), "the monitor detail's error line")


# end to end, both channels named


def _run_cli(args: list[str], url: str, cwd) -> subprocess.CompletedProcess:
    env = {**os.environ, "LIONAGI_STATE_DB_URL": url}
    return subprocess.run(
        [sys.executable, "-m", "lionagi.cli.main", *args],
        env=env,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=180,
    )


@pytest.mark.parametrize(
    "url", [SERVER_URL, CREDENTIALED_PATH], ids=["server", "credentialed-path"]
)
@pytest.mark.parametrize("args", [["--machine", "dispatch", "ls"], ["--machine", "stats", "runs"]])
def test_machine_mode_prints_the_password_on_neither_channel(tmp_path, url, args):
    """Machine mode is the channel that gets stored: its JSON lands in logs."""
    proc = _run_cli(args, url, tmp_path)

    assert PASSWORD not in proc.stdout, f"the password reached STDOUT: {proc.stdout!r}"
    assert PASSWORD not in proc.stderr, f"the password reached STDERR: {proc.stderr!r}"

    envelope = json.loads(proc.stdout)
    assert envelope["ok"] is True, f"the command did not run, so it masked nothing: {envelope}"
    assert MASKED in proc.stdout, (
        "no masked store name in the output, so this command never consulted the store and "
        f"the password check above is vacuous: {proc.stdout!r}"
    )


@pytest.mark.parametrize(
    "url", [SERVER_URL, CREDENTIALED_PATH], ids=["server", "credentialed-path"]
)
def test_the_human_size_report_prints_the_password_on_neither_channel(tmp_path, url):
    proc = _run_cli(["state", "stats"], url, tmp_path)

    assert PASSWORD not in proc.stdout, f"the password reached STDOUT: {proc.stdout!r}"
    assert PASSWORD not in proc.stderr, f"the password reached STDERR: {proc.stderr!r}"
    assert MASKED in proc.stdout, (
        "the report does not name the store at all, so it never reached the code under "
        f"test: {proc.stdout!r}"
    )
