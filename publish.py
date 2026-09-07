#!/usr/bin/env python3
"""
publish.py - uploads markdown content to WordPress over SSH + WP-CLI.

No credentials: it uses your ssh key and WP-CLI installed on the server.
Idempotent on the slug: updates if it exists, creates if it doesn't. Default: ONLINE.
Content that is already published is updated in place, with no backup copy.

Config in config.yml next to the script (no secrets):
    ssh:   ssh host, e.g. root@vps
    path:  path of the WordPress installation on the server
    cli:   wp command on the server (default: "wp --allow-root")

Usage:
    python3 publish.py --dry-run                    # what it would do
    python3 publish.py content/page.md              # online
    python3 publish.py content/page.md --draft      # as a draft
    python3 publish.py content/page.md --cover-only # only the image
"""

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

import markdown
import yaml

import featured

CONTENT_DIR = Path(__file__).parent / "content"
STATUSES = ("publish", "draft", "pending", "private", "future")


def config():
    cfg_file = Path(__file__).parent / "config.yml"
    if not cfg_file.exists():
        sys.exit("config.yml is missing next to the script (see README.md)")
    cfg = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
    if not cfg.get("ssh") or not cfg.get("path"):
        sys.exit("config.yml: 'ssh' and 'path' are required")
    return cfg["ssh"], cfg["path"], cfg.get("cli", "wp --allow-root"), cfg.get("author")


