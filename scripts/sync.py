"""自动发现 PHP / Nginx / Apache / Redis 的 Windows 最新版本并镜像到本仓库 Releases。

期望环境变量：
    GITHUB_TOKEN, GITHUB_REPO, FORCE (true/false), ONLY (php|nginx|apache|redis|'')

tag 命名约定: "{software}-{version}"
每个 Release body 放 ```json``` 代码块 (sha256/size/exe_rel/...)，manifest 生成器读它。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
import yaml

REPO = os.environ.get("GITHUB_REPO") or ""
TOKEN = os.environ.get("GITHUB_TOKEN") or ""
FORCE = os.environ.get("FORCE", "false").lower() == "true"
ONLY = os.environ.get("ONLY", "").strip().lower()

GH_API = "https://api.github.com"
SESS = requests.Session()
SESS.headers.update({
    "Accept": "application/vnd.github+json",
    "User-Agent": "ruststudy-mirror-sync/1.0",
})
if TOKEN:
    SESS.headers["Authorization"] = f"Bearer {TOKEN}"

CONFIG_PATH = Path("config.yaml")
MANIFEST_PATH = Path("manifest.json")


@dataclass
class Pkg:
    software: str
    version: str
    source_url: str
    exe_rel: str
    variant: Optional[str] = None
    category: str = ""

    @property
    def tag(self) -> str:
        return f"{self.software}-{self.version}"

    @property
    def filename(self) -> str:
        return urlparse(self.source_url).path.rsplit("/", 1)[-1]


# ---------- 上游发现 ----------

def discover_php(cfg: dict) -> list:
    url = "https://windows.php.net/downloads/releases/releases.json"
    r = SESS.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()
    include_branches = cfg.get("include_branches")
    exclude_versions = set(cfg.get("exclude_versions") or [])
    out = []
    for branch_key, branch in data.items():
        if not branch_key[0:1].isdigit():
            continue
        if include_branches is not None and branch_key not in include_branches:
            continue
        version = branch.get("version")
        if not version or version in exclude_versions:
            continue
        compiler, variant = None, None
        for key, cc in [("nts-vs17-x64", "vs17"), ("nts-vs16-x64", "vs16"), ("nts-vc15-x64", "vc15")]:
            if key in branch and branch[key].get("zip"):
                variant = branch[key]
                compiler = cc
                break
        if not variant:
            continue
        zip_path = variant["zip"]["path"]
        out.append(Pkg(
            software="php",
            version=version,
            source_url=f"https://windows.php.net/downloads/releases/{zip_path}",
            exe_rel="php-cgi.exe",
            variant=f"x64-NTS {compiler}",
            category="php",
        ))
    return out


def discover_nginx(cfg: dict) -> list:
    url = "https://nginx.org/en/download.html"
    r = SESS.get(url, timeout=30)
    r.raise_for_status()
    versions = sorted(set(re.findall(r"nginx-(\d+\.\d+\.\d+)\.zip", r.text)),
                      key=_semver_tuple, reverse=True)[:8]
    exclude = set(cfg.get("exclude_versions") or [])
    out = []
    for v in versions:
        if v in exclude:
            continue
        out.append(Pkg(
            software="nginx",
            version=v,
            source_url=f"https://nginx.org/download/nginx-{v}.zip",
            exe_rel="nginx.exe",
            category="web",
        ))
    return out


def discover_apache(cfg: dict) -> list:
    url = "https://www.apachelounge.com/download/"
    try:
        r = SESS.get(url, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"[apache] upstream fetch failed: {e}", file=sys.stderr)
        return []
    m = re.findall(r'href="(VS\d+/binaries/(httpd-(\d+\.\d+\.\d+)[^"]+Win64-VS\d+\.zip))"', r.text)
    seen = {}
    for href, filename, ver in m:
        if ver not in seen:
            seen[ver] = (href, filename)
    exclude = set(cfg.get("exclude_versions") or [])
    out = []
    items = sorted(seen.items(), key=lambda kv: _semver_tuple(kv[0]), reverse=True)[:4]
    for ver, (href, filename) in items:
        if ver in exclude:
            continue
        out.append(Pkg(
            software="apache",
            version=ver,
            source_url=f"https://www.apachelounge.com/download/{href}",
            exe_rel="bin/httpd.exe",
            category="web",
        ))
    return out


def discover_redis(cfg: dict) -> list:
    url = f"{GH_API}/repos/tporadowski/redis/releases?per_page=10"
    r = SESS.get(url, timeout=30)
    r.raise_for_status()
    releases = r.json()
    exclude = set(cfg.get("exclude_versions") or [])
    out = []
    for rel in releases[:6]:
        tag = rel["tag_name"].lstrip("v")
        if tag in exclude:
            continue
        zip_asset = next(
            (a for a in rel.get("assets", [])
             if a["name"].endswith(".zip") and "x64" in a["name"].lower()),
            None,
        )
        if not zip_asset:
            continue
        out.append(Pkg(
            software="redis",
            version=tag,
            source_url=zip_asset["browser_download_url"],
            exe_rel="redis-server.exe",
            category="cache",
        ))
    return out


DISCOVERY = {
    "php": discover_php,
    "nginx": discover_nginx,
    "apache": discover_apache,
    "redis": discover_redis,
}


# ---------- GitHub Release ----------

def list_existing_tags() -> set:
    tags = set()
    page = 1
    while True:
        r = SESS.get(f"{GH_API}/repos/{REPO}/releases",
                     params={"per_page": 100, "page": page}, timeout=30)
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        for rel in data:
            if not rel.get("draft"):
                tags.add(rel["tag_name"])
        if len(data) < 100:
            break
        page += 1
    return tags


def delete_release_by_tag(tag: str) -> None:
    r = SESS.get(f"{GH_API}/repos/{REPO}/releases/tags/{tag}", timeout=30)
    if r.status_code == 404:
        return
    r.raise_for_status()
    rid = r.json()["id"]
    SESS.delete(f"{GH_API}/repos/{REPO}/releases/{rid}", timeout=30).raise_for_status()
    SESS.delete(f"{GH_API}/repos/{REPO}/git/refs/tags/{tag}", timeout=30)


def create_release(pkg, zip_path, sha256, size):
    body_meta = {
        "sha256": sha256,
        "size_bytes": size,
        "exe_rel": pkg.exe_rel,
        "variant": pkg.variant,
        "category": pkg.category,
        "source_url": pkg.source_url,
    }
    body_text = "```json\n" + json.dumps(body_meta, indent=2) + "\n```"
    payload = {
        "tag_name": pkg.tag,
        "name": f"{pkg.software} {pkg.version}",
        "body": body_text,
        "draft": False,
        "prerelease": False,
    }
    r = SESS.post(f"{GH_API}/repos/{REPO}/releases", json=payload, timeout=30)
    r.raise_for_status()
    rel = r.json()
    upload_url = rel["upload_url"].split("{")[0]
    with open(zip_path, "rb") as f:
        up = SESS.post(
            upload_url,
            params={"name": pkg.filename},
            headers={"Content-Type": "application/zip"},
            data=f,
            timeout=600,
        )
    up.raise_for_status()
    return rel


def download_and_hash(url: str, dest: Path):
    h = hashlib.sha256()
    total = 0
    with SESS.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1024 * 64):
                if chunk:
                    f.write(chunk)
                    h.update(chunk)
                    total += len(chunk)
    return h.hexdigest(), total


def build_manifest() -> None:
    manifest = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repo": REPO,
        "packages": {},
    }
    page = 1
    while True:
        r = SESS.get(f"{GH_API}/repos/{REPO}/releases",
                     params={"per_page": 100, "page": page}, timeout=30)
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        for rel in data:
            if rel.get("draft") or rel.get("prerelease"):
                continue
            tag = rel["tag_name"]
            if "-" not in tag:
                continue
            software, version = tag.split("-", 1)
            body_raw = rel.get("body") or ""
            meta = {}
            m = re.search(r"```json\s*(\{.*?\})\s*```", body_raw, re.DOTALL)
            if m:
                try:
                    meta = json.loads(m.group(1))
                except Exception:
                    meta = {}
            asset = next((a for a in rel.get("assets", []) if a["name"].endswith(".zip")), None)
            if not asset:
                continue
            manifest["packages"].setdefault(software, []).append({
                "version": version,
                "variant": meta.get("variant"),
                "filename": asset["name"],
                "size_bytes": asset["size"],
                "sha256": meta.get("sha256"),
                "exe_rel": meta.get("exe_rel", ""),
                "tag": tag,
                "download_urls": [
                    f"https://cdn.jsdelivr.net/gh/{REPO}@{tag}/{asset['name']}",
                    asset["browser_download_url"],
                ],
            })
        if len(data) < 100:
            break
        page += 1
    for arr in manifest["packages"].values():
        arr.sort(key=lambda e: _semver_tuple(e["version"]), reverse=True)
    MANIFEST_PATH.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    total = sum(len(v) for v in manifest["packages"].values())
    print(f"[manifest] wrote {MANIFEST_PATH}, {total} entries")


def _semver_tuple(v: str):
    parts = []
    for seg in re.split(r"[.\-]", v):
        try:
            parts.append((0, int(seg)))
        except Exception:
            parts.append((1, seg))
    return tuple(parts)


def main() -> int:
    if not REPO or not TOKEN:
        print("GITHUB_REPO / GITHUB_TOKEN not set", file=sys.stderr)
        return 2
    config = {}
    if CONFIG_PATH.exists():
        config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    existing = list_existing_tags()
    print(f"[existing] {len(existing)} releases")
    softwares = ["php", "nginx", "apache", "redis"]
    if ONLY:
        softwares = [s for s in softwares if s == ONLY]
        if not softwares:
            print(f"[ONLY] invalid: {ONLY}", file=sys.stderr)
            return 2
    all_pkgs = []
    for sw in softwares:
        cfg = config.get(sw) or {}
        try:
            pkgs = DISCOVERY[sw](cfg)
        except Exception as e:
            print(f"[{sw}] discovery failed: {e}", file=sys.stderr)
            continue
        print(f"[{sw}] discovered {len(pkgs)} versions")
        all_pkgs.extend(pkgs)
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        for pkg in all_pkgs:
            if pkg.tag in existing and not FORCE:
                continue
            if FORCE and pkg.tag in existing:
                print(f"[force] delete and rebuild {pkg.tag}")
                try:
                    delete_release_by_tag(pkg.tag)
                except Exception as e:
                    print(f"  delete failed: {e}", file=sys.stderr)
                    continue
            print(f"[sync] {pkg.tag} <- {pkg.source_url}")
            zip_path = td_path / pkg.filename
            try:
                sha, size = download_and_hash(pkg.source_url, zip_path)
            except Exception as e:
                print(f"  download failed: {e}", file=sys.stderr)
                continue
            size_mb = size / 1024 / 1024
            print(f"  sha256={sha[:16]}... size={size_mb:.1f}MB")
            try:
                create_release(pkg, zip_path, sha, size)
            except Exception as e:
                print(f"  upload failed: {e}", file=sys.stderr)
                continue
            print(f"  ok {pkg.tag}")
    build_manifest()
    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
