#!/usr/bin/env python3
"""
build_icon_data.py

Reads the Font Awesome Pro files that ship in this repo and writes the two
things the gallery is built from. Nothing is hardcoded and nothing is fetched:
every name, codepoint and style comes out of css/ and webfonts/.

  1. css/fontawesome.css and css/brands.css map icon names -> codepoints.
     A rule may carry several names; the extras are aliases.
  2. Every other css/*.css declares one style: the family class, the style
     class, and the .woff2 it loads.
  3. The .woff2 cmap tables say which codepoints that style actually draws,
     so the gallery never renders a missing glyph as a blank box.

Outputs:
  data/catalog.js    every icon this install can draw - reference data that
                     only changes when the Font Awesome version does.
  scripts/icons.csv  the pending queue, one icon-and-style pair per row,
                     shuffled so each daily batch is a varied mix.

The queue is what scripts/add_icons.py publishes from, a batch at a time.
Anything already listed in data/published.js is left out of a rebuilt queue,
so re-running this after a Font Awesome upgrade queues up only what is new.

Usage:
  python scripts/build_icon_data.py            # catalog only
  python scripts/build_icon_data.py --queue    # catalog + rebuild the queue
  python scripts/build_icon_data.py --stats    # report only, write nothing
"""

import argparse
import csv
import datetime
import glob
import json
import os
import random
import re

try:
    from fontTools.ttLib import TTFont
except ImportError:
    raise SystemExit(
        "fontTools is required. Install it with:\n"
        "    pip install fonttools brotli"
    )

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
CSS_DIR = os.path.join(REPO_ROOT, "css")
WEBFONT_DIR = os.path.join(REPO_ROOT, "webfonts")
DATA_DIR = os.path.join(REPO_ROOT, "data")

CATALOG_PATH = os.path.join(DATA_DIR, "catalog.js")
PUBLISHED_PATH = os.path.join(DATA_DIR, "published.js")
QUEUE_PATH = os.path.join(SCRIPT_DIR, "icons.csv")

# Keeps the shuffle reproducible: rebuilding the queue twice gives the same
# order, so a rebuild after an upgrade does not reshuffle what is left.
SHUFFLE_SEED = 20260909

BACKSLASH = chr(92)

# Where the icon names live. 7.3 renamed this from fontawesome.css to all.css
# and minified it; both shapes parse the same way.
NAME_FILES = ("all.css", "fontawesome.css")

# .fa-one,.fa-two{--fa:"\f015"} - the value is read separately, because 7.3
# writes some icons as escaped ASCII (\$, \%) or bare characters ("A") rather
# than always as a hex codepoint.
NAME_RULE = re.compile(
    r"((?:\.fa-[a-z0-9-]+\s*,\s*)*\.fa-[a-z0-9-]+)\s*\{\s*--fa:\s*"
    r'"((?:[^"' + BACKSLASH + BACKSLASH + r']|'
    + BACKSLASH + BACKSLASH + r'.)*)"\s*;?\s*\}'
)
HEX_ESCAPE = re.compile(r"[0-9a-fA-F]{1,6}")
FONT_SRC = re.compile(r"src:\s*url\(\.\./webfonts/([^)]+)\)")
FONT_WEIGHT = re.compile(r"@font-face\{[^}]*?font-weight:\s*(\d+)")
FAMILY_RULE = re.compile(r"([^{}]*)\{[^{}]*--fa-family:\s*var\(--fa-family-[a-z-]+\)")
STYLE_RULE = re.compile(r"([^{}]*)\{[^{}]*--fa-style:\s*\d+")
VERSION = re.compile(r"Font Awesome Pro ([\d.]+)")

# Names that read as a style but are really the family half of the pair.
FAMILY_LIKE = {"fa-classic", "fa-brands"}

# Font weight is the reliable name for a style: a family such as duotone
# declares --fa-style on its own family class, so the class list alone would
# label it "Duotone Duotone".
WEIGHT_NAMES = {
    900: "Solid",
    600: "Semibold",
    400: "Regular",
    300: "Light",
    100: "Thin",
}


