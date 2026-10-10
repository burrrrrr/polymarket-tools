"""Tests for polymarket.keys: the private key from the environment or the
Bitwarden vault. The CLI is a fake that keeps the vault's state, so the tests
read whether the vault ends locked. The real bw never runs."""

import json
import os
import unittest
from unittest import mock

from polymarket import keys
from polymarket.keys import BwResult, KeyUnavailable, load_private_key, run_bw

KEY = "0x" + "c0ffee" * 10 + "abcd"
PASSWORD = "correct horse battery staple"
SESSION = "c2Vzc2lvbi10b2tlbi1mb3ItdGVzdHM="
NOTE = {"id": "item-1", "name": "Polymarket wallet", "type": 2, "notes": f"  {KEY}\n"}
LOGIN = {"id": "item-2", "name": "Polymarket login", "type": 1, "notes": "not the key"}


class FakeBw:
    """The bw CLI over one vault. Reading an item needs the vault unlocked and
    the session the unlock printed, or no session when the vault was unlocked
    before the call, as with the user's own BW_SESSION."""

    def __init__(self, status="locked", items=(NOTE,), broken=(), garbage=()):
        self.status = status
        self.items = list(items)
        self.broken, self.garbage = set(broken), set(garbage)
        self.session = None
        self.calls = []

    def __call__(self, args, session=None, stdin=None):
        self.calls.append((list(args), session, stdin))
        command = args[0]
        if command in self.broken:
            return BwResult(False, "", f"{command} failed")
        if command in self.garbage:
            return BwResult(True, f"mac failed. {KEY}", "")
        if command == "status":
            return BwResult(True, json.dumps({"status": self.status}), "")
        if command == "unlock":
            if self.status != "locked" or stdin != PASSWORD:
                return BwResult(False, "", "Invalid master password.")
            self.status, self.session = "unlocked", SESSION
            return BwResult(True, SESSION + "\n", "")
        if command == "lock":
            self.status, self.session = "locked", None
            return BwResult(True, "Your vault is locked.", "")
        if self.status != "unlocked" or session != self.session:
            return BwResult(False, "", "Vault is locked.")
        if args[:3] == ["list", "items", "--search"]:
            return BwResult(True, json.dumps([i for i in self.items if args[3] in i["name"]]), "")
        if args[:2] == ["get", "item"]:
            found = [i for i in self.items if i["id"] == args[2]]
            return BwResult(True, json.dumps(found[0]), "") if found else \
                BwResult(False, "", "Not found.")
        raise AssertionError(f"unexpected bw call: {args}")

    def commands(self):
        return [args[0] for args, _, _ in self.calls]


def never_asked():
    raise AssertionError("the password was asked for")


def chain(exc):
    """The exception and every exception chained to it."""
    while exc is not None:
        yield exc
        exc = exc.__cause__ or exc.__context__


BY_ID = {"BW_POLYMARKET_ITEM_ID": "item-1"}
BY_NAME = {"BW_POLYMARKET_ITEM_NAME": "Polymarket wallet"}


class KeysTestCase(unittest.TestCase):
    def load(self, env, bw, ask_password=lambda: PASSWORD):
        with mock.patch.dict(os.environ, env, clear=True):
            try:
                return load_private_key(ask_password, bw=bw)
            finally:
                self.environment_after = dict(os.environ)


class EnvironmentKeyTests(KeysTestCase):
    def test_the_variable_wins_and_the_vault_is_never_touched(self):
        bw = FakeBw()
        self.assertEqual(self.load({"POLYMARKET_PRIVATE_KEY": KEY, **BY_ID}, bw, never_asked), KEY)
        self.assertEqual(bw.calls, [])

    def test_source_env_never_falls_back_to_the_vault(self):
        bw = FakeBw()
        with self.assertRaises(KeyUnavailable) as raised:
            self.load({"POLYMARKET_KEY_SOURCE": "env", **BY_ID}, bw, never_asked)
        self.assertIn("POLYMARKET_PRIVATE_KEY", str(raised.exception))
        self.assertEqual(bw.calls, [])

    def test_source_bitwarden_ignores_the_variable(self):
        bw = FakeBw()
        env = {"POLYMARKET_KEY_SOURCE": "Bitwarden", "POLYMARKET_PRIVATE_KEY": "0xfromenv", **BY_ID}
        self.assertEqual(self.load(env, bw), KEY)

    def test_with_no_variable_and_no_item_configured_it_raises_before_running_bw(self):
        bw = FakeBw()
        with self.assertRaises(KeyUnavailable) as raised:
            self.load({}, bw, never_asked)
        self.assertIn("BW_POLYMARKET_ITEM_ID", str(raised.exception))
        self.assertEqual(bw.calls, [])


