# Checks a PR title is a Conventional Commit header; its type decides the release bump (docs/CI.md).
import re
import sys

ALLOWED_TYPES = ("feat", "fix", "perf", "refactor", "docs", "test", "ci", "build", "chore", "revert", "style")

TITLE_PATTERN = re.compile(r"(?P<type>[a-z]+)(\([^()\s]+\))?!?: \S.*")

FORMAT_PROBLEM = 'title must look like "type(scope): subject" or "type: subject", e.g. "fix(upload): retry on 503"'


def title_problems(title: str) -> list[str]:
    match = TITLE_PATTERN.fullmatch(title.strip())
    if match is None:
        return [FORMAT_PROBLEM]
    if match["type"] not in ALLOWED_TYPES:
        return [f'unknown type "{match["type"]}"; use one of: {", ".join(ALLOWED_TYPES)}']
    return []


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print('usage: python pr_title.py "<pr title>"', file=sys.stderr)
        return 2
    problems = title_problems(argv[0])
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