def decode_value(value):
    r"""A --fa: value -> codepoint.

    '\f015' is a hex escape, '\$' escapes a literal character, and 'A' is a
    bare one. The letter, digit and punctuation icons added in 7.3 use the
    last two, so reading only hex would silently drop 55 icons.
    """
    if value.startswith(BACKSLASH):
        body = value[1:].rstrip()
        if HEX_ESCAPE.fullmatch(body):
            return int(body, 16)
        return ord(value[1]) if len(value) > 1 else None
    return ord(value) if len(value) == 1 else None


def names_path():
    for name in NAME_FILES:
        candidate = os.path.join(CSS_DIR, name)
        if os.path.exists(candidate):
            return candidate
    raise SystemExit(
        "No icon name stylesheet found. Expected one of: " + ", ".join(NAME_FILES)
    )


def read_names(path):
    """codepoint -> [name, alias, ...] from a name-bearing stylesheet."""
    css = open(path, encoding="utf-8").read()
    found = {}
    for selector, value in NAME_RULE.findall(css):
        codepoint = decode_value(value)
        if codepoint is None:
            continue
        found[codepoint] = re.findall(r"\.fa-([a-z0-9-]+)", selector)
    return found


def classes_in(selector_matches):
    out = []
    for selector in selector_matches:
        out.extend(re.findall(r"\.(fa-[a-z0-9-]+)(?![\w-])", selector))
    return sorted(set(out))


def label_for(classes):
    """'fa-sharp-duotone' -> 'Sharp Duotone'."""
    words = []
    for token in classes.split():
        words.extend(token[3:].split("-"))
    return " ".join(w.capitalize() for w in words)


def read_styles():
    """One entry per installed style, with the codepoints it can draw."""
    styles = []
    for css_path in sorted(glob.glob(os.path.join(CSS_DIR, "*.css"))):
        # These carry the names, and all.css bundles every @font-face, so
        # neither describes a style of its own.
        if os.path.basename(css_path) in NAME_FILES:
            continue

        css = open(css_path, encoding="utf-8").read()
        src = FONT_SRC.search(css)
        if not src:
            continue

        font_path = os.path.join(WEBFONT_DIR, src.group(1))
        if not os.path.exists(font_path):
            print(f"  skip {os.path.basename(css_path)}: missing {src.group(1)}")
            continue

        # Every codepoint the font draws. Filtering to the Private Use Area
        # here would drop the letter, digit and punctuation icons, which sit
        # at their ASCII codepoints; unnamed glyphs are excluded later by
        # intersecting against the names instead.
        font = TTFont(font_path, lazy=True)
        codepoints = set(font.getBestCmap())
        font.close()

        families = classes_in(FAMILY_RULE.findall(css))
        family = next((f for f in families if f != "fa-classic"), "fa-classic")

        weight = FONT_WEIGHT.search(css)
        weight_name = WEIGHT_NAMES.get(int(weight.group(1)) if weight else 0, "Regular")

        style_names = [c for c in classes_in(STYLE_RULE.findall(css))
                       if c not in FAMILY_LIKE and c != family]

        if family == "fa-brands":
            # A family that needs no style class beside it.
            classes, label = "fa-brands", "Brands"
        elif family == "fa-classic":
            classes = style_names[0] if style_names else "fa-solid"
            label = weight_name
        elif style_names:
            classes = f"{family} {style_names[0]}"
            label = f"{label_for(family)} {weight_name}"
        else:
            # e.g. duotone, which sets its own weight on the family class.
            classes = family
            label = f"{label_for(family)} {weight_name}"

        styles.append({
            "classes": classes,
            "label": label,
            "font": src.group(1),
            "codepoints": codepoints,
        })

    # Solid first, then the rest of classic, then brands, then the extra packs.
    preferred = ["fa-solid", "fa-regular", "fa-light", "fa-thin"]

    def sort_key(s):
        if s["classes"] in preferred:
            return (0, preferred.index(s["classes"]))
        if s["classes"] == "fa-brands":
            return (1, 0)
        return (2, s["classes"])

    styles.sort(key=sort_key)
    return styles


