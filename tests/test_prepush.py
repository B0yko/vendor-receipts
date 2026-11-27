"""Tests for tools/prepush.py (pre-push guard) and tools/publish_check.py (full-history check and visibility flip).

Offline: temp git repos, a local bare remote and a fake `gh` on PATH. Every product, vendor and domain here is
invented (.test is a reserved TLD); token and attribution strings are assembled at runtime so this file holds none.
The scripts run in-process (their main(), output captured) for speed; the push test goes through the real hook.
"""
import contextlib, csv, io, os, shutil, stat, subprocess, sys, tempfile, unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import prepush, publish_check  # noqa: E402
G = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
     "-c", "tag.gpgsign=false"]
COLS = ["id", "url", "product", "vendor", "overall", "green_flags", "yellow_flags", "red_flags", "reasoning",
        "reviewer_notes_original"]
SHARED = "Category twelve covers data residency and subprocessors as defined in the shared review rubric"
ROW1_TEXT = "Pricing page lists every storage tier openly and the changelog shows weekly releases since spring"
ROW2_TEXT = "Support answered within two hours on a Sunday and the status history goes back three years"
LABELS = [
    ["1", "https://www.acmewidgets.test/", "Acme Widgets Pro", "Acmewidget GmbH", "Green", SHARED[:20], "", "",
     ROW1_TEXT, "ok"],
    ["2", "https://app.blinkoria.test/start", "Blinkoria Desk", "Blinkoria Labs Inc", "Yellow", "", "", "",
     ROW2_TEXT, ""],
    ["3", "https://zentrovia.test", "Zentrovia", "Zentrovia Systems", "Red", SHARED, "", "", "", ""],
    ["4", "https://quorbex.test/", "QBX", "Velmora Analytics GmbH", "Red", SHARED, "", "", "", ""],
    ["5", "tiny.test", "Tiny", "Tny Co", "Yellow", "", "", "", "", ""],
]
BLIND = [
    ["b1", "https://glimmerforge.test", "Glimmer Forge", "Glimmerforge Ltd"],
    ["b2", "https://blinkoria.test/desk", "Blinkoria Desk", "Blinkoria Labs Inc"],
    ["b3", "https://acmewidget-mail.test", "Acmewidget Mail", "Acmewidget GmbH"],
]
FULL_TERMS = ("acmewidgets", "acme widgets pro", "zentrovia", "velmora", "glimmerforge", "blinkoria", "pricing page")


def table(header, rows):
    s = io.StringIO()
    csv.writer(s).writerows([header] + rows)
    return s.getvalue()


class GuardRepo(unittest.TestCase):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k != "LABELS"}

    @classmethod
    def setUpClass(cls):
        """One base repo per class (data/, tools, hooks, one clean commit); each test works on a copy of it."""
        cls.base = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.base, True)
        self = cls.__new__(cls)
        r = self.repo = os.path.join(cls.base, "repo")
        os.makedirs(os.path.join(r, "tools"))
        for f in ("prepush.py", "publish_check.py", "eval.py"):
            shutil.copy(os.path.join(ROOT, "tools", f), os.path.join(r, "tools", f))
        shutil.copytree(os.path.join(ROOT, "hooks"), os.path.join(r, "hooks"))
        self.put("data/hackathon_40_solutions_commented.csv", table(COLS, LABELS))
        self.put("data/blind.csv", table(["id", "url", "product", "vendor"], BLIND))
        self.put(".gitignore", "/data\n/runs\n/.cache\n")
        self.put("README.md", "# demo\nUses web.archive.org and example.com.\n")
        self.git("init", "-q", "--template=", "-b", "main")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "kickoff")

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.repo = os.path.join(self.d, "repo")
        shutil.copytree(os.path.join(self.base, "repo"), self.repo, symlinks=True)

    def put(self, rel, text):
        p = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    def git(self, *a):
        return subprocess.run(G + list(a), cwd=self.repo, capture_output=True, text=True, env=self.env)

    def commit(self, msg, sha=False):
        self.git("add", "-A")
        p = self.git("commit", "-q", "--allow-empty", "-m", msg)
        self.assertEqual(p.returncode, 0, p.stderr)
        return self.git("rev-parse", "HEAD").stdout.strip() if sha else None

    def run_main(self, main, args, env):
        out, old = io.StringIO(), os.getcwd()
        try:
            os.chdir(self.repo)
            with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(out):
                rc = main(list(args))
        finally:
            os.chdir(old)
        return rc, out.getvalue()

    def prepush(self, *args):
        return self.run_main(prepush.main, args, self.env)

    def publish(self, *args, env=None):
        return self.run_main(publish_check.main, args, env or self.env)

    def no_terms(self, out):
        for t in FULL_TERMS:
            self.assertNotIn(t, out.lower())


