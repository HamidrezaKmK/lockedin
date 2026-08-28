"""The bubble's 🤖 one-command setup link: minting, serving, and redeeming a ticket.

A ticket is a bearer credential with a very short life, so the properties that matter are
negative ones: it cannot be minted without a session, it cannot be spent twice, it expires, and
serving its script never hands the token to whoever fetched the URL.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from fastapi.testclient import TestClient

from lockedin import server, setup_tickets


@contextmanager
def client():
    with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"LOCKEDIN_HOME": directory}):
        setup_tickets.clear()
        with TestClient(server.build_app(), base_url="https://testserver") as api:
            assert api.post("/api/signup",
                            json={"username": "robot", "password": "testpass"}).status_code == 200
            slug = api.post("/api/bubbles", json={"name": "Robot Bubble"}).json()["slug"]
            yield api, slug
        setup_tickets.clear()


class SetupLinkMinting(unittest.TestCase):
    def test_a_link_carries_this_server_and_bubble_for_both_shells(self):
        with client() as (api, slug):
            body = api.post(f"/api/bubbles/{slug}/setup-link").json()
            self.assertTrue(body["ticket"])
            # Built from the request's own origin, so a tunnel or localhost yields a working link
            # rather than a hardcoded production domain.
            self.assertIn(f"https://testserver/setup/{body['ticket']}.sh", body["unix"])
            self.assertIn(f"https://testserver/setup/{body['ticket']}.ps1", body["powershell"])
            self.assertTrue(body["unix"].startswith("curl "))
            self.assertIn("irm ", body["powershell"])

    def test_minting_requires_a_session_and_a_real_bubble(self):
        with client() as (api, slug):
            self.assertEqual(api.post("/api/bubbles/nope/setup-link").status_code, 404)
            api.post("/api/logout")
            self.assertEqual(api.post(f"/api/bubbles/{slug}/setup-link").status_code, 401)


class SetupScriptServing(unittest.TestCase):
    def test_the_unix_script_installs_then_connects_with_a_terminal_to_ask_on(self):
        with client() as (api, slug):
            ticket = api.post(f"/api/bubbles/{slug}/setup-link").json()["ticket"]
            api.post("/api/logout")          # the ticket is the credential, not the session
            response = api.get(f"/setup/{ticket}.sh")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            script = response.text
            self.assertIn("install.sh | bash", script)
            self.assertIn("https://testserver/setup/scientist_cli.py", script)
            self.assertIn("lockedin-scientist connect", script)
            self.assertIn(f"--ticket '{ticket}'", script)
            self.assertIn(f"--bubble '{slug}'", script)
            # The script itself arrives on stdin from curl, so without this redirect the folder
            # prompt would swallow the rest of the script instead of reaching a human.
            self.assertIn("< /dev/tty", script)
            # Serving a script must never leak the token; only redeeming hands that over.
            self.assertNotIn("li_sc_", script)

    def test_the_powershell_script_installs_then_connects(self):
        with client() as (api, slug):
            ticket = api.post(f"/api/bubbles/{slug}/setup-link").json()["ticket"]
            script = api.get(f"/setup/{ticket}.ps1").text
            self.assertIn("install.ps1 | iex", script)
            self.assertIn("https://testserver/setup/scientist_cli.py", script)
            self.assertIn("lockedin-scientist connect", script)
            self.assertIn(f"--ticket '{ticket}'", script)
            self.assertNotIn("li_sc_", script)

    def test_an_unknown_ticket_still_answers_with_a_script(self):
        """A JSON 404 piped into a shell is a parse error; an expired link must explain itself."""
        with client() as (api, _slug):
            for suffix, marker in ((".sh", "exit 1"), (".ps1", "Write-Error")):
                response = api.get(f"/setup/does-not-exist{suffix}")
                self.assertEqual(response.status_code, 200)
                self.assertIn("expired", response.text)
            self.assertIn(marker, response.text)

    def test_server_exposes_the_exact_matching_client_without_a_session(self):
        with client() as (api, _slug):
            api.post("/api/logout")
            response = api.get("/setup/scientist_cli.py")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertIn(f'SKILL_VERSION = {server.scientist_cli.SKILL_VERSION}', response.text)


class SetupScriptWithoutATerminal(unittest.TestCase):
    """The same line has to work for a person and for an agent.

    A person pastes it into their own terminal, where the folder prompt reaches them only through
    /dev/tty (the script itself is on stdin, from curl). An agent pastes it into a shell with no
    controlling terminal, where opening /dev/tty is a hard error — and that is exactly the case
    where the client is least likely to be installed already, so failing there would break the one
    path that could have set it up.
    """

    @staticmethod
    def _script():
        return setup_tickets.unix_script("https://x.test", "TKT", "WS", "bub")

    def test_the_unix_script_offers_both_a_prompt_and_a_fallback(self):
        script = self._script()
        self.assertIn("< /dev/tty", script)
        self.assertIn('--project "$PWD"', script)

    def test_the_powershell_script_offers_both(self):
        script = setup_tickets.powershell_script("https://x.test", "TKT", "WS", "bub")
        self.assertIn("IsInputRedirected", script)
        self.assertIn('--project "$PWD"', script)

    def test_a_shell_with_no_terminal_actually_takes_the_fallback(self):
        """Exercise the branch rather than the string: this is shell logic, not copy."""
        import subprocess
        script = self._script()
        # Keep the real branch; stub the install and the command it would exec.
        script = script.replace(
            "curl -fsSL https://raw.githubusercontent.com/HamidrezaKmK/lockedin/main/install.sh | bash",
            "true")
        script = script.replace(
            "curl -fsSL 'https://x.test/setup/scientist_cli.py' -o \"$client_tmp\"",
            "true")
        script = script.replace("exec lockedin-scientist connect", "echo CHOSE:")
        run = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, cwd="/tmp")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("CHOSE:", run.stdout)
        self.assertIn("--project /tmp", run.stdout.replace('"', ""))
        self.assertIn("No terminal to ask on", run.stdout)
        # And it must be quiet about it: the failed open has to be silenced *before* it happens,
        # or bash prints "/dev/tty: No such device" into the agent's log for no reason.
        self.assertNotIn("No such device", run.stderr)
        self.assertEqual(run.stderr.strip(), "")


class SetupTicketRedemption(unittest.TestCase):
    def test_redeeming_returns_the_authorization_exactly_once(self):
        with client() as (api, slug):
            ticket = api.post(f"/api/bubbles/{slug}/setup-link").json()["ticket"]
            api.post("/api/logout")
            headers = {"X-LockedIn-Scientist-Version": server.SCIENTIST_CLIENT_VERSION}
            first = api.get(f"/api/scientist/v2/setup/{ticket}", headers=headers)
            self.assertEqual(first.status_code, 200)
            granted = first.json()
            self.assertEqual(granted["user"], "robot")
            self.assertEqual(granted["bubble"], slug)
            self.assertTrue(granted["token"].startswith("li_sc_"))
            self.assertTrue(granted["workspace_id"])
            # The token works: the client uses it immediately for the manifest probe.
            manifest = api.get(f"/api/scientist/v2/bubbles/{slug}/manifest", headers={
                **headers, "Authorization": f"Bearer {granted['token']}",
                "X-LockedIn-Workspace": granted["workspace_id"]})
            self.assertEqual(manifest.status_code, 200)
            self.assertEqual(api.get(f"/api/scientist/v2/setup/{ticket}", headers=headers).status_code, 404)

    def test_an_outdated_client_cannot_redeem(self):
        with client() as (api, slug):
            ticket = api.post(f"/api/bubbles/{slug}/setup-link").json()["ticket"]
            response = api.get(f"/api/scientist/v2/setup/{ticket}",
                               headers={"X-LockedIn-Scientist-Version": "2020.01.01.1"})
            self.assertEqual(response.status_code, 426)

    def test_a_ticket_expires(self):
        with client() as (api, slug):
            ticket = api.post(f"/api/bubbles/{slug}/setup-link").json()["ticket"]
            with patch.object(setup_tickets, "TICKET_TTL", -1):
                self.assertIsNone(setup_tickets.peek(ticket))
                self.assertIsNone(setup_tickets.redeem(ticket))

    def test_tickets_are_never_written_to_disk(self):
        """Like presence and sessions: a restart must invalidate outstanding credentials."""
        with client() as (api, slug):
            ticket = api.post(f"/api/bubbles/{slug}/setup-link").json()["ticket"]
            home = os.environ["LOCKEDIN_HOME"]
            found = [os.path.join(root, name)
                     for root, _dirs, names in os.walk(home) for name in names
                     if ticket in open(os.path.join(root, name), "rb").read().decode("utf-8", "ignore")]
            self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
