#!/usr/bin/env python3
"""
add_icons.py

Publishes the next batch of icons to the gallery.

scripts/icons.csv is the pending queue that build_icon_data.py fills, one
icon-and-style pair per row. This script moves the next N rows out of it and
into data/published.js, which is the list index.html renders. Rows leave the
queue as they are published, so a batch is never repeated.

The gallery is driven by that published list, so a batch of 9 adds 9 cards.

Usage:
  python scripts/add_icons.py                      # stage the next 9
  python scripts/add_icons.py 9 --commit           # stage 9, commit and push
  python scripts/add_icons.py --take 45 --dry-run  # show a day's worth
"""

import argparse
import csv
import datetime
import json
import os
import re
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)

QUEUE_PATH = os.path.join(SCRIPT_DIR, "icons.csv")
PUBLISHED_PATH = os.path.join(REPO_ROOT, "data", "published.js")

HEADER = "/* Icons published to the gallery. scripts/add_icons.py appends here. */"
ENTRY = re.compile(r'"([a-z0-9-]+:\d+)"')


def parse_args():
    p = argparse.ArgumentParser(description="Publish the next batch of icons")
    p.add_argument("count", nargs="?", type=int,
                   help="how many icons to publish (default: --take)")
    p.add_argument("--take", type=int, default=9,
                   help="batch size when no count is given (default: 9)")
    p.add_argument("--queue", default=QUEUE_PATH, help="pending queue CSV")
    p.add_argument("--published", default=PUBLISHED_PATH, help="published list JS")
    p.add_argument("--dry-run", action="store_true",
                   help="print the batch without writing or committing")
    p.add_argument("--commit", action="store_true", help="commit and push the batch")
    p.add_argument("--no-push", action="store_true",
                   help="with --commit, commit only; useful when several "
                        "batches are committed before one push")
    p.add_argument("--token-env", default="GITHUB_TOKEN",
                   help="env var holding a GitHub token for HTTPS push (optional)")
    p.add_argument("--message", default=None,
                   help="commit message template; {count} {names} {date} are available")
    return p.parse_args()


def read_queue(path):
    """Remaining rows, plus the header so it can be written back."""
    if not os.path.exists(path):
        return [], []
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if not rows:
        return [], []
    header, body = rows[0], rows[1:]
    # Tolerate a queue saved without its header row.
    if header and header[0] != "name":
        return ["name", "style", "classes", "label", "style_label"], rows
    return header, body


def write_queue(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


def read_published(path):
    if not os.path.exists(path):
        return []
    return ENTRY.findall(open(path, encoding="utf-8").read())


def write_published(path, keys):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(HEADER + "\n")
        f.write("window.FA_PUBLISHED = [\n")
        f.write(",\n".join(json.dumps(k) for k in keys))
        f.write("\n];\n")


def git(*args):
    subprocess.check_call(["git", "-C", REPO_ROOT] + list(args))


def redact(text, secret):
    """Keep a token out of console output and scripts/logs/."""
    return text.replace(secret, "***") if secret else text


def commit_and_push(paths, message, token_env=None, push=True):
    git("add", *paths)
    git("commit", "-m", message)

    if not push:
        return

    branch = subprocess.check_output(
        ["git", "-C", REPO_ROOT, "rev-parse", "--abbrev-ref", "HEAD"]
    ).decode().strip()

    token = os.environ.get(token_env) if token_env else None
    if token:
        origin = subprocess.check_output(
            ["git", "-C", REPO_ROOT, "remote", "get-url", "origin"]
        ).decode().strip()

        push_url = None
        if origin.startswith("git@"):
            match = re.match(r"git@([^:]+):(.+)", origin)
            if match:
                push_url = f"https://{match.group(1)}/{match.group(2)}"
        elif origin.startswith("https://"):
            push_url = origin

        if push_url:
            authed = push_url.replace("https://", f"https://{token}@")
            try:
                git("push", authed, f"HEAD:refs/heads/{branch}")
            except subprocess.CalledProcessError as e:
                # The failed command carries the token, so never let it reach
                # the console or the log file.
                raise RuntimeError(redact(str(e), token)) from None
            return

    git("push", "origin", branch)


def main():
    args = parse_args()

    header, queued = read_queue(args.queue)
    if not queued:
        print(f"Queue is empty: {os.path.relpath(args.queue, REPO_ROOT)}")
        print("Refill it with: python scripts/build_icon_data.py --queue")
        return 1

    take = min(args.count if args.count is not None else args.take, len(queued))
    batch, remaining = queued[:take], queued[take:]

    for name, style, classes, label, style_label in batch:
        print(f"  {label} ({style_label})".ljust(48) + classes)

    plural = "" if take == 1 else "s"

    if args.dry_run:
        print(f"\nDry run: {take} icon{plural} would be published, "
              f"{len(remaining)} left in the queue.")
        return 0

    published = read_published(args.published)
    known = set(published)
    for name, style, _classes, _label, _style_label in batch:
        key = f"{name}:{style}"
        if key not in known:
            published.append(key)
            known.add(key)

    write_published(args.published, published)
    write_queue(args.queue, header, remaining)

    print(f"\nPublished {take} icon{plural} ({len(published)} total, "
          f"{len(remaining)} still queued).")

    if args.commit:
        names = ", ".join(row[3] for row in batch)
        template = args.message or "Add {count} icon{plural}: {names}"
        message = template.format(
            count=take,
            plural=plural,
            names=names,
            date=datetime.date.today().isoformat(),
        )
        try:
            commit_and_push([args.published, args.queue], message,
                            token_env=args.token_env, push=not args.no_push)
            print("Committed." if args.no_push else "Committed and pushed.")
        except (subprocess.CalledProcessError, RuntimeError) as e:
            print("Git command failed:", redact(str(e), os.environ.get(args.token_env)))
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