class VaultKeyTests(KeysTestCase):
    def test_a_locked_vault_is_unlocked_with_the_password_read_and_locked_again(self):
        bw = FakeBw()
        asked = []
        key = self.load(BY_ID, bw, lambda: asked.append(1) or PASSWORD)
        self.assertEqual(key, KEY)
        self.assertEqual(len(asked), 1)
        self.assertEqual(bw.status, "locked")
        self.assertEqual(bw.commands(), ["status", "unlock", "get", "lock"])

    def test_the_item_is_found_by_name_when_no_id_is_set(self):
        bw = FakeBw(items=(LOGIN, NOTE))
        self.assertEqual(self.load(BY_NAME, bw), KEY)
        self.assertEqual(bw.status, "locked")

    def test_the_id_wins_over_the_name_and_skips_the_search(self):
        bw = FakeBw()
        self.assertEqual(self.load({**BY_ID, "BW_POLYMARKET_ITEM_NAME": "something else"}, bw), KEY)
        self.assertNotIn("list", bw.commands())

    def test_every_failed_read_raises_and_leaves_the_vault_locked(self):
        cases = {
            "no item matches the name": (FakeBw(), {"BW_POLYMARKET_ITEM_NAME": "nothing"}),
            "no item has the id": (FakeBw(), {"BW_POLYMARKET_ITEM_ID": "item-9"}),
            "the item is not a secure note": (FakeBw(items=(LOGIN,)),
                                              {"BW_POLYMARKET_ITEM_ID": "item-2"}),
            "the note is empty": (FakeBw(items=({**NOTE, "notes": "  "},)), BY_ID),
            "the note has no notes field": (FakeBw(items=({**NOTE, "notes": None},)), BY_ID),
            "the search fails": (FakeBw(broken={"list"}), BY_NAME),
            "the read fails": (FakeBw(broken={"get"}), BY_ID),
            "the read prints no JSON": (FakeBw(garbage={"get"}), BY_ID),
            "the search prints no JSON": (FakeBw(garbage={"list"}), BY_NAME),
        }
        for name, (bw, env) in cases.items():
            with self.subTest(name):
                with self.assertRaises(KeyUnavailable) as raised:
                    self.load(env, bw)
                self.assertEqual(bw.status, "locked")
                self.assertEqual(bw.commands()[-1], "lock")
                for link in chain(raised.exception):
                    self.assertNotIn(KEY, f"{link} {vars(link)}")

    def test_an_error_that_is_not_about_the_key_still_leaves_the_vault_locked(self):
        class Flaky(FakeBw):
            def __call__(self, args, session=None, stdin=None):
                if args[0] == "get":
                    self.calls.append((list(args), session, stdin))
                    raise OSError("bw crashed")
                return super().__call__(args, session, stdin)

        bw = Flaky()
        with self.assertRaises(OSError):
            self.load(BY_ID, bw)
        self.assertEqual(bw.status, "locked")

    def test_a_wrong_password_raises_and_reads_nothing(self):
        bw = FakeBw()
        with self.assertRaises(KeyUnavailable) as raised:
            self.load(BY_ID, bw, lambda: "wrong")
        self.assertEqual(bw.status, "locked")
        self.assertNotIn("get", bw.commands())
        self.assertNotIn("wrong", str(raised.exception))

    def test_a_cli_that_is_not_logged_in_says_to_run_bw_login_and_never_asks(self):
        bw = FakeBw(status="unauthenticated")
        with self.assertRaises(KeyUnavailable) as raised:
            self.load(BY_ID, bw, never_asked)
        self.assertIn("bw login", str(raised.exception))
        self.assertEqual(bw.commands(), ["status"])

    def test_a_vault_the_user_unlocked_is_read_without_asking_and_left_unlocked(self):
        bw = FakeBw(status="unlocked")
        self.assertEqual(self.load(BY_ID, bw, never_asked), KEY)
        self.assertEqual(bw.status, "unlocked")
        self.assertNotIn("lock", bw.commands())

    def test_a_status_that_cannot_be_read_raises_without_asking(self):
        cases = {"the command fails": FakeBw(broken={"status"}),
                 "it prints no JSON": FakeBw(garbage={"status"}),
                 "the status is not one bw documents": FakeBw(status="odd")}
        for name, bw in cases.items():
            with self.subTest(name), self.assertRaises(KeyUnavailable):
                self.load(BY_ID, bw, never_asked)

    def test_a_vault_that_will_not_lock_again_raises_instead_of_returning_the_key(self):
        class StaysOpen(FakeBw):
            def __call__(self, args, session=None, stdin=None):
                if args[0] == "lock":
                    self.calls.append((list(args), session, stdin))
                    return BwResult(False, "", "lock failed")
                return super().__call__(args, session, stdin)

        with self.assertRaises(KeyUnavailable) as raised:
            self.load(BY_ID, StaysOpen())
        self.assertIn("bw lock", str(raised.exception))

    def test_the_session_and_the_password_never_enter_this_processes_environment(self):
        for name, bw, env in (("read", FakeBw(), BY_ID),
                              ("failed read", FakeBw(broken={"get"}), BY_ID)):
            with self.subTest(name):
                try:
                    self.load(env, bw)
                except KeyUnavailable:
                    pass
                self.assertIn("unlock", bw.commands())
                self.assertEqual(self.environment_after, env)

    def test_the_password_reaches_only_the_unlock_and_only_on_standard_input(self):
        bw = FakeBw()
        self.load(BY_ID, bw)
        for args, session, stdin in bw.calls:
            self.assertNotIn(PASSWORD, args)
            self.assertNotEqual(session, PASSWORD)
            self.assertEqual(stdin, PASSWORD if args[0] == "unlock" else None)


