#!/usr/bin/env python3
"""
add_icons.py

Usage:
  python scripts/add_icons.py --input scripts/icons.csv --index index.html --take 9 --dry-run
    python scripts/add_icons.py 9

This script reads an icons CSV (classes,name), takes the next N entries,
replaces the contents of the `<section class="gallery">` in `index.html`
with generated card markup, and optionally commits & pushes the change.

It moves used entries out of the input CSV so they won't be reused.
"""

import argparse
import csv
import datetime
import os
import re
import subprocess
import sys
import random


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)

def parse_args():
    p = argparse.ArgumentParser(description="Inject icons into index.html and optionally commit/push")
    p.add_argument("count", nargs="?", type=int, help="How many icons to take from input file")
    p.add_argument("--input", default=os.path.join(SCRIPT_DIR, "icons.csv"), help="CSV file with icon rows: classes,name or <i ...></i>,name")
    p.add_argument("--index", default=os.path.join(REPO_ROOT, "index.html"), help="Path to index.html to edit")
    p.add_argument("--take", type=int, default=9, help="How many icons to take from input file")
    p.add_argument("--dry-run", action="store_true", help="Don't write files or run git commands; print output")
    p.add_argument("--commit", action="store_true", help="Commit and push changes via git")
    p.add_argument("--token-env", default="GITHUB_TOKEN", help="Environment variable name that contains a GitHub token for HTTPS push (optional)")
    p.add_argument("--message", default=None, help="Commit message template; use {datetime} and {rand} tokens")
    return p.parse_args()


def extract_class_from_itag(itag):
    m = re.search(r'class\s*=\s*"([^"]+)"', itag)
    if m:
        return m.group(1).strip()
    return itag.strip()


def read_icons(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, newline='', encoding='utf-8') as f:
        reader = csv.reader(f)
        for r in reader:
            if not r:
                continue
            if len(r) == 1:
                part = r[0].strip()
                # allow "<i class='fa ...'></i> Name" separated by whitespace
                if "," in part:
                    cls, name = part.split(',', 1)
                    rows.append((cls.strip(), name.strip()))
                else:
                    continue
            else:
                cls = r[0].strip()
                name = r[1].strip()
                # keep the full <i...> tag verbatim if provided in the CSV
                rows.append((cls, name))
    return rows


def write_icons(remaining, path):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        for cls, name in remaining:
            writer.writerow([cls, name])


def generate_card(cls, name):
        # If the CSV provided a full <i ...></i> tag, use it verbatim.
        s = cls.strip()
        if s.startswith('<') and s.endswith('>'):
                # assume the user provided a full element like: <i class="fa-solid fa-thumbs-up"></i>
                icon_html = s
        else:
                icon_html = f'<i class="{cls}" aria-hidden="true"></i>'

        return f'''        <article class="card"><div class="icon-mark">{icon_html}</div><h2>{name}</h2></article>'''


def replace_gallery(index_path, cards_html, dry_run=False):
    with open(index_path, 'r', encoding='utf-8') as f:
        html = f.read()

    pattern = re.compile(r'(<section[^>]*class="gallery"[^>]*>)(.*?)(</section>)', re.DOTALL)
    m = pattern.search(html)
    if not m:
        raise RuntimeError("Could not find gallery <section> in index.html")

    start, old_inner, end = m.group(1), m.group(2), m.group(3)
    # Append new cards before the closing </section>
    append_html = "\n" + "\n".join(cards_html) + "\n"
    insert_at = m.start(3)
    new_html = html[:insert_at] + append_html + html[insert_at:]

    if dry_run:
        print("--- DRY RUN: Generated gallery append HTML ---\n")
        print(append_html)
        return new_html

    with open(index_path, 'w', encoding='utf-8') as f:
        f.write(new_html)
    return None


def git_commit_and_push(paths, message, token_env=None):
    # add and commit
    subprocess.check_call(["git", "add"] + paths)
    subprocess.check_call(["git", "commit", "-m", message])

    # determine current branch
    branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"]).decode().strip()

    token = None
    if token_env:
        token = os.environ.get(token_env)

    # If a token is provided, attempt a one-off https push using the token
    if token:
        try:
            origin = subprocess.check_output(["git", "remote", "get-url", "origin"]).decode().strip()
        except subprocess.CalledProcessError:
            origin = None

        push_url = None
        if origin:
            if origin.startswith("git@"):
                m = re.match(r"git@([^:]+):(.+)", origin)
                if m:
                    host = m.group(1)
                    path = m.group(2)
                    push_url = f"https://{host}/{path}"
            elif origin.startswith("https://"):
                push_url = origin

        if push_url:
            # inject token into https url
            push_url_with_token = push_url.replace("https://", f"https://{token}@")
            subprocess.check_call(["git", "push", push_url_with_token, f"HEAD:refs/heads/{branch}"])
            return

    # default push using local git config (ssh or credential helper)
    subprocess.check_call(["git", "push", "origin", branch])


def main():
    args = parse_args()
    icons = read_icons(args.input)
    if not icons:
        print(f"No icons found in {args.input}")
        return 1

    requested_take = args.count if args.count is not None else args.take
    take = min(requested_take, len(icons))
    selected = icons[:take]
    remaining = icons[take:]

    cards = [generate_card(cls, name) for cls, name in selected]

    if args.dry_run:
        replace_gallery(args.index, cards, dry_run=True)
        print(f"Would remove {take} entries from {args.input} and write remaining {len(remaining)} back.")
        return 0

    replace_gallery(args.index, cards, dry_run=False)

    # rotate input
    write_icons(remaining, args.input)
    print(f"Wrote remaining {len(remaining)} entries back to {args.input}")

    if args.commit:
        now = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
        rand = random.randint(1000, 9999)
        if args.message:
            msg = args.message.format(datetime=now, rand=rand)
        else:
            msg = f"Add {take} icons: {now} ({rand})"
        try:
            git_commit_and_push([args.index, args.input], msg)
            print("Committed and pushed changes.")
        except subprocess.CalledProcessError as e:
            print("Git command failed:", e)
            return 1

    return 0

if __name__ == '__main__':
    sys.exit(main())