def build():
    source = names_path()
    print(f"Reading icon names from {os.path.basename(source)}...")
    named = read_names(source)
    brands = read_names(os.path.join(CSS_DIR, "brands.css"))
    # The name file covers brands too, so the brands sheet is what identifies
    # which codepoints belong to that family.
    classic = {cp: names for cp, names in named.items() if cp not in brands}
    print(f"  classic: {len(classic)} codepoints")
    print(f"  brands:  {len(brands)} codepoints")

    print("Reading webfonts...")
    styles = read_styles()
    for s in styles:
        print(f"  {s['label']:26} {len(s['codepoints']):5} glyphs  ({s['font']})")

    brand_style = next(
        (i for i, s in enumerate(styles) if s["classes"] == "fa-brands"), None
    )

    icons = []
    combinations = 0

    # Brand names come from the shared name file too, which carries their
    # aliases; the brands sheet only says which codepoints are brands.
    brand_named = {cp: named.get(cp, names) for cp, names in brands.items()}

    for group, is_brand in ((classic, False), (brand_named, True)):
        for codepoint, names in group.items():
            mask = 0
            for index, style in enumerate(styles):
                if codepoint not in style["codepoints"]:
                    continue
                if is_brand != (index == brand_style):
                    continue
                mask |= 1 << index

            if not mask:
                continue  # named in CSS but no installed font draws it

            combinations += bin(mask).count("1")
            icons.append({
                "name": names[0],
                "label": names[0].replace("-", " ").title(),
                "unicode": format(codepoint, "x"),
                "aliases": names[1:],
                "mask": mask,
            })

    icons.sort(key=lambda i: i["name"])
    return styles, icons, combinations


def fa_version():
    header = open(names_path(), encoding="utf-8").read(400)
    found = VERSION.search(header)
    return found.group(1) if found else "unknown"


def write_catalog(styles, icons, combinations):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CATALOG_PATH, "w", encoding="utf-8") as f:
        f.write("/* Generated by scripts/build_icon_data.py - do not edit by hand. */\n")
        f.write("window.FA_CATALOG = {\n")
        f.write(f'"version": {json.dumps(fa_version())},\n')
        f.write(f'"generated": {json.dumps(datetime.date.today().isoformat())},\n')
        f.write(f'"iconCount": {len(icons)},\n')
        f.write(f'"combinationCount": {combinations},\n')
        f.write('"styles": [\n')
        f.write(",\n".join(json.dumps([s["classes"], s["label"]]) for s in styles))
        f.write("\n],\n")
        # One icon per line keeps the diff readable if this is ever rebuilt.
        f.write('"icons": {\n')
        f.write(",\n".join(
            json.dumps(i["name"]) + ":" +
            json.dumps([i["label"], i["unicode"], format(i["mask"], "x"), i["aliases"]])
            for i in icons
        ))
        f.write("\n}};\n")
    return os.path.getsize(CATALOG_PATH)


def read_published():
    """The 'name:styleIndex' pairs already on the page."""
    if not os.path.exists(PUBLISHED_PATH):
        return set()
    text = open(PUBLISHED_PATH, encoding="utf-8").read()
    return set(re.findall(r'"([a-z0-9-]+:\d+)"', text))


def write_queue(styles, icons):
    """Every icon-and-style pair that is not published yet, shuffled."""
    published = read_published()
    rows = []
    for icon in icons:
        for index, style in enumerate(styles):
            if not icon["mask"] >> index & 1:
                continue
            key = f"{icon['name']}:{index}"
            if key in published:
                continue
            rows.append([
                icon["name"],
                index,
                f"{style['classes']} fa-{icon['name']}",
                icon["label"],
                style["label"],
            ])

    random.Random(SHUFFLE_SEED).shuffle(rows)

    with open(QUEUE_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["name", "style", "classes", "label", "style_label"])
        writer.writerows(rows)

    return len(rows), len(published)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", action="store_true",
                        help="also rebuild scripts/icons.csv from what is unpublished")
    parser.add_argument("--stats", action="store_true",
                        help="print the summary without writing anything")
    args = parser.parse_args()

    styles, icons, combinations = build()

    print()
    print(f"  Font Awesome Pro {fa_version()}")
    print(f"  {len(icons):>6} unique icons")
    print(f"  {len(styles):>6} styles")
    print(f"  {combinations:>6} icon-style combinations")

    if args.stats:
        return 0

    size = write_catalog(styles, icons, combinations)
    print()
    print(f"Wrote data/catalog.js ({size / 1024:.0f} KB)")

    if args.queue:
        queued, published = write_queue(styles, icons)
        print(f"Wrote scripts/icons.csv ({queued} queued, {published} already published)")
        print(f"  {queued / 45:.0f} days of batches at 45 a day")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