class TestRules(GuardRepo):
    def test_clean_history_passes(self):
        rc, err = self.prepush()
        self.assertEqual(rc, 0, err)
        self.assertIn("checked against", err)
        self.assertIn(": 0 findings", err)
        rc, out = self.publish()
        self.assertEqual(rc, 0, out)
        self.assertIn("RESULT PASS", out)
        for kind in ("private path", "labelled domain", "labelled name", "labelled text", "blind domain", "token",
                     "attribution"):
            self.assertIn(f"PASS {kind}: 0 findings", out)
        self.assertIn("5 labelled rows", out)

    def test_labelled_domain_name_and_text_fail_masked(self):
        self.put("app/agent.py", "BASE = 'https://docs.acmewidgets.test/pricing'\n")
        self.put("app/prompts.py", "# compare with ACME   widgets PRO\n")
        self.put("notes.md", "PRICING page lists every storage tier, openly; and the changelog shows it\n")
        self.commit("add agent")
        rc, err = self.prepush()
        self.assertEqual(rc, 1)
        self.assertIn("app/agent.py:ac", err)
        self.assertIn("(labelled domain)", err)
        self.assertIn("app/prompts.py:ac", err)
        self.assertIn("(labelled name)", err)
        self.assertIn("notes.md:pr", err)
        self.assertIn("(labelled text)", err)
        self.assertIn("push blocked", err)
        self.no_terms(err)
        rc, out = self.publish()
        self.assertEqual(rc, 1)
        self.assertIn("FAIL labelled domain: 1 findings", out)
        self.assertIn("FAIL labelled text: 1 findings", out)
        self.assertIn("RESULT FAIL", out)
        self.no_terms(out)

    def test_whole_word_min_length_suffix_and_allow_list(self):
        self.put("a.md", "notacmewidgets.test, Acme Widgets Professional, Tiny, Zentrovias\n" + SHARED + "\n")
        self.commit("near misses")
        rc, err = self.prepush()
        self.assertEqual(rc, 0, err)
        self.put("b.md", "built by Velmora Analytics, see tiny.test\n")
        self.commit("vendor without its legal suffix, short product on its domain")
        rc, err = self.prepush()
        self.assertEqual(rc, 1)
        self.assertIn("b.md:ve", err)
        self.assertIn("b.md:ti", err)
        self.put("c.md", "zentrovia is a word here\n")
        self.put("tests/leak_allow.txt", "zentrovia # generic in this test\n")
        self.commit("allowed word")
        rc, err = self.prepush()
        self.assertNotIn("c.md", err)

    def test_blind_domains_only_in_zone(self):
        self.put("traces/b1.md", "Checked https://glimmerforge.test/about and Glimmer Forge pricing.\n")
        self.put("stackgini-verdicts.csv", "id,url,overall\nb1,https://glimmerforge.test,Green\n")
        self.commit("verdicts")
        rc, out = self.publish()
        self.assertEqual(rc, 0, out)
        self.put("app/x.py", "SEEN = 'glimmerforge.test'\n")
        self.put("traces/notes/deep.md", "www.glimmerforge.test\n")
        self.commit("leak")
        rc, out = self.publish()
        self.assertEqual(rc, 1)
        self.assertIn("FAIL blind domain: 1 findings", out)
        self.assertIn("app/x.py:gl", out)
        self.no_terms(out)
        self.git("rm", "-q", "app/x.py")
        self.commit("drop it: was GlimmerForge.test")
        rc, out = self.publish()
        self.assertIn("FAIL blind domain: 2 findings", out)
        self.assertIn(":(message):gl", out)

    def test_labelled_rules_inside_zone(self):
        self.put("traces/b1.md", "Similar to acmewidgets.test.\n")
        self.commit("bad trace")
        rc, out = self.publish()
        self.assertEqual(rc, 1)
        self.assertIn("traces/b1.md:ac", out)
        self.git("rm", "-q", "traces/b1.md")
        self.commit("fix trace")
        # b2 is also a labelled product (same registrable domain): its domain, names and text are fine in ZONE.
        self.put("traces/b2.md", "app.blinkoria.test, Blinkoria Desk by Blinkoria Labs Inc. " + ROW2_TEXT + "\n")
        self.put("stackgini-verdicts.csv", "id,url\nb2,https://blinkoria.test/desk\n")
        # b3 shares a labelled vendor: the vendor name is fine in ZONE, the other labelled product is not. Row text
        # is checked outside ZONE only (a trace can match it only by coincidence).
        self.put("traces/b3.md", "Acmewidget GmbH makes Acmewidget Mail. " + ROW1_TEXT + "\n")
        self.commit("traces")
        rc, out = self.publish()
        self.assertIn("FAIL labelled domain: 1 findings", out)
        self.assertNotIn("traces/b2.md", out)
        self.assertNotIn("traces/b3.md", out)
        self.assertNotIn("stackgini-verdicts.csv", out)
        self.put("traces/b3.md", "Acmewidget GmbH makes Acmewidget Mail and Acme Widgets Pro.\n")
        self.put("README.md", "see blinkoria.test\n")
        self.commit("more")
        rc, out = self.publish()
        self.assertIn("traces/b3.md:ac", out)
        self.assertIn("README.md:bl", out)
        self.no_terms(out)

    def test_history_and_other_refs(self):
        self.put("old.md", "zentrovia.test\n")
        bad = self.commit("add old", sha=True)
        self.git("rm", "-q", "old.md")
        self.commit("remove old")
        rc, err = self.prepush()
        self.assertEqual(rc, 1)
        self.assertIn(bad[:8] + ":old.md:", err)
        rc, out = self.publish()
        self.assertIn(bad[:8] + ":old.md:", out)

    def test_branch_only_publish_check_sees_it(self):
        self.git("checkout", "-q", "-b", "side")
        self.put("side.md", "Zentrovia Systems\n")
        self.commit("side work")
        self.git("checkout", "-q", "main")
        rc, err = self.prepush()
        self.assertEqual(rc, 0, err)
        rc, out = self.publish()
        self.assertEqual(rc, 1)
        self.assertIn("side.md:ze", out)

    def test_tokens(self):
        a = "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
        toks = ["sk" + "-proj-" + a, "tvly" + "-dev-" + a, "gh" + "p_" + a, "github" + "_pat_" + a,
                "xo" + "xb-1234-" + a, "AK" + "IAABCDEFGH12345678", "-----" + "BEGIN OPENSSH PRIVATE KEY-----"]
        self.put("conf.txt", "\n".join(toks) + "\n")
        self.put("ok.txt", "task-abcdefghijklmnopqrstuvwxyz\nsk-learn-compatible-estimator-wrapper-x\n")
        self.commit("config")
        rc, out = self.publish()
        self.assertEqual(rc, 1)
        self.assertIn("FAIL token: 7 findings", out)
        self.assertNotIn("ok.txt", out)
        for t in toks:
            self.assertNotIn(t, out)
        rc, err = self.prepush()
        self.assertEqual(rc, 1)

    def test_attribution_in_messages_files_and_tags(self):
        phrase = "Co-" + "Authored-By: Some Agent <a@b.invalid>"
        self.commit("feat\n\n" + phrase)
        rc, err = self.prepush()
        self.assertEqual(rc, 1)
        self.assertIn(":(message):co", err)
        self.assertIn("(attribution)", err)
        self.git("reset", "-q", "--hard", "HEAD~1")  # unreachable now: no ref, so out of --all
        self.put("NOTES.md", "Made with care. " + "Generated" + " with a coding agent\n")
        with open(os.path.join(self.repo, "tools", "prepush.py"), "a") as f:
            f.write("# the guard may name " + "claude" + " code\n")
        self.commit("notes")
        self.git("tag", "-a", "v1", "-m", "release " + "anthropic" + ".com/claude")
        rc, out = self.publish()
        self.assertEqual(rc, 1)
        self.assertIn("FAIL attribution: 2 findings", out)
        self.assertIn("NOTES.md:ge", out)
        self.assertIn("(tag v1):an", out)
        self.assertNotIn("tools/prepush.py", out)

    def test_private_paths_and_path_names(self):
        self.git("add", "-f", "data/blind.csv")
        self.put(".env", "X=1\n")
        self.put("tests/fixtures/zentrovia.json", "{}\n")
        self.commit("oops")
        rc, err = self.prepush()
        self.assertEqual(rc, 1)
        self.assertIn(":data/blind.csv:data/blind.csv (private path)", err)
        self.assertIn(":.env:.env (private path)", err)
        self.assertIn("(labelled name in path)", err)
        self.no_terms(err)
        rc, out = self.publish()
        self.assertIn("FAIL private path: 2 findings", out)
        self.no_terms(out)

    def test_fail_closed_without_labels(self):
        os.remove(os.path.join(self.repo, "data", "hackathon_40_solutions_commented.csv"))
        rc, err = self.prepush()
        self.assertEqual(rc, 0, err)
        self.assertIn("warning: no labelled rows", err)
        rc, out = self.publish()
        self.assertEqual(rc, 1)
        self.assertIn("FAIL setup: no labelled rows", out)


