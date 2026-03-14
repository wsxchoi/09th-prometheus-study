#!/usr/bin/env python3
"""
Converts backtick-wrapped file paths in markdown files to GitHub links.
e.g. `path/to/file.go` becomes [`path/to/file.go`](https://github.com/prometheus/prometheus/blob/release-3.10/path/to/file.go)

Usage:
    python scripts/linkify.py code_analysis/Week2_The_Head_Block.md
    python scripts/linkify.py code_analysis/*.md
"""

import re
import sys

BASE_URL = "https://github.com/prometheus/prometheus/blob/release-3.10"
FILE_EXTENSIONS = (
    ".go", ".yaml", ".yml", ".json", ".toml", ".proto",
    ".sh", ".md", ".txt", ".cfg", ".conf",
)


def parse_file_ref(text):
    """Parse a file reference, returning (path, line_spec) or None.

    Supports:
        path/to/file.go          -> ("path/to/file.go", None)
        path/to/file.go:42       -> ("path/to/file.go", "42")
        path/to/file.go:10-20    -> ("path/to/file.go", "10-20")
    """
    # Split on the first colon that follows a file extension
    m = re.match(r"^(.+?(?:" + "|".join(re.escape(ext) for ext in FILE_EXTENSIONS) + r"))(?::(\d+(?:-\d+)?))?$", text)
    if m and "/" in m.group(1):
        return m.group(1), m.group(2)
    return None


def linkify_line(line):
    """Replace `path/to/file.go` with [`path/to/file.go`](github_url) if not already inside a link."""
    if line.strip().startswith("```"):
        return line

    def replace_match(m):
        raw = m.group(1)
        parsed = parse_file_ref(raw)

        if parsed is None:
            return m.group(0)

        path, line_spec = parsed
        url_path = path.lstrip("/")
        url = f"{BASE_URL}/{url_path}"

        if line_spec:
            if "-" in line_spec:
                start, end = line_spec.split("-", 1)
                url += f"#L{start}-L{end}"
            else:
                url += f"#L{line_spec}"

        return f"[`{raw}`]({url})"

    # Match backtick-wrapped paths that are NOT already inside a markdown link [...](...).
    # Negative lookbehind: not preceded by [  |  Negative lookahead: not followed by ](
    result = re.sub(r"(?<!\[)`([^`]+)`(?!\]\()", replace_match, line)
    return result


def process_file(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()

    in_code_block = False
    new_lines = []
    for line in lines:
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
        if in_code_block:
            new_lines.append(line)
        else:
            new_lines.append(linkify_line(line))

    with open(filepath, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    print(f"Processed: {filepath}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/linkify.py <file.md> [file2.md ...]")
        sys.exit(1)

    for path in sys.argv[1:]:
        process_file(path)