class RunBwTests(unittest.TestCase):
    def run_bw(self, *args, **kwargs):
        done = mock.Mock(returncode=0, stdout="out", stderr="")
        with mock.patch.dict(os.environ, {"PATH": "somewhere"}, clear=True), \
                mock.patch.object(keys.shutil, "which", return_value="/opt/bw") as which, \
                mock.patch.object(keys.subprocess, "run", return_value=done) as run:
            result = run_bw(*args, **kwargs)
            self.environment_after = dict(os.environ)
        which.assert_called_once_with("bw")
        return result, run.call_args

    def test_the_session_goes_to_the_child_environment_and_not_to_this_one(self):
        result, call = self.run_bw(["get", "item", "item-1"], session=SESSION)
        self.assertEqual(result, BwResult(True, "out", ""))
        self.assertEqual(call.args[0], ["/opt/bw", "get", "item", "item-1"])
        self.assertEqual(call.kwargs["env"], {"PATH": "somewhere", "BW_SESSION": SESSION})
        self.assertEqual(self.environment_after, {"PATH": "somewhere"})

    def test_without_a_session_the_child_inherits_the_environment(self):
        _, call = self.run_bw(["status"])
        self.assertIsNone(call.kwargs["env"])

    def test_the_password_is_written_to_standard_input_and_is_not_an_argument(self):
        _, call = self.run_bw(["unlock", "--raw"], stdin=PASSWORD)
        self.assertEqual(call.kwargs["input"], PASSWORD)
        self.assertNotIn(PASSWORD, call.args[0])

    def test_a_call_with_no_input_cannot_read_this_processes_terminal(self):
        _, call = self.run_bw(["status"])
        self.assertIs(call.kwargs["stdin"], keys.subprocess.DEVNULL)

    def test_a_failed_command_is_not_ok(self):
        done = mock.Mock(returncode=1, stdout="", stderr="Vault is locked.")
        with mock.patch.object(keys.shutil, "which", return_value="/opt/bw"), \
                mock.patch.object(keys.subprocess, "run", return_value=done):
            self.assertEqual(run_bw(["get", "item", "x"]), BwResult(False, "", "Vault is locked."))

    def test_a_machine_without_the_cli_raises_key_unavailable(self):
        with mock.patch.object(keys.shutil, "which", return_value=None), \
                self.assertRaises(KeyUnavailable):
            run_bw(["status"])


if __name__ == "__main__":
    unittest.main()
