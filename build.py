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
import time
import urllib.parse
import urllib.request
import zipfile

API = "https://en.wikipedia.org/w/api.php"
UA = "OfflineAI-PackBuilder/0.1 (offline knowledge packs)"
SKIP_SECTIONS = re.compile(
    r"^(See also|References|Further reading|External links|Notes|Citations|Bibliography|Gallery)$",
    re.IGNORECASE)


def fetch_plaintext(title, retries=4):
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
                time.sleep(5 * (attempt + 1)); continue
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

    for subj in subjects:
        pid, title, articles = subj["id"], subj["title"], subj["articles"]
        print(f"building {pid} ...", flush=True)
        try:
            chunks = build_pack(pid, title, articles, args.max_chunks)
        except Exception as e:
            print(f"  FAILED {pid}: {e}"); failed.append(pid); continue
        if not chunks:
            print(f"  no content for {pid}"); failed.append(pid); continue

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

    with open(os.path.join(args.out, "pack-index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2)
    print(f"\npacks={len(index['packs'])} changed={len(changed)} "
          f"unchanged={len(unchanged)} failed={failed}")
    if changed:
        print("changed:", ", ".join(changed))


if __name__ == "__main__":
    main()
