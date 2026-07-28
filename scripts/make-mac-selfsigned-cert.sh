#!/usr/bin/env bash
# Mint a self-signed code-signing certificate for the macOS desktop build.
#
# WHY THIS EXISTS
# ---------------
# A $99/yr Apple Developer ID is the only thing that satisfies Gatekeeper (the
# "Apple could not verify MindFlock is free of malware" prompt). We don't have
# one, and this script does NOT change that — users still clear that prompt
# once by hand (README explains how).
#
# What a self-signed cert DOES fix, for free: the app gets a STABLE code
# identity. macOS TCC keys folder-access grants ("MindFlock would like to
# access files in your Documents folder") to that identity. An unsigned app has
# no stable identity, so the grant never sticks and the prompt returns on every
# launch. Sign every build with the SAME cert and the grant is remembered.
#
# USAGE
# -----
#   scripts/make-mac-selfsigned-cert.sh [password]
#
# Produces (in the current directory):
#   mindflock-selfsigned.p12          the cert + private key, for electron-builder
#   mindflock-selfsigned.p12.base64   the same, base64'd for a GitHub secret
#
# Then store these as repository secrets so CI signs release builds with it:
#   MAC_CSC_LINK          = contents of mindflock-selfsigned.p12.base64
#   MAC_CSC_KEY_PASSWORD  = the password printed below
# release.yml passes them to electron-builder as CSC_LINK / CSC_KEY_PASSWORD.
#
# KEEP THE SAME CERT. Regenerating it changes the code identity, which resets
# every user's remembered folder grant (they get re-prompted once). The .p12 is
# a signing key — do not commit it; the base64 lives only in the CI secret.
#
# Runs anywhere with openssl (no macOS required) — only the eventual build must
# run on macOS.
set -euo pipefail

# Hex, not base64: the password travels through a GitHub secret and macOS
# `security import`; a plain [0-9a-f] string can't pick up a stray +/=/newline
# that would later read back as the wrong password.
PASS="${1:-$(openssl rand -hex 24)}"
OUT_P12="mindflock-selfsigned.p12"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Self-signed cert with the code-signing EKU so `codesign` / `security
# find-identity -p codesigning` will accept it as a signing identity.
openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
  -keyout "$TMP/key.pem" -out "$TMP/cert.pem" \
  -subj "/CN=MindFlock Self-Signed/O=MindFlock/C=US" \
  -addext "keyUsage=critical,digitalSignature" \
  -addext "extendedKeyUsage=critical,codeSigning"

# Bundle into a PKCS#12 that electron-builder can import. The friendly name is
# the identity string electron-builder/codesign will match on.
#
# -legacy + -macalg sha1 are REQUIRED: OpenSSL 3.x defaults to a SHA-256 MAC
# and AES-256 encryption that macOS's `security import` cannot read — it fails
# with "MAC verification failed during PKCS12 import (wrong password?)", which
# looks like a password bug but is really an algorithm mismatch. Legacy PBE
# (3DES) + a SHA-1 MAC is what the macOS keychain importer expects.
openssl pkcs12 -export -legacy -macalg sha1 \
  -inkey "$TMP/key.pem" -in "$TMP/cert.pem" \
  -name "MindFlock Self-Signed" \
  -out "$OUT_P12" -passout pass:"$PASS"

base64 < "$OUT_P12" > "$OUT_P12.base64"

echo
echo "Wrote $OUT_P12 and $OUT_P12.base64"
echo
echo "Set these repository secrets (Settings -> Secrets and variables -> Actions):"
echo "  MAC_CSC_LINK          = contents of $OUT_P12.base64"
echo "  MAC_CSC_KEY_PASSWORD  = $PASS"
echo
echo "Do NOT commit $OUT_P12 (it holds a private key). It is a signing key."
