#!/usr/bin/env python3
"""
Self-contained pack builder + packager for the public packs repo.

Reads catalogue.json (subjects + their Wikipedia source articles), rebuilds each
pack from fresh Wikipedia content, then packages into zips + a version-aware
pack-index.json. Version is bumped ONLY for packs whose content changed vs the
previously published index, so the weekly workflow doesn't force needless
re-downloads.

Usage:
    python build.py --base-url <url> [--prev-index prev-index.json] [--max-chunks 150]
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import zipfile

API = "https://en.wikipedia.org/w/api.php"
# A descriptive User-Agent with contact is Wikipedia's policy and gets far less
# aggressive rate-limiting than a generic one.
UA = ("OfflineAI-PackBuilder/1.0 "
      "(https://github.com/KhaleonProductions/offline-ai-packs; contact via GitHub issues)")
SKIP_SECTIONS = re.compile(
    r"^(See also|References|Further reading|External links|Notes|Citations|Bibliography|Gallery)$",
    re.IGNORECASE)


def fetch_plaintext(title, retries=7):
    """Fetch an article with exponential backoff on 429 (CI shares throttled IPs)."""
    params = {"action": "query", "prop": "extracts", "explaintext": "1",
              "redirects": "1", "format": "json", "titles": title}
    url = API + "?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
            for _, page in data.get("query", {}).get("pages", {}).items():
                return page.get("extract", "") or ""
            return ""
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                # Exponential backoff: 10, 20, 40, 80 ... seconds.
                wait = 10 * (2 ** attempt)
                print(f"    429 on '{title}' — backing off {wait}s "
                      f"(attempt {attempt + 1}/{retries})", flush=True)
                time.sleep(wait)
                continue
            raise
    return ""


def to_chunks(text, source):
    chunks, section, skipping = [], "", False
    for raw in text.split("\n"):
        para = raw.strip()
        if not para:
            continue
        m = re.match(r"^=+\s*(.+?)\s*=+$", para)
        if m:
            section = m.group(1).strip()
            skipping = bool(SKIP_SECTIONS.match(section))
            continue
        if skipping or len(para) < 120:
            continue
        prefix = f"[{source}"
        if section and section.lower() not in ("introduction", source.lower()):
            prefix += f" — {section}"
        prefix += "] "
        chunks.append(prefix + para)
    return chunks


def build_pack(pack_id, title, articles, max_chunks):
    all_chunks = []
    for article in articles:
        text = fetch_plaintext(article)
        if text:
            all_chunks.extend(to_chunks(text, article.replace("_", " ")))
        time.sleep(2.0)
    if len(all_chunks) > max_chunks:
        step = len(all_chunks) / max_chunks
        all_chunks = [all_chunks[int(i * step)] for i in range(max_chunks)]
    return all_chunks


def content_hash(title, chunks):
    h = hashlib.sha256()
    h.update(title.encode("utf-8"))
    for c in chunks:
        h.update(c.encode("utf-8"))
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--prev-index", default="")
    ap.add_argument("--max-chunks", type=int, default=150)
    ap.add_argument("--out", default="./dist")
    args = ap.parse_args()

    with open("catalogue.json", encoding="utf-8") as f:
        subjects = json.load(f)["subjects"]

    prev = {}
    if args.prev_index and os.path.isfile(args.prev_index):
        with open(args.prev_index, encoding="utf-8") as f:
            for p in json.load(f).get("packs", []):
                prev[p["id"]] = p

    os.makedirs(args.out, exist_ok=True)
    index = {"packs": []}
    changed, unchanged, failed = [], [], []

    def build_one(subj):
        pid, title, articles = subj["id"], subj["title"], subj["articles"]
        try:
            chunks = build_pack(pid, title, articles, args.max_chunks)
        except Exception as e:
            print(f"  FAILED {pid}: {e}", flush=True)
            return None
        if not chunks:
            print(f"  no content for {pid}", flush=True)
            return None
        return chunks

    # First pass.
    pending = list(subjects)
    results = {}  # id -> chunks
    for subj in pending:
        print(f"building {subj['id']} ...", flush=True)
        c = build_one(subj)
        if c:
            results[subj["id"]] = c

    # Retry pass for any that failed (usually transient 429s), after a cooldown.
    retry = [s for s in subjects if s["id"] not in results]
    if retry:
        print(f"\nretrying {len(retry)} failed pack(s) after a 60s cooldown ...", flush=True)
        time.sleep(60)
        for subj in retry:
            print(f"retry {subj['id']} ...", flush=True)
            c = build_one(subj)
            if c:
                results[subj["id"]] = c

    for subj in subjects:
        pid, title, articles = subj["id"], subj["title"], subj["articles"]
        chunks = results.get(pid)
        if not chunks:
            failed.append(pid); continue

        chash = content_hash(title, chunks)
        prev_entry = prev.get(pid)
        if prev_entry and prev_entry.get("contentHash") == chash:
            version = prev_entry["version"]; unchanged.append(pid)
        else:
            version = (prev_entry["version"] + 1) if prev_entry else 1
            changed.append(pid)

        manifest = {
            "id": pid, "title": title, "version": version, "origin": "CURATED",
            "chunkCount": len(chunks), "sizeBytes": 0, "refreshOptIn": True,
            "createdEpochMs": 0, "updatedEpochMs": 0,
        }
        zip_path = os.path.join(args.out, f"{pid}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps(manifest, indent=2))
            for i, c in enumerate(chunks):
                z.writestr(f"chunks/{i:04d}.txt", c)

        size = os.path.getsize(zip_path)
        with open(zip_path, "rb") as f:
            zip_sha = hashlib.sha256(f.read()).hexdigest()
        index["packs"].append({
            "id": pid, "title": title, "version": version,
            "url": f"{args.base_url}/{pid}.zip", "sizeBytes": size,
            "sha256": zip_sha, "contentHash": chash,
        })

    # For any pack that FAILED to build this run, keep its previously-published
    # index entry (if any) so it stays downloadable at its old version — a
    # transient build failure must NOT make a pack disappear from the catalogue.
    have = {p["id"] for p in index["packs"]}
    for pid in failed:
        if pid in prev and pid not in have:
            index["packs"].append(prev[pid])
            print(f"  kept previous published entry for failed pack: {pid}", flush=True)
    index["packs"].sort(key=lambda p: p["id"])

    with open(os.path.join(args.out, "pack-index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2)
    print(f"\npacks={len(index['packs'])} changed={len(changed)} "
          f"unchanged={len(unchanged)} failed={failed}")
    if changed:
        print("changed:", ", ".join(changed))

    # Fail the job (non-zero exit) if any pack failed AND it had no previous
    # published version to fall back on — i.e. it's missing from the catalogue.
    # (Failures with a fallback are tolerated: the old version stays live.)
    missing = [pid for pid in failed if pid not in prev]
    if missing:
        print(f"\nERROR: {len(missing)} pack(s) failed with no fallback: {missing}")
        sys.exit(1)
    if failed:
        print(f"\nWARNING: {len(failed)} pack(s) failed but kept their previous "
              f"published version: {failed}")


if __name__ == "__main__":
    main()
