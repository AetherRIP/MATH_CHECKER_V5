"""Developer-side release tool (§9/§25): keys, packages and signatures.

The console manages releases, but SIGNING must happen on the Developer's own
machine — the private key never reaches the backend, the browser or a device.
This script is what produces the three values the console asks for: the
detached signature, the signing key id, and (informationally) the public key.

Usage
-----
Generate a key pair (do this once, keep the private key safe and offline):

    python scripts/sign_release.py generate-keys --out ./release_keys

Build a canonical package file from a directory of files:

    python scripts/sign_release.py build --src ./staging --version 1.9.0 \
        --out ./updates/1.9.0.json

Sign a package and print what the console needs:

    python scripts/sign_release.py sign --package ./updates/1.9.0.json \
        --private-key ./release_keys/release_private.pem

The package body is exactly the canonical JSON a device reconstructs into its
``Package`` object, so the checksum and the signature both cover the same bytes
that are stored and downloaded.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path

# Allow running as a plain script from the backend directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.distributed import signing  # noqa: E402


def generate_keys(out: str) -> int:
    target = Path(out)
    target.mkdir(parents=True, exist_ok=True)
    private_pem, public_pem = signing.generate_keypair()
    private_path = target / "release_private.pem"
    public_path = target / "release_public.pem"
    private_path.write_text(private_pem, encoding="utf-8")
    public_path.write_text(public_pem, encoding="utf-8")
    try:
        private_path.chmod(0o600)
    except OSError:
        pass  # Windows ACLs differ; the file is still the Developer's to protect
    print(f"private key: {private_path}  (KEEP THIS OFFLINE — never commit it)")
    print(f"public key : {public_path}")
    print(f"signing key id: {signing.key_id(public_pem)}")
    return 0


def build(src: str, version: str, out: str) -> int:
    root = Path(src)
    if not root.is_dir():
        print(f"error: {src} is not a directory", file=sys.stderr)
        return 2
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        files[rel] = path.read_bytes().hex()
    if not files:
        print("error: no files found to package", file=sys.stderr)
        return 2
    # Canonical bytes — exactly what the device reconstructs and verifies.
    body = json.dumps({"target_version": version, "files": files},
                      sort_keys=True).encode("utf-8")
    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    print(f"wrote {target} ({len(body)} bytes, {len(files)} file(s))")
    print(f"sha256: {hashlib.sha256(body).hexdigest()}")
    return 0


def sign(package: str, private_key: str) -> int:
    body = Path(package).read_bytes()
    try:
        private_pem = Path(private_key).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot read private key: {exc}", file=sys.stderr)
        return 2
    signature = signing.sign_package(body, private_pem)
    public_pem = signing.public_pem_from_private(private_pem)
    print(f"sha256          : {hashlib.sha256(body).hexdigest()}")
    print(f"signing_key_id  : {signing.key_id(public_pem)}")
    print(f"package_signature: {signature}")
    print()
    print("Paste the key id and signature into the console's New release form.")
    print("The private key was used locally and has NOT been uploaded.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate-keys", help="create an Ed25519 key pair")
    gen.add_argument("--out", default="./release_keys")

    bld = sub.add_parser("build", help="build a canonical package file")
    bld.add_argument("--src", required=True)
    bld.add_argument("--version", required=True)
    bld.add_argument("--out", required=True)

    sgn = sub.add_parser("sign", help="sign a package file")
    sgn.add_argument("--package", required=True)
    sgn.add_argument("--private-key", required=True)

    args = parser.parse_args(argv)
    if args.command == "generate-keys":
        return generate_keys(args.out)
    if args.command == "build":
        return build(args.src, args.version, args.out)
    if args.command == "sign":
        return sign(args.package, args.private_key)
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
