"""Fill the panel vote template: question, frozen rubric, blind bundle, and an optional runoff note.
usage: panel_vote.py TEMPLATE RUBRIC BUNDLE OUT [RUNOFF_NOTE_FILE]"""
import sys
from pathlib import Path

template, rubric, bundle, out, *runoff = sys.argv[1:]
q = (Path.home() / "outbreak/seminar/question.md").read_text().strip()
note = Path(runoff[0]).read_text().strip() + "\n\n" if runoff else ""
text = Path(template).read_text()
for k, v in {"{question}": q, "{rubric}": Path(rubric).read_text().strip(),
             "{bundle}": Path(bundle).read_text().strip(), "{runoff}": note}.items():
    assert k in text, k
    text = text.replace(k, v)
Path(out).write_text(text)
print("wrote", out, len(text), "chars")
