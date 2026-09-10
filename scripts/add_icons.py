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
import random
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
                   help="how many icons to publish per batch (default: --take)")
    p.add_argument("--take", type=int, default=9,
                   help="batch size when no count is given (default: 9)")
    p.add_argument("--batches", type=int, default=1,
                   help="number of batches to publish in this run (default: 1)")
    p.add_argument("--min-batches", type=int, default=None,
                   help="minimum batches to publish (random selection with --max-batches)")
    p.add_argument("--max-batches", type=int, default=None,
                   help="maximum batches to publish (random selection with --min-batches)")
    p.add_argument("--spread-timestamps", action="store_true",
                   help="spread commit timestamps across daytime hours to look human")
    p.add_argument("--rest-day-chance", type=float, default=0.0,
                   help="probability (0.0 to 1.0) of taking a rest day on weekdays")
    p.add_argument("--weekend-rest-chance", type=float, default=0.0,
                   help="probability (0.0 to 1.0) of taking a rest day on weekends")
    p.add_argument("--force", action="store_true",
                   help="force publishing even if rest day roll would skip")
    p.add_argument("--tz-offset", default="+05:30",
                   help="timezone offset string for commit timestamps (default: +05:30)")
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


def parse_tz(offset_str):
    sign = -1 if offset_str.startswith("-") else 1
    raw = offset_str.lstrip("+-")
    parts = [int(p) for p in raw.split(":")]
    hours = parts[0]
    minutes = parts[1] if len(parts) > 1 else 0
    return datetime.timezone(sign * datetime.timedelta(hours=hours, minutes=minutes))


def is_rest_day(weekday_chance, weekend_chance, tz_offset_str="+05:30"):
    tz = parse_tz(tz_offset_str)
    now = datetime.datetime.now(tz)
    is_weekend = now.weekday() >= 5  # Saturday=5, Sunday=6
    chance = weekend_chance if is_weekend else weekday_chance
    if chance <= 0.0:
        return False
    return random.random() < chance


def generate_timestamps(n, tz_offset_str="+05:30"):
    tz = parse_tz(tz_offset_str)
    now = datetime.datetime.now(tz)
    today = now.date()

    # Typical active coding window: 09:00 AM to 21:45 PM
    window_start = datetime.datetime(today.year, today.month, today.day, 9, 0, 0, tzinfo=tz)
    window_end = datetime.datetime(today.year, today.month, today.day, 21, 45, 0, tzinfo=tz)

    # Prevent future timestamps (leave at least 2 mins before current time)
    effective_end = min(window_end, now - datetime.timedelta(minutes=2))

    if effective_end <= window_start:
        # If running earlier in the morning, spread within recent morning hours (e.g. from 7:30 AM)
        morning_floor = datetime.datetime(today.year, today.month, today.day, 7, 30, 0, tzinfo=tz)
        if effective_end <= morning_floor:
            effective_start = effective_end - datetime.timedelta(hours=1)
        else:
            effective_start = morning_floor
    else:
        effective_start = window_start

    total_seconds = max(60, int((effective_end - effective_start).total_seconds()))

    timestamps = []
    if n == 1:
        chosen_sec = random.randint(0, total_seconds)
        timestamps.append(effective_start + datetime.timedelta(seconds=chosen_sec))
    else:
        segment_size = total_seconds / n
        for i in range(n):
            seg_start = int(i * segment_size)
            seg_end = int((i + 1) * segment_size)
            margin = max(1, int(segment_size * 0.15))
            lower = seg_start + margin
            upper = max(lower, seg_end - margin)
            chosen = random.randint(lower, upper)
            timestamps.append(effective_start + datetime.timedelta(seconds=chosen))

    timestamps.sort()
    return [ts.isoformat(timespec="seconds") for ts in timestamps]


def read_queue(path):
    """Remaining rows, plus the header so it can be written back."""
    if not os.path.exists(path):
        return [], []
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if not rows:
        return [], []
    header, body = rows[0], rows[1:]
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


def git(*args, env=None):
    cmd_env = os.environ.copy()
    if env:
        cmd_env.update(env)
    subprocess.check_call(["git", "-C", REPO_ROOT] + list(args), env=cmd_env)


def redact(text, secret):
    """Keep a token out of console output and scripts/logs/."""
    return text.replace(secret, "***") if secret else text


def commit_and_push(paths, message, token_env=None, push=True, commit_date=None):
    git("add", *paths)
    commit_env = {}
    if commit_date:
        commit_env["GIT_AUTHOR_DATE"] = commit_date
        commit_env["GIT_COMMITTER_DATE"] = commit_date
    git("commit", "-m", message, env=commit_env)

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
                raise RuntimeError(redact(str(e), token)) from None
            return

    git("push", "origin", branch)