def parse_file(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---"):
        sys.exit(f"{path.name}: the YAML front-matter is missing")
    _, front, body = raw.split("---", 2)
    meta = yaml.safe_load(front) or {}
    for f in ("title", "slug"):
        if not meta.get(f):
            sys.exit(f"{path.name}: required field '{f}' is missing")
    meta.setdefault("type", "post")
    # jetpack-portfolio: the 13 portfolio projects (Jetpack custom post type).
    # Added on 2026-09-05 so their typos can be fixed through the same safe flow used for
    # pages and posts, without new ad-hoc commands over SSH.
    if meta["type"] not in ("post", "page", "jetpack-portfolio"):
        sys.exit(f"{path.name}: type must be post, page or jetpack-portfolio")
    meta["html"] = markdown.markdown(body.strip(), extensions=["extra", "sane_lists"])
    meta["source_file"] = path.name
    return meta


def wp(host, path, cli, args, stdin=None, check=True) -> str:
    remote = f"{cli} {' '.join(shlex.quote(a) for a in args)} --path={shlex.quote(path)}"
    r = subprocess.run(["ssh", host, remote], input=stdin if stdin is not None else "", encoding="utf-8", capture_output=True)
    if r.returncode != 0:
        # check=False for the commands whose failure is a legitimate answer:
        # "wp post meta get" exits 1 when the meta doesn't exist, which is a normal case.
        if check:
            sys.exit(f"    wp-cli ERROR: {r.stderr.strip()[:400]}")
        return ""
    return r.stdout.strip()


def has_cover(host, path, cli, pid) -> bool:
    """True if the post already has a featured image. Covers are never overwritten:
    if there is one, a person chose it and it wins."""
    out = wp(host, path, cli, ["post", "meta", "get", pid, "_thumbnail_id", "--format=json"],
             check=False)          # exits 1 if the meta isn't there: that's a "no", not an error
    return bool(out and out.strip('"').strip() not in ("", "0", "null"))


def upload_cover(host, path, cli, pid, png: Path, title: str) -> str | None:
    """Copies the PNG to the server, imports it into the media library and sets it as the
    post's featured image. Returns the attachment ID, or None if it skips."""
    remote_png = f"/tmp/{png.name}"
    scp = subprocess.run(["scp", "-q", str(png), f"{host}:{remote_png}"], capture_output=True, encoding="utf-8")
    if scp.returncode != 0:
        print(f"            cover not uploaded (scp): {scp.stderr.strip()[:200]}")
        return None
    try:
        aid = wp(host, path, cli, [
            "media", "import", remote_png, f"--post_id={pid}", "--featured_image",
            f"--title={title}", "--porcelain",
        ])
    finally:
        # The temp file isn't ours to leave lying around on the server.
        subprocess.run(["ssh", host, f"rm -f {shlex.quote(remote_png)}"], capture_output=True)
    return aid


def find(host, path, cli, post_type, slug):
    """Returns (id, status) of the content with that slug, or (None, None)."""
    out = wp(host, path, cli, [
        "post", "list", f"--post_type={post_type}", f"--name={slug}",
        # NOT "any": with --name set, WP_Query excludes the statuses with
        # exclude_from_search (draft included) and drafts would never be found.
        f"--post_status={','.join(STATUSES)}", "--fields=ID,post_status", "--format=csv",
    ])
    lines = out.splitlines()
    if len(lines) < 2:
        return None, None
    pid, status = lines[1].split(",")[:2]
    return pid.strip(), status.strip()


def cover_only(host, path, cli, meta, dry_run):
    """Sets only the featured image of content already online, without touching its text.

    Needed because a post that is already published can't be updated without --publish (it
    would end up on a [BOZZA] copy): here the content isn't touched at all, only the image
    changes.
    """
    slug, ptype = meta["slug"], meta["type"]
    if ptype != "post":
        print(f"  {'SKIP':9} {slug}: covers are only for posts")
        return
    pid, status = find(host, path, cli, ptype, slug)
    if not pid:
        print(f"  {'SKIP':9} {slug}: it doesn't exist on the site")
        return
    if has_cover(host, path, cli, pid):
        print(f"  {'SKIP':9} {slug} (id={pid}): it already has a cover, it won't be overwritten")
        return
    png = featured.image_path(slug)
    print(f"  {'COVER':9} {ptype:5} /{slug:45} [{status}] id={pid}  <- {png.name}")
    if dry_run:
        if not png.exists():
            print("            (to be generated)")
        return
    png = featured.generate(meta)
    if not png:
        return
    aid = upload_cover(host, path, cli, pid, png, meta["title"])
    if aid:
        print(f"            cover set: attachment id={aid}")


def sync_one(host, path, cli, meta, draft, dry_run, images=True, author=None):
    slug, title, ptype = meta["slug"], meta["title"], meta["type"]
    author = meta.get("author", author)      # the front-matter wins over config.yml
    pid = status = None
    if not dry_run:
        pid, status = find(host, path, cli, ptype, slug)

    # Default: online. Content that is already published is updated in place, with no
    # [BOZZA] copies. With --draft it is uploaded as a draft.
    note = ""
    new_status = "draft" if draft else "publish"
    print(f"  {'UPDATE' if pid else 'CREATE':9} {ptype:5} /{slug:45} [{new_status}]  <- {meta['source_file']}{note}")
    if dry_run:
        print(f"            author: {author if author else 'NONE (post_author=0)'}")
        if images and ptype == "post":
            png = featured.image_path(slug)
            state = "already there" if png.exists() else "to be generated"
            print(f"            cover: {png.name} ({state}), if the post doesn't already have one")
        return

    # A new post headed online is created as a draft, given its cover, and only published at
    # the end. That way transition_post_status - the hook the social auto-poster attaches to -
    # fires when og:image is already there, and the preview on Facebook and LinkedIn doesn't
    # go out without an image (they cache the first scrape).
    # This is NOT done on content that is ALREADY online: it would mean taking it off the site
    # for a few seconds and firing the hook again, i.e. a second social post for an update.
    cover_first = images and ptype == "post" and not pid and new_status == "publish"

    common = [f"--post_title={title}", f"--post_status={'draft' if cover_first else new_status}"]
    if author:
        common.append(f"--post_author={author}")
    for key in ("excerpt", "parent", "menu_order"):
        if meta.get(key) is not None:
            common.append(f"--post_{key}={meta[key]}")
    if meta.get("categories"):            # numeric IDs: wp term list category --fields=term_id,name
        common.append("--post_category=" + ",".join(str(c) for c in meta["categories"]))
    if meta.get("tags"):                  # names, created if they don't exist
        common.append("--tags_input=" + ",".join(meta["tags"]))

    if pid:
        wp(host, path, cli, ["post", "update", pid, "-", *common], stdin=meta["html"])
    else:
        pid = wp(host, path, cli, [
            "post", "create", "-", f"--post_type={ptype}", f"--post_name={slug}",
            "--porcelain", *common,
        ], stdin=meta["html"])
    print(f"            id={pid}")

    # Cover: only for posts, only if they don't already have one.
    if images and ptype == "post" and not has_cover(host, path, cli, pid):
        png = featured.generate(meta)
        if png:
            aid = upload_cover(host, path, cli, pid, png, meta["title"])
            if aid:
                print(f"            cover set: attachment id={aid}")

    if cover_first:
        wp(host, path, cli, ["post", "update", pid, "--post_status=publish"])
        print("            published (the cover was already there)")

    # Cleans up an old [BOZZA] copy left over from the previous version of the script.
    # The "-bozza" suffix is what that version actually used on the site: it stays as it is,
    # otherwise the leftovers online would never be found.
    bid, _ = find(host, path, cli, ptype, f"{meta['slug']}-bozza")
    if bid:
        wp(host, path, cli, ["post", "delete", bid, "--force"])
        print(f"            removed the old [BOZZA] copy id={bid}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--draft", action="store_true",
                    help="upload as a draft instead of online")
    ap.add_argument("--no-image", action="store_true",
                    help="do not generate/upload the cover")
    ap.add_argument("--cover-only", action="store_true",
                    help="set only the featured image, without touching the text")
    a = ap.parse_args()

    paths = [Path(f) for f in a.files] or sorted(CONTENT_DIR.glob("*.md"))
    if not paths:
        sys.exit("No content.")
    contents = [parse_file(p) for p in paths]

    # config.yml is always read: it's a local file, and the dry-run has to be able to say
    # which author the post would end up with - without one, the social auto-poster doesn't
    # fire (post_author=0).
    host, path, cli, author = config()
    if a.dry_run and not a.cover_only:
        print(f"\nDRY RUN - {len(contents)} items, no changes:\n")
    else:
        # --cover-only queries the site in dry-run too: to say what it would do it has to know
        # which posts already have a cover, and those are all reads.
        mode = "DRY RUN covers" if a.dry_run else (
            "COVERS" if a.cover_only else ("DRAFTS" if a.draft else "PUBLISHING"))
        print(f"\n{mode} on {host}:{path} - {len(contents)} items:\n")

    for m in contents:
        if a.cover_only:
            cover_only(host, path, cli, m, a.dry_run)
        else:
            sync_one(host, path, cli, m, a.draft, a.dry_run,
                     images=not a.no_image, author=author)
    print()


if __name__ == "__main__":
    main()
