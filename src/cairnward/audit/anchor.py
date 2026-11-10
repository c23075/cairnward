"""External audit anchoring (spec §5.2, D49, FR-AUD-4).

Every hour (configurable) and on demand, Cairnward signs the audit log's latest
checkpoint (sequence number + hash; no content) and commits it to a private git repo
the user owns, pushing with a deploy key that can write to that repo only.
`cairnward audit verify --anchors` checks the local log against what the repo holds.

Keys are OpenSSH Ed25519 keys made with ssh-keygen (no extra Python dependency):
`deploy_key` pushes, `sign_key` signs checkpoints (`ssh-keygen -Y sign`, namespace
"cairnward-anchor"). The repo holds, per installation, `signers` (the public signing
key) and `checkpoints.jsonl`.

Since 2026-11-10 each checkpoint (`"v": 2`) also signs `root`, the RFC 9162 Merkle root over
records 1..seq (merkle.py). Verifying then also recomputes each root from the log and checks that
every checkpoint's tree extends the one before (a consistency proof), and `cairnward audit prove`
can show one record is in an anchored checkpoint without handing over the rest of the log.
Version 1 checkpoints (hash only) still verify as before.

Honest limit: someone in full control of this machine can alter records newer than the
last anchor, or stop anchoring; the gap shows as missing or late anchors.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cairnward.audit import merkle
from cairnward.audit.log import AuditLog
from cairnward.config.models import AnchorConfig
from cairnward.config.paths import Paths

NAMESPACE = "cairnward-anchor"
IDENTITY = "cairnward"


class AnchorError(Exception):
    pass


def _tool(name: str) -> str:
    exe = shutil.which(name)
    if exe is None:
        raise AnchorError(f"{name} not found (install git and openssh-client)")
    return exe


@dataclass
class AnchorResult:
    ok: bool
    anchors: int = 0
    last_seq: int | None = None
    last_ts: str | None = None
    unanchored: int = 0
    timestamped: int = 0  # checkpoints with a verified RFC 3161 timestamp
    last_tsa_time: str | None = None
    merkle_roots: int = 0  # checkpoints whose Merkle root matched the log
    problems: list[str] = field(default_factory=list)


class Anchorer:
    def __init__(self, paths: Paths, cfg: AnchorConfig):
        self.cfg = cfg
        self.dir = paths.data_dir / "anchor"
        self.repo_dir = self.dir / "repo"
        self.deploy_key = self.dir / "deploy_key"
        self.sign_key = self.dir / "sign_key"
        self.install_file = self.dir / "install_id"

    # ------------------------------------------------------------------ setup

    @property
    def install_id(self) -> str:
        if not self.install_file.exists():
            raise AnchorError("anchoring isn't set up: cairnward audit anchor --setup")
        return self.install_file.read_text().strip()

    def setup(self) -> dict[str, str]:
        """Create the keys (once); returns the public keys to register."""
        keygen = _tool("ssh-keygen")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.dir.chmod(0o700)
        for key, comment in ((self.deploy_key, "cairnward-anchor-deploy"),
                             (self.sign_key, "cairnward-anchor-sign")):  # fmt: skip
            if not key.exists():
                subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-C", comment,
                                "-f", str(key)], check=True)  # fmt: skip
        if not self.install_file.exists():
            self.install_file.write_text(secrets.token_hex(8) + "\n")
        return {
            "deploy": self.deploy_key.with_suffix(".pub").read_text().strip(),
            "sign": self.sign_key.with_suffix(".pub").read_text().strip(),
            "install": self.install_id,
        }

    # ------------------------------------------------------------------ git

    def _env(self) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
            "GIT_AUTHOR_NAME": "cairnward", "GIT_AUTHOR_EMAIL": "cairnward@localhost",
            "GIT_COMMITTER_NAME": "cairnward", "GIT_COMMITTER_EMAIL": "cairnward@localhost",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }  # fmt: skip
        if self.deploy_key.exists():
            env["GIT_SSH_COMMAND"] = (
                f"ssh -i {self.deploy_key} -o IdentitiesOnly=yes "
                "-o StrictHostKeyChecking=accept-new -o BatchMode=yes"
            )
        return env

    def _git(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        res = subprocess.run([_tool("git"), *args], cwd=cwd or self.repo_dir, env=self._env(),
                             capture_output=True, text=True, timeout=120)  # fmt: skip
        if check and res.returncode != 0:
            raise AnchorError(f"git {args[0]} failed: {res.stderr.strip()[:300]}")
        return res.stdout

    def _sync(self) -> None:
        """A local clone of the anchors repo, up to date with the remote."""
        if not self.cfg.repo:
            raise AnchorError("no anchors repo: set [anchor] repo in cairnward.toml")
        branch = self.cfg.branch
        if not (self.repo_dir / ".git").exists():
            self.repo_dir.mkdir(parents=True, exist_ok=True)
            self._git("init", "--quiet", "-b", branch)
            self._git("remote", "add", "origin", self.cfg.repo)
        else:
            self._git("remote", "set-url", "origin", self.cfg.repo)
        fetched = subprocess.run(
            [_tool("git"), "fetch", "--quiet", "origin", branch], cwd=self.repo_dir,
            env=self._env(), capture_output=True, text=True, timeout=120,
        )  # fmt: skip
        if fetched.returncode == 0:
            self._git("checkout", "--quiet", "-B", branch, f"origin/{branch}")
        elif "couldn't find remote ref" not in fetched.stderr.lower():
            raise AnchorError(f"git fetch failed: {fetched.stderr.strip()[:300]}")
        # otherwise: an empty repo; the first anchor creates the branch

    # ------------------------------------------------------------------ signing

    def _sign(self, payload: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "payload"
            f.write_text(payload, encoding="utf-8")
            subprocess.run([_tool("ssh-keygen"), "-q", "-Y", "sign", "-n", NAMESPACE,
                            "-f", str(self.sign_key), str(f)], check=True,
                           capture_output=True)  # fmt: skip
            return (Path(tmp) / "payload.sig").read_text()

    @staticmethod
    def _verify_sig(signers: Path, payload: str, sig: str) -> bool:
        with tempfile.TemporaryDirectory() as tmp:
            s = Path(tmp) / "sig"
            s.write_text(sig)
            res = subprocess.run(
                [_tool("ssh-keygen"), "-Y", "verify", "-f", str(signers), "-I", IDENTITY,
                 "-n", NAMESPACE, "-s", str(s)],
                input=payload.encode(), capture_output=True, timeout=30,
            )  # fmt: skip
            return res.returncode == 0

    # ------------------------------------------------------------------ anchor / verify

    def _files(self) -> tuple[Path, Path]:
        base = self.repo_dir / "anchors" / self.install_id
        return base / "signers", base / "checkpoints.jsonl"

    def anchor(self, log: AuditLog) -> dict[str, Any] | None:
        """Sign and push the current head; None when it's already anchored."""
        install = self.install_id
        self._sync()
        signers, checkpoints = self._files()
        seq, head = log.head()
        if seq == 0:
            return None  # an empty log: nothing to anchor yet
        last = self._entries(checkpoints)
        if last and last[-1]["payload"]["seq"] == seq:
            return None
        signers.parent.mkdir(parents=True, exist_ok=True)
        pub = self.sign_key.with_suffix(".pub").read_text().strip()
        if not signers.exists():
            signers.write_text(f"{IDENTITY} {pub}\n")
        payload = {"v": 2, "install": install, "seq": seq, "hash": head,
                   "root": merkle.root(log.hashes(seq)),
                   "ts": datetime.now(UTC).isoformat(timespec="seconds")}  # fmt: skip
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        entry = {"payload": payload, "sig": self._sign(text)}
        if self.cfg.tsa_url:  # third-party proof of the time (D69); failure isn't fatal
            import base64

            from cairnward.audit import timestamp

            try:
                reply = timestamp.request(self.cfg.tsa_url, text.encode())
                entry["tsr"] = base64.b64encode(reply).decode()
            except timestamp.TimestampError as exc:
                entry["tsr_error"] = str(exc)[:200]
        with checkpoints.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        self._git("add", "anchors")
        self._git("commit", "--quiet", "-m", f"checkpoint {install} seq {seq}")
        self._git("push", "--quiet", "origin", f"HEAD:{self.cfg.branch}")
        return payload

    @staticmethod
    def _check_root(log: AuditLog, seq: int, signed_root: str,
                    previous: tuple[int, str] | None) -> str | None:  # fmt: skip
        """Why a v2 checkpoint's Merkle root fails, or None: it must match the log's records
        1..seq, and its tree must extend the previous checkpoint's (RFC 9162 consistency)."""
        hashes = log.hashes(seq)
        if len(hashes) != seq:
            return f"seq {seq}: the log has {len(hashes)} records up to it, so some are missing"
        try:
            if merkle.root(hashes) != signed_root:
                return f"seq {seq}: the Merkle root differs from its anchor: the log was altered"
            if previous is not None:
                old_seq, old_root = previous
                proof = merkle.consistency_proof(old_seq, hashes)
                if not merkle.verify_consistency(old_seq, seq, old_root, signed_root, proof):
                    return (f"seq {seq}: its tree doesn't extend the checkpoint at seq "
                            f"{old_seq}: anchored history was rewritten")  # fmt: skip
        except merkle.ProofError as exc:
            return f"seq {seq}: malformed Merkle data ({exc})"
        return None

    @staticmethod
    def _entries(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                out.append({"payload": {}, "sig": "", "broken": line[:80]})
        return out

    def checkpoints(self) -> tuple[Path, list[dict[str, Any]]]:
        """The signers file and every checkpoint entry for this installation (after a sync)."""
        self._sync()
        signers, checkpoints = self._files()
        return signers, self._entries(checkpoints)

    def verify(self, log: AuditLog) -> AnchorResult:
        self._sync()
        signers, checkpoints = self._files()
        result = AnchorResult(ok=True)
        if not signers.exists():
            return AnchorResult(False, problems=["no anchors in the repo for this installation"])
        local_pub = self.sign_key.with_suffix(".pub").read_text().split()[:2]
        if signers.read_text().split()[1:3] != local_pub:
            result.problems.append("the repo's signing key differs from this machine's")
        previous: tuple[int, str] | None = None  # the last checkpoint with a verified root
        for entry in self._entries(checkpoints):
            payload = entry.get("payload") or {}
            text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            seq = payload.get("seq")
            if entry.get("broken") or not self._verify_sig(signers, text, entry.get("sig", "")):
                result.problems.append(f"anchor for seq {seq}: bad signature or format")
                continue
            result.anchors += 1
            result.last_seq, result.last_ts = seq, payload.get("ts")
            if entry.get("tsr") and self.cfg.tsa_ca_file:
                import base64

                from cairnward.audit import timestamp

                try:
                    when = timestamp.verify(text.encode(), base64.b64decode(entry["tsr"]),
                                            Path(self.cfg.tsa_ca_file).expanduser())  # fmt: skip
                    result.timestamped += 1
                    result.last_tsa_time = when
                except timestamp.TimestampError as exc:
                    result.problems.append(f"anchor for seq {seq}: {exc}")
            rec = log.at(int(seq)) if isinstance(seq, int) else None
            if rec is None:
                result.problems.append(f"seq {seq} was anchored but is missing from the log")
            elif rec.hash != payload.get("hash"):
                result.problems.append(f"seq {seq} differs from its anchor: the log was altered")
            if "root" in payload and isinstance(seq, int):
                problem = self._check_root(log, seq, str(payload["root"]), previous)
                if problem:
                    result.problems.append(problem)
                else:
                    result.merkle_roots += 1
                    previous = (seq, str(payload["root"]))
        head_seq, _ = log.head()
        if result.last_seq is not None:
            result.unanchored = max(0, head_seq - int(result.last_seq))
        result.ok = not result.problems
        return result


# ------------------------------------------------------------------ inclusion proofs

PROOF_FORMAT = "cairnward-inclusion-proof-1"


def make_proof(anchorer: Anchorer, log: AuditLog, seq: int) -> dict[str, Any]:
    """A self-contained proof that record `seq` is in the latest anchored checkpoint: the
    record (metadata and digests only, as in the log), its Merkle audit path, and the signed
    checkpoint. Someone holding it and the signers file can check it without the log."""
    rec = log.at(seq)
    if rec is None:
        raise AnchorError(f"there is no record {seq}")
    signers, entries = anchorer.checkpoints()
    covering = [e for e in entries
                if not e.get("broken") and "root" in (e.get("payload") or {})
                and e["payload"].get("seq", 0) >= seq]  # fmt: skip
    if not covering:
        raise AnchorError(f"no checkpoint with a Merkle root covers record {seq} yet: "
                          "run `cairnward audit anchor` first")  # fmt: skip
    entry = covering[-1]
    size = int(entry["payload"]["seq"])
    hashes = log.hashes(size)
    if len(hashes) != size or merkle.root(hashes) != entry["payload"]["root"]:
        raise AnchorError(f"the log no longer matches the checkpoint at seq {size}: run "
                          "`cairnward audit verify --anchors`")  # fmt: skip
    from dataclasses import asdict

    return {"format": PROOF_FORMAT, "record": asdict(rec), "index": seq - 1, "size": size,
            "proof": merkle.inclusion_proof(seq - 1, hashes), "checkpoint": entry,
            "signers": signers.read_text().strip() if signers.exists() else None}  # fmt: skip


def check_proof(bundle: dict[str, Any], signers: Path | None = None) -> list[str]:
    """Problems with a proof from make_proof; empty when it holds. Needs no log or config.
    The signature is checked only against a signers file the checker trusts (`signers`):
    the copy inside the bundle came with it, so it proves nothing on its own."""
    from cairnward.audit.log import AuditRecord

    problems: list[str] = []
    if bundle.get("format") != PROOF_FORMAT:
        return [f"not a Cairnward inclusion proof (format {bundle.get('format')!r})"]
    try:
        rec = AuditRecord(**bundle["record"])
        payload = bundle["checkpoint"]["payload"]
        index, size = int(bundle["index"]), int(bundle["size"])
        if rec.compute_hash() != rec.hash:
            problems.append("the record's fields don't match its hash: it was edited")
        if index != rec.seq - 1 or size != int(payload["seq"]):
            problems.append("the proof's position or size doesn't match the record and checkpoint")
        if not merkle.verify_inclusion(rec.hash, index, size, bundle["proof"], payload["root"]):
            problems.append("the record isn't in the checkpoint's Merkle tree")
    except (KeyError, TypeError, ValueError) as exc:
        return [f"malformed proof: {type(exc).__name__}: {str(exc)[:120]}"]
    if signers is None:
        problems.append("signature not checked: pass the signers file you trust (--signers)")
    else:
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if not Anchorer._verify_sig(signers, text, str(bundle["checkpoint"].get("sig", ""))):
            problems.append("the checkpoint's signature doesn't verify with that signers file")
    return problems