def update_github_actions_env(made, icons_each, queued, published):
    if "GITHUB_ENV" in os.environ:
        with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as ef:
            ef.write(f"made={made}\n")
            ef.write(f"icons_each={icons_each}\n")

    if "GITHUB_STEP_SUMMARY" in os.environ:
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as sf:
            sf.write("### Icon batches published\n\n")
            sf.write("| Metric | Value |\n|---|---|\n")
            sf.write(f"| Commits this run | {made} |\n")
            sf.write(f"| Icons published today | {made * icons_each} |\n")
            sf.write(f"| On the gallery | {published} |\n")
            sf.write(f"| Still queued | {queued} |\n")
            if made * icons_each > 0:
                sf.write(f"| Days of queue left | {queued // (made * icons_each)} |\n")


def main():
    args = parse_args()

    # 1. Check rest day
    if not args.force and is_rest_day(args.rest_day_chance, args.weekend_rest_chance, args.tz_offset):
        print(f"[Rest Day] Skipping icon publish run today to simulate natural developer rhythm.")
        update_github_actions_env(0, args.count or args.take, len(read_queue(args.queue)[1]), len(read_published(args.published)))
        return 0

    # 2. Determine number of batches
    if args.min_batches is not None and args.max_batches is not None:
        low = min(args.min_batches, args.max_batches)
        high = max(args.min_batches, args.max_batches)
        num_batches = random.randint(low, high)
    elif args.batches > 1:
        num_batches = args.batches
    else:
        num_batches = 1

    take_per_batch = args.count if args.count is not None else args.take

    # 3. Timestamps
    if args.spread_timestamps:
        timestamps = generate_timestamps(num_batches, args.tz_offset)
    else:
        timestamps = [None] * num_batches

    print(f"Planning {num_batches} batch(es) of {take_per_batch} icon(s)...")

    header, queued = read_queue(args.queue)
    if not queued:
        print(f"Queue is empty: {os.path.relpath(args.queue, REPO_ROOT)}")
        print("Refill it with: python scripts/build_icon_data.py --queue")
        return 1

    if args.dry_run:
        print("\n--- Dry Run Preview ---")
        offset = 0
        for b_idx in range(num_batches):
            slice_icons = queued[offset:offset + take_per_batch]
            offset += len(slice_icons)
            ts_label = f" @ {timestamps[b_idx]}" if timestamps[b_idx] else ""
            print(f"Batch {b_idx + 1}/{num_batches} ({len(slice_icons)} icons){ts_label}:")
            for name, style, classes, label, style_label in slice_icons[:3]:
                print(f"    {label} ({style_label}) - {classes}")
            if len(slice_icons) > 3:
                print(f"    ... and {len(slice_icons) - 3} more")
            if not slice_icons:
                break
        print(f"\nDry run complete: {offset} total icons across {num_batches} batches would be published.")
        return 0

    published = read_published(args.published)
    known = set(published)
    made = 0

    for b_idx in range(num_batches):
        header, queued = read_queue(args.queue)
        if not queued:
            print(f"Queue is empty after {made} batch(es) - stopping early.")
            break

        take = min(take_per_batch, len(queued))
        batch, remaining = queued[:take], queued[take:]

        for name, style, _classes, _label, _style_label in batch:
            key = f"{name}:{style}"
            if key not in known:
                published.append(key)
                known.add(key)

        write_published(args.published, published)
        write_queue(args.queue, header, remaining)

        plural = "" if take == 1 else "s"
        ts = timestamps[b_idx] if b_idx < len(timestamps) else None
        ts_label = f" (dated {ts})" if ts else ""
        print(f"Batch {b_idx + 1}/{num_batches}: Published {take} icon{plural}{ts_label}.")

        if args.commit:
            names = ", ".join(row[3] for row in batch)
            template = args.message or "Add {count} icon{plural}: {names}"
            message = template.format(
                count=take,
                plural=plural,
                names=names,
                date=datetime.date.today().isoformat(),
            )
            is_last = (b_idx == num_batches - 1)
            should_push = (not args.no_push) and is_last
            try:
                commit_and_push([args.published, args.queue], message,
                                token_env=args.token_env, push=should_push,
                                commit_date=ts)
                print(f"  Committed{ts_label}.")
            except (subprocess.CalledProcessError, RuntimeError) as e:
                print("Git command failed:", redact(str(e), os.environ.get(args.token_env)))
                return 1

        made += 1

    print(f"\nSuccessfully published {made} batch(es) ({made * take_per_batch} icons total).")
    print(f"{len(published)} icons now live on the gallery; {len(remaining)} left in the queue.")

    update_github_actions_env(made, take_per_batch, len(remaining), len(published))
    return 0


if __name__ == "__main__":
    sys.exit(main())

