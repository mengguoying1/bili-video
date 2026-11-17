#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""bili_cookie_grab.py — pull bilibili SESSDATA straight out of a local Chromium browser.

Needed when a download must go above 720P: anonymous bilibili only serves 720P,
1080P+ / 4K require an account cookie.

What it does
  * reads `<profile>/Local State` -> os_crypt.encrypted_key -> DPAPI-unprotect
    (pure ctypes, no pywin32 needed) -> AES-256-GCM key,
  * copies every profile's Cookies sqlite (falls back to a shared-read copy),
  * decrypts `v10`/`v11` cookies and prints the bilibili ones.

Known limits (measured 2026-11 on Windows):
  * Chrome 127+ writes `v20` cookies that are app-bound-encrypted; those cannot
    be decrypted outside the browser -> the script reports them and you copy
    SESSDATA from devtools instead (Application -> Cookies -> .bilibili.com).
  * If the browser is running, its Cookies file can be opened exclusively
    (`ERROR_SHARING_VIOLATION`). Close the browser, or just use devtools.

Usage
  python bili_cookie_grab.py              # scan every browser/profile
  python bili_cookie_grab.py --json out.json
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import shutil
import sqlite3
import sys
import tempfile

try:
    from Crypto.Cipher import AES
except ImportError:
    AES = None

WANT = ("SESSDATA", "bili_jct", "DedeUserID", "buvid3", "buvid4", "bili_ticket", "sid")


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi_decrypt(blob: bytes) -> bytes:
    buf = ctypes.create_string_buffer(blob, len(blob))
    blobin = DATA_BLOB(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blobout = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blobin), None, None, None, None, 0, ctypes.byref(blobout)):
        raise OSError("CryptUnprotectData failed")
    data = ctypes.string_at(blobout.pbData, blobout.cbData)
    ctypes.windll.kernel32.LocalFree(blobout.pbData)
    return data


def master_key(local_state: str) -> bytes:
    with open(local_state, "r", encoding="utf-8") as f:
        st = json.load(f)
    enc = base64.b64decode(st["os_crypt"]["encrypted_key"])
    if enc[:5] == b"DPAPI":
        enc = enc[5:]
    return dpapi_decrypt(enc)


def decrypt(enc: bytes, key: bytes) -> str:
    if not enc:
        return ""
    tag = bytes(enc[:3])
    if tag in (b"v10", b"v11"):
        nonce, payload = bytes(enc[3:15]), bytes(enc[15:])
        try:
            return AES.new(key, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(
                payload[:-16], payload[-16:]).decode("utf-8", "replace")
        except Exception as e:
            return "[[gcm-fail:%s]]" % e
    if tag == b"v20":
        return "[[v20-appbound: read SESSDATA from devtools instead]]"
    try:
        return dpapi_decrypt(bytes(enc)).decode("utf-8", "replace")
    except Exception as e:
        return "[[dpapi-fail:%s]]" % e


def copy_db(src: str) -> str | None:
    dst = os.path.join(tempfile.gettempdir(), "ckcopy_%d.db" % abs(hash(src)))
    try:
        shutil.copy2(src, dst)
        return dst
    except Exception:
        pass
    # shared-read fallback (works when the lock is not exclusive)
    try:
        with open(src, "rb") as f, open(dst, "wb") as o:
            o.write(f.read())
        return dst
    except Exception as e:
        print("   [skip] %s -> %s" % (src, e))
        return None


def scan(max_profiles=6):
    local = os.environ.get("LOCALAPPDATA", "")
    bases = [("chrome", os.path.join(local, r"Google\Chrome\User Data")),
             ("edge", os.path.join(local, r"Microsoft\Edge\User Data")),
             ("brave", os.path.join(local, r"BraveSoftware\Brave-Browser\User Data")),
             ("vivaldi", os.path.join(local, r"Vivaldi\User Data"))]
    found, shown = {}, 0
    for label, base in bases:
        ls = os.path.join(base, "Local State")
        if not os.path.exists(ls):
            continue
        try:
            key = master_key(ls)
        except Exception as e:
            print("[%s] no AES key: %s" % (label, e))
            continue
        print("[%s] AES key ok" % label)
        profs = [p for p in os.listdir(base)
                 if p == "Default" or p.startswith("Profile")]
        for prof in profs[:max_profiles]:
            for db in (os.path.join(base, prof, "Network", "Cookies"),
                       os.path.join(base, prof, "Cookies")):
                if not os.path.exists(db):
                    continue
                tmp = copy_db(db)
                if not tmp:
                    continue
                try:
                    con = sqlite3.connect(tmp)
                    rows = con.execute(
                        "select host_key,name,value,encrypted_value from cookies "
                        "where host_key like '%bilibili%'").fetchall()
                    con.close()
                except Exception as e:
                    print("   [db] %s" % e)
                    continue
                for host, name, plain, enc in rows:
                    val = plain or decrypt(enc, key)
                    print("%-7s %-11s %-16s %-12s %s" % (
                        label, prof, host, name,
                        (val[:28] + "...") if len(val) > 31 else val))
                    shown += 1
                    if name in WANT and val and not val.startswith("[["):
                        found.setdefault(name, val)
    if not shown:
        print("\nno bilibili cookies found — either that browser was never used to "
              "sign in, or the profile DB is exclusively locked / v20-encrypted.")
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None, help="write the usable cookies here")
    args = ap.parse_args()
    if AES is None:
        raise SystemExit("pycryptodome missing: pip install pycryptodome")
    found = scan()
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(found, f)
        print("\nwrote", args.json)
    if "SESSDATA" in found:
        print("\nuse it:  python bili_fetch.py <BV> --sessdata \"%s...\"" % found["SESSDATA"][:12])
    return 0


if __name__ == "__main__":
    sys.exit(main())
