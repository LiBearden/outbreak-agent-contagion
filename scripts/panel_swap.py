"""Reverse the order of the two proposals in a filled round-3 question file; every other byte stays the same.
usage: panel_swap.py IN OUT"""
import sys
from pathlib import Path

src, out = sys.argv[1:3]
text = Path(src).read_text()
head, rest = text.split("----- round-2 proposals -----\n", 1)
body, tail = rest.split("\n----- end round-2 proposals -----", 1)
assert body.startswith("===== Proposal 1 =====\n") and body.count("===== Proposal 2 =====") == 1
a, b = body[len("===== Proposal 1 =====\n"):].split("\n\n===== Proposal 2 =====\n")
new = f"===== Proposal 1 =====\n{b}\n\n===== Proposal 2 =====\n{a}"
swapped = f"{head}----- round-2 proposals -----\n{new}\n----- end round-2 proposals -----{tail}"
assert len(swapped) == len(text) and sorted(swapped) == sorted(text) and swapped != text
Path(out).write_text(swapped)
print("wrote", out, len(swapped), "chars; proposals reversed")
