"""Derive the User Guide Markdown from README.md for PDF rendering.

`make pdf` builds a single PDF, docs/user-guide.pdf, from the project README so
the README stays the one source of truth. The User Guide is the README with a
few edits that only make sense for a standalone printed document:

  * a pandoc title block (title / subtitle / author) is prepended, so the PDF
    gets Labelmaker's LaTeX title page instead of a bare "Labelmaker" heading;
  * the top-level "# Labelmaker" heading is replaced by an "# Introduction"
    heading (the title block already carries the Labelmaker title), so the intro
    paragraph and hero image get their own numbered section after the contents;
  * the line that points at the PDF is dropped — inside the PDF it would be
    pointing at itself;
  * the whole "# Installation" section is dropped, because the User Guide is
    distributed to people who already have Labelmaker installed.

Usage:
    python3 docs/pandoc/build_user_guide_md.py README.md docs/.build/user-guide.md
"""
import sys

TITLE_BLOCK = """\
---
title: "Labelmaker — User Guide"
subtitle: "Features, configuration, and preferences"
author: "Labelmaker"
---
"""

INTRO_HEADING = "# Labelmaker"
INSTALL_HEADING = "# Installation"
SELF_REFERENCE_MARKER = "docs/user-guide.pdf"


def build_user_guide(readme_text):
    output_lines = [TITLE_BLOCK]
    is_inside_install_section = False

    for line in readme_text.splitlines():
        is_top_level_heading = line.startswith("# ")

        if is_inside_install_section:
            # The install section runs until the next top-level heading.
            if is_top_level_heading and line != INSTALL_HEADING:
                is_inside_install_section = False
            else:
                continue

        if line == INSTALL_HEADING:
            is_inside_install_section = True
            continue

        if line == INTRO_HEADING:
            # The title block already carries the Labelmaker title, so give the
            # intro paragraph and hero image their own "Introduction" section.
            output_lines.append("# Introduction")
            continue

        if SELF_REFERENCE_MARKER in line:
            continue

        output_lines.append(line)

    return "\n".join(output_lines) + "\n"


def main():
    readme_path, output_path = sys.argv[1], sys.argv[2]
    with open(readme_path, encoding="utf-8") as readme_file:
        readme_text = readme_file.read()
    with open(output_path, "w", encoding="utf-8") as output_file:
        output_file.write(build_user_guide(readme_text))


if __name__ == "__main__":
    main()