class TestPushAndFlip(GuardRepo):
    def setUp(self):
        super().setUp()
        self.bare = os.path.join(self.d, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", self.bare], check=True, env=self.env)
        self.git("remote", "add", "origin", self.bare)
        self.git("config", "core.hooksPath", "hooks")
        bin_ = os.path.join(self.d, "bin")
        os.makedirs(bin_)
        self.log = os.path.join(self.d, "gh.log")
        gh = os.path.join(bin_, "gh")
        with open(gh, "w") as f:
            f.write(f"#!/bin/sh\necho \"$*\" >> {self.log}\n"
                    "[ \"$2\" = view ] && echo '{\"visibility\":\"PUBLIC\"}'\nexit 0\n")
        os.chmod(gh, os.stat(gh).st_mode | stat.S_IEXEC)
        self.fenv = dict(self.env, PATH=bin_ + os.pathsep + self.env.get("PATH", ""))

    def gh_calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return f.read().splitlines()

    def test_hook_blocks_push(self):
        self.put("app.py", "TARGET = 'https://quorbex.test'\n")
        self.commit("add app")
        p = self.git("push", "-q", "origin", "main")  # through hooks/pre-push, a real subprocess
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("push blocked", p.stderr)
        self.assertIn("(labelled domain)", p.stderr)
        self.assertNotIn("quorbex", p.stderr)
        self.assertEqual(self.git("ls-remote", "origin").stdout, "")

    def test_hook_checks_only_the_pushed_range(self):
        self.put("app.py", "TARGET = 'https://quorbex.test'\n")
        bad = self.commit("add app", sha=True)
        self.git("rm", "-q", "app.py")
        good = self.commit("drop app", sha=True)

        def hook(remote_sha):
            line = f"refs/heads/main {good} refs/heads/main {remote_sha}\n"
            with mock.patch("sys.stdin", io.StringIO(line)):
                return self.prepush("--hook")
        rc, err = hook(bad)  # the remote already has the bad commit: only the fix is new
        self.assertEqual(rc, 0, err)
        self.assertIn("(1 commits", err)
        rc, err = hook("0" * 40)  # a new remote branch: every commit not on a remote
        self.assertEqual(rc, 1)
        self.assertIn(bad[:8] + ":app.py:qu", err)

    def test_flip_only_on_pass(self):
        self.put("app.py", "x = 'zentrovia.test'\n")
        self.commit("bad")
        rc, out = self.publish("--flip", env=self.fenv)
        self.assertEqual(rc, 1)
        self.assertIn("not flipped", out)
        self.assertEqual(self.gh_calls(), [])
        self.git("reset", "-q", "--hard", "HEAD~1")
        rc, out = self.publish("--flip", env=self.fenv)
        self.assertEqual(rc, 0, out)
        self.assertIn("RESULT PASS", out)
        self.assertIn('{"visibility":"PUBLIC"}', out)
        self.assertEqual(self.gh_calls(), [
            "repo edit B0yko/vendor-receipts --visibility public --accept-visibility-change-consequences",
            "repo view B0yko/vendor-receipts --json visibility"])

    def test_flip_refuses_when_fetch_fails(self):
        self.git("remote", "set-url", "origin", os.path.join(self.d, "missing.git"))
        rc, out = self.publish("--flip", env=self.fenv)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL fetch", out)
        self.assertEqual(self.gh_calls(), [])


if __name__ == "__main__":
    unittest.main()
