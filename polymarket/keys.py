"""The wallet's private key, from the environment, the Bitwarden vault, or
standard input.

This module never prompts, prints or exits: the entry point that owns a
terminal passes ask_password and shows what KeyUnavailable says. The vault's
session token and master password live only in local variables here and in the
bw child process. Neither is written to this process's environment.
"""

import json
import os
import shutil
import subprocess
from typing import NamedTuple

SECURE_NOTE = 2


class KeyUnavailable(Exception):
    """No private key could be loaded. str() is the reason, fit to show the user."""


class BwResult(NamedTuple):
    ok: bool
    stdout: str
    stderr: str


def run_bw(args, session=None, stdin=None):
    """Run the Bitwarden CLI with these arguments.

    session reaches the child as BW_SESSION and stdin as its standard input.
    With no stdin the child reads nothing, so it cannot prompt on the terminal.
    """
    program = shutil.which("bw")
    if program is None:
        raise KeyUnavailable("the Bitwarden CLI (bw) is not installed")
    env = {**os.environ, "BW_SESSION": session} if session else None
    piped = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    done = subprocess.run([program, *args], env=env, capture_output=True, text=True, **piped)
    return BwResult(done.returncode == 0, done.stdout, done.stderr)


def load_private_key(ask_password, bw=run_bw):
    """The private key: POLYMARKET_PRIVATE_KEY, else the note of the Bitwarden item.

    POLYMARKET_KEY_SOURCE=env or =bitwarden reads that source alone. The item
    is BW_POLYMARKET_ITEM_ID, else the first item that a search for
    BW_POLYMARKET_ITEM_NAME finds, and it must be a secure note.

    ask_password() returns the vault's master password. It is called only when
    the vault is locked, and a vault this call unlocks is locked again before
    the call returns or raises. A vault the user had unlocked is left as it was.

    Raises KeyUnavailable with the reason. bw runs the CLI.
    """
    source = os.getenv("POLYMARKET_KEY_SOURCE", "").lower()
    if source != "bitwarden":
        key = os.getenv("POLYMARKET_PRIVATE_KEY")
        if key:
            return key
        if source == "env":
            raise KeyUnavailable("POLYMARKET_PRIVATE_KEY is not set")

    item_id, item_name = os.getenv("BW_POLYMARKET_ITEM_ID"), os.getenv("BW_POLYMARKET_ITEM_NAME")
    if not (item_id or item_name):
        raise KeyUnavailable(
            ("" if source == "bitwarden" else "POLYMARKET_PRIVATE_KEY is not set, and ")
            + "no Bitwarden item is named in BW_POLYMARKET_ITEM_ID or BW_POLYMARKET_ITEM_NAME")

    status = _vault_status(bw)
    if status == "unlocked":
        return _read_key(bw, None, item_id, item_name)
    if status == "unauthenticated":
        raise KeyUnavailable("the Bitwarden CLI is not logged in: run `bw login` once")
    if status != "locked":
        raise KeyUnavailable(f"the Bitwarden CLI reports an unknown status: {status!r}")

    session = _unlock(bw, ask_password())
    try:
        key = _read_key(bw, session, item_id, item_name)
    finally:
        _lock(bw)
    return key


def read_stdin_key(stream):
    """The private key: the first line of stream, the process's standard input.

    The stream's descriptor then reads the null device. A file that held the
    key stays readable through a descriptor still open on it, even once it is
    deleted: on Linux any process of the same user can open /proc/<pid>/fd/0.
    A stream with no descriptor is left as it is.

    Raises KeyUnavailable when the line is empty.
    """
    key = stream.readline().strip()
    _point_at_null(stream)
    if not key:
        raise KeyUnavailable("no key arrived on standard input")
    return key


def _point_at_null(stream):
    try:
        descriptor = stream.fileno()
    except OSError:
        return
    null = os.open(os.devnull, os.O_RDONLY)
    try:
        os.dup2(null, descriptor)
    finally:
        os.close(null)


def _lock(bw):
    """A process that could not lock the vault again does not go on to trade."""
    if not bw(["lock"]).ok:
        raise KeyUnavailable("the Bitwarden vault did not lock again: run `bw lock`")


def _vault_status(bw):
    return _json(bw(["status"]), "read the Bitwarden status").get("status")


def _unlock(bw, password):
    result = bw(["unlock", "--raw"], stdin=password)
    session = result.stdout.strip()
    if not result.ok or not session:
        raise KeyUnavailable(f"the Bitwarden vault did not unlock: {result.stderr.strip()}")
    return session


def _read_key(bw, session, item_id, item_name):
    if not item_id:
        found = _json(bw(["list", "items", "--search", item_name], session=session),
                      "search the Bitwarden vault")
        if not found:
            raise KeyUnavailable(f"no Bitwarden item matches '{item_name}'")
        item_id = found[0]["id"]
    item = _json(bw(["get", "item", item_id], session=session), "read the Bitwarden item")
    notes = (item.get("notes") or "").strip()
    if item.get("type") != SECURE_NOTE or not notes:
        raise KeyUnavailable("the Bitwarden item is not a secure note that holds the key")
    return notes


def _json(result, doing):
    if not result.ok:
        raise KeyUnavailable(f"could not {doing}: {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except ValueError:
        pass
    # Raised outside the handler so the decode error is not chained to it: that
    # error holds the CLI's whole output, which may hold the key.
    raise KeyUnavailable(f"could not {doing}: the CLI printed no JSON")
