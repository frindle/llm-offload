#!/usr/bin/env python3
"""Extract the P256 signing surface from CryptoKit's .swiftinterface, verbatim.

WHY THIS IS NOT awk
-------------------
The first cut of this used `awk '/^extension CryptoKit\\.P256 \\{/,/^\\}/' | head -80`
and it produced a BROKEN excerpt in two ways that would have invalidated the
whole arm:

  1. `head -80` ran off the end of the `Signing` block and pulled in twenty lines
     of `P256.KeyAgreement` -- the wrong enum entirely, truncated mid-declaration.
     KeyAgreement has no signing API; including it is worse than including
     nothing.
  2. The sign/verify functions were grepped out of their `extension
     CryptoKit.P256.Signing.PrivateKey { ... }` wrappers, so the excerpt showed
     four bare `public func signature(...)` lines with nothing saying which type
     they hang off.

Defect 2 matters more than it looks. This arm exists because three models
invented three different wrong identifiers (P256.KeyPair, P256.SigningKey,
P256.Signing.Signature) -- i.e. they did not know WHERE the call lives. An
excerpt that shows the call without its owning type reproduces exactly that
ambiguity, so the arm would have been testing whether a model can guess from a
misleading handout rather than whether supplying the real surface removes the
failure. The treatment has to be correct or the experiment measures the
instrument.

So: brace-matched extraction, and every declaration keeps its enclosing scope.
"""
import re
import subprocess
import sys
from pathlib import Path

FALLBACK = ("/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/System/Library"
            "/Frameworks/CryptoKit.framework/Modules/CryptoKit.swiftmodule"
            "/arm64e-apple-macos.swiftinterface")


def find_interface():
    try:
        sdk = subprocess.run(["xcrun", "--show-sdk-path"], capture_output=True,
                             text=True, timeout=30).stdout.strip()
    except Exception:                                               # noqa: BLE001
        sdk = ""
    if sdk:
        hits = sorted(Path(sdk).glob(
            "System/Library/Frameworks/CryptoKit.framework/Modules/"
            "CryptoKit.swiftmodule/*-apple-macos.swiftinterface"))
        if hits:
            return hits[0]
    p = Path(FALLBACK)
    return p if p.is_file() else None


def block_at(lines, start_idx):
    """Return the lines of a brace-delimited block beginning on start_idx,
    ending on the line where nesting returns to zero. Counting braces is enough
    here: .swiftinterface is generated output with no string literals or
    comments containing braces."""
    depth = 0
    out = []
    for i in range(start_idx, len(lines)):
        line = lines[i]
        out.append(line)
        depth += line.count("{") - line.count("}")
        if depth <= 0 and i > start_idx:
            break
        if depth == 0 and i == start_idx and "{" not in line:
            break
    return out


def nested_block(lines, outer_start, inner_pattern):
    """Find `inner_pattern` inside the block that starts at outer_start and
    return just that inner block -- this is what keeps `enum Signing` from
    bleeding into its sibling `enum KeyAgreement`."""
    outer = block_at(lines, outer_start)
    for j, line in enumerate(outer):
        if re.search(inner_pattern, line):
            return block_at(outer, j)
    return []


def main():
    iface = find_interface()
    if iface is None:
        print("__API_SURFACE_UNAVAILABLE__")
        return 1

    lines = iface.read_text().splitlines()
    chunks = []

    # 1. P256.Signing -- the PublicKey / PrivateKey types themselves.
    for i, line in enumerate(lines):
        if re.match(r"^extension CryptoKit\.P256 \{", line):
            blk = nested_block(lines, i, r"public enum Signing\b")
            if blk:
                chunks.append("extension CryptoKit.P256 {\n" + "\n".join(blk) + "\n}")
                break

    # 2. ECDSASignature.
    for i, line in enumerate(lines):
        if re.match(r"^extension CryptoKit\.P256\.Signing \{", line):
            chunks.append("\n".join(block_at(lines, i)))
            break

    # 3. The sign/verify extensions, WITH their `extension ... {` headers, so
    #    every function keeps the type it belongs to.
    for i, line in enumerate(lines):
        if re.match(r"^extension CryptoKit\.P256\.Signing\.(PrivateKey|PublicKey) \{", line):
            chunks.append("\n".join(block_at(lines, i)))

    if len(chunks) < 3:
        print("__API_SURFACE_UNAVAILABLE__")
        return 1

    print(f"Relevant CryptoKit API surface, excerpted verbatim from {iface.name}:")
    print()
    print("```swift")
    print("\n".join(chunks))
    print("```")
    return 0


if __name__ == "__main__":
    sys.exit(main())
