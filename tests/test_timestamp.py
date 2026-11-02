"""RFC 3161 timestamps (D69) against a real timestamp authority run locally with
`openssl ts` (its own CA and TSA certificate made for the test): tokens verify for the
exact checkpoint and fail for anything else."""

import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from cairnward.audit import timestamp

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")

TSA_CONF = """
[ tsa ]
default_tsa = tsa_config
[ tsa_config ]
dir = {dir}
serial = $dir/serial
signer_cert = $dir/tsa.crt
certs = $dir/ca.crt
signer_key = $dir/tsa.key
signer_digest = sha256
default_policy = 1.2.3.4.1
digests = sha256
accuracy = secs:1
ordering = no
tsa_name = no
ess_cert_id_chain = no
ess_cert_id_alg = sha256
"""


def run(*args: str, cwd: Path) -> None:
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


def make_tsa(base: Path) -> Path:
    """A local timestamp authority: CA and TSA certificates, and an openssl ts config."""
    d = base / "tsa"
    d.mkdir()
    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.crt",
        "-subj", "/CN=Test CA", "-days", "2", "-addext", "basicConstraints=critical,CA:true",
        "-addext", "keyUsage=critical,keyCertSign", cwd=d)  # fmt: skip
    (d / "ext.cnf").write_text("extendedKeyUsage=critical,timeStamping\n"
                               "keyUsage=critical,digitalSignature\n")  # fmt: skip
    run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "tsa.key", "-out", "tsa.csr",
        "-subj", "/CN=Test TSA", cwd=d)  # fmt: skip
    run("x509", "-req", "-in", "tsa.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial",
        "-out", "tsa.crt", "-days", "2", "-extfile", "ext.cnf", cwd=d)  # fmt: skip
    (d / "serial").write_text("01\n")
    (d / "tsa.cnf").write_text(TSA_CONF.format(dir=d.as_posix()))  # openssl: no backslashes
    return d


@pytest.fixture
def tsa(tmp_path: Path) -> Path:
    return make_tsa(tmp_path)


def authority(d: Path) -> httpx.MockTransport:
    def handle(req: httpx.Request) -> httpx.Response:
        assert req.headers["content-type"] == "application/timestamp-query"
        (d / "q.tsq").write_bytes(req.read())
        run("ts", "-reply", "-config", "tsa.cnf", "-queryfile", "q.tsq", "-out", "r.tsr", cwd=d)
        return httpx.Response(200, content=(d / "r.tsr").read_bytes(),
                              headers={"content-type": "application/timestamp-reply"})  # fmt: skip

    return httpx.MockTransport(handle)


def test_timestamp_and_verify(tsa: Path) -> None:
    checkpoint = b'{"hash":"abc","seq":42}'
    reply = timestamp.request("https://tsa.example/tsr", checkpoint, transport=authority(tsa))
    when = timestamp.verify(checkpoint, reply, tsa / "ca.crt")
    assert "GMT" in when
    with pytest.raises(timestamp.TimestampError, match="doesn't verify"):
        timestamp.verify(b'{"hash":"abd","seq":42}', reply, tsa / "ca.crt")  # altered


def test_authority_failures(tsa: Path) -> None:
    down = httpx.MockTransport(lambda r: httpx.Response(503))
    with pytest.raises(timestamp.TimestampError, match="answered 503"):
        timestamp.request("https://tsa.example/tsr", b"x", transport=down)
