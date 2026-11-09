"""Fill a panel-round template with the revealed takes of an earlier panel seminar, shuffled and scrubbed.
usage: make_round.py FROM_RUN TEMPLATE OUT LABEL   (LABEL names the placeholder items, e.g. Draft or Proposal)"""
import secrets
import sys
from pathlib import Path

from adapters.seminar import SCRUB, Seminar, _name_re
from core.store import Store, canon

run, template, out, label = sys.argv[1:5]
R = Path.home() / "research-stack/runs"
sem = Seminar(Store(R, sink=None), run)
takes = sem.takes()
order = list(sem.roles)
secrets.SystemRandom().shuffle(order)
scrub = _name_re([*SCRUB, *sem._names()])
keep = R / ".sealed" / f"{run}-next-order.json"
assert not keep.exists(), f"{keep} exists; an order was already drawn"
items = "\n\n".join(f"===== {label} {i} =====\n{scrub.sub('[model]', takes[r]['take'])}" for i, r in enumerate(order, 1))
assert not scrub.search(items)
q = (Path.home() / "outbreak/seminar/question.md").read_text().strip()
text = Path(template).read_text().replace("{question}", q).replace("{drafts}", items).replace("{proposals}", items)
keep.write_bytes(canon({"order": order}))
Path(out).write_text(text)
print("wrote", out, len(text), "chars; order sealed in", keep.name)
