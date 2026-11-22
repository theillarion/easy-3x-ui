"""Print the changelog entry for an existing release tag."""

import argparse
import re
from pathlib import Path

VERSION = r"(?:0|[1-9][0-9]*)"


def release_notes(tag: str, changelog: str) -> str:
    """Return the body of the matching dated changelog section."""
    if not re.fullmatch(rf"v{VERSION}\.{VERSION}\.{VERSION}", tag):
        raise ValueError("Expected a release tag in the form vX.Y.Z.")

    heading = rf"## \[{re.escape(tag[1:])}\] - [0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}"
    sections = re.split(r"(?m)^(?=## )", changelog)
    for section in sections:
        title, _, body = section.partition("\n")
        if re.fullmatch(heading, title) and body.strip():
            return body.strip()
    raise ValueError(
        f"Add a dated [{tag[1:]}] section with release notes to CHANGELOG.md."
    )


def main() -> None:
    """Write notes to standard output for GitHub CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    args = parser.parse_args()
    try:
        print(release_notes(args.tag, Path("CHANGELOG.md").read_text(encoding="utf-8")))
    except (OSError, ValueError) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
