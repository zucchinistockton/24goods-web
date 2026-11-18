"""Synthetic tests of scripts/pull-prod-backups.sh (issue #54 F2); no SSH, no real host or data.

A fake ``sshpass`` runs the remote command locally against a temporary "remote" directory and copies
files for ``scp``; ``FAIL_SCP`` makes the copy of a named member fail, ``CORRUPT`` flips a byte."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAMP = "20261118T051500Z"
FAKE = r"""#!/usr/bin/env bash
shift 2  # -f <password file>
tool=$1; shift
if [[ $tool == ssh ]]; then
  cmd=${@: -1}; printf 'ssh %s\n' "$cmd" >> "$FAKE_LOG"; exec bash -c "$cmd"
fi
src=${@: -2:1}; dst=${@: -1}; path=${src#*:}
printf 'scp %s\n' "$(basename "$path")" >> "$FAKE_LOG"
if [[ -n ${FAIL_SCP:-} && $path == *"$FAIL_SCP"* ]]; then head -c 3 "$path" > "$dst"; exit 23; fi
cp "$path" "$dst"
if [[ -n ${CORRUPT:-} && $path == *"$CORRUPT"* ]]; then printf 'x' >> "$dst"; fi
"""


class PullTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "bin").mkdir()
        fake = root / "bin/sshpass"
        fake.write_text(FAKE)
        fake.chmod(0o700)
        self.remote, self.dest, self.secrets = root / "remote", root / "dest", root / "secrets"
        for d in (self.remote, self.dest, self.secrets):
            d.mkdir()
        (self.secrets / "c4_ssh_pw").write_text("synthetic\n")
        for name in (f"goods24-{STAMP}.dump", f"uploads-{STAMP}.tgz", f"config-{STAMP}.tgz"):
            (self.remote / name).write_bytes(os.urandom(256))
        self.log = root / "log"
        self.env = {
            **os.environ,
            "PATH": f"{root / 'bin'}:{os.environ['PATH']}",
            "PROD_REMOTE_DIR": str(self.remote),
            "PROD_LOCAL_BACKUPS": str(self.dest),
            "PROD_SECRETS_DIR": str(self.secrets),
            "FAKE_LOG": str(self.log),
        }

    def run_pull(self, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(ROOT / "scripts/pull-prod-backups.sh")],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            timeout=20,
        )

    def test_success_publishes_a_verified_set_and_the_next_run_skips_it(self) -> None:
        r = self.run_pull()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = self.dest / f"nightly-{STAMP}"
        self.assertTrue((out / "COMPLETE").exists() and (out / "SHA256SUMS").exists())
        self.assertEqual(len(list(out.glob("*-" + STAMP + ".*"))), 3)
        self.assertEqual(list(self.dest.glob(".partial-*")), [])
        r = self.run_pull()
        self.assertIn("ok already have", r.stdout)

    def test_an_interrupted_copy_is_never_complete_and_the_retry_repairs_it(self) -> None:
        r = self.run_pull(FAIL_SCP="uploads")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.dest / f"nightly-{STAMP}").exists())
        r = self.run_pull()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ok pulled", r.stdout)
        out = self.dest / f"nightly-{STAMP}"
        self.assertEqual(
            (out / f"uploads-{STAMP}.tgz").read_bytes(),
            (self.remote / f"uploads-{STAMP}.tgz").read_bytes(),
        )

    def test_a_corrupt_member_fails_and_publishes_nothing(self) -> None:
        r = self.run_pull(CORRUPT="config")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("checksum mismatch", r.stdout)
        self.assertFalse((self.dest / f"nightly-{STAMP}").exists())

    def test_an_old_unverified_copy_is_replaced_not_trusted(self) -> None:
        old = self.dest / f"nightly-{STAMP}"
        old.mkdir()
        (old / f"goods24-{STAMP}.dump").write_bytes(b"truncated")
        r = self.run_pull()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((old / "COMPLETE").exists())
        self.assertEqual(
            (old / f"goods24-{STAMP}.dump").read_bytes(),
            (self.remote / f"goods24-{STAMP}.dump").read_bytes(),
        )

    def test_retention_counts_verified_sets_only(self) -> None:
        for i in range(3):
            d = self.dest / f"nightly-2026010{i}T000000Z"
            d.mkdir()
            (d / "COMPLETE").write_text("x")
        unverified = self.dest / "nightly-20250101T000000Z"
        unverified.mkdir()
        r = self.run_pull(KEEP_SETS="2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        left = sorted(p.name for p in self.dest.glob("nightly-*"))
        self.assertEqual(
            left, ["nightly-20250101T000000Z", "nightly-20260102T000000Z", f"nightly-{STAMP}"]
        )


if __name__ == "__main__":
    unittest.main()
