# Panel deliberation protocol (draft, 2026-11-09)

Replaces the panel vote. Li asked for a collaborative seminar in which the three judges work together to a final finding, with no voting, designed against unfair advantage and gaming.

## What the earlier process showed
- Single judge (7f395d): tied 50/50, so a judgment call decided it.
- Panel vote (4eeb73): won 2 of 3. Each judge's first choice could be swayed by self-preference.
- Rubric step (54e17b, 5f1d3c):
  - In both orders, the first-choice votes of two of the three judges followed position.
  - A unanimity rule gave one judge, the author of the winning rubric, an effective veto.
  - Word limits were not enforced, so one judge had more space than the others.

## Safeguards
1. **No winner vote.** The finding is a set of claims. Ranking claims compete like any other claim, and "not separable" is a valid finding.
2. **Simultaneous sealed rounds.** Every judge commits before anyone reveals (ADR-0009), so no one anchors on a first speaker.
3. **Blind on both sides.**
   - Takes get fresh labels W–Z, committed before round 1.
   - Judges appear to each other as Judge 1–3, reshuffled each round.
   - Unblinding happens only after the finding is fixed.
4. **Position counterbalanced.** Every judge gets the same prompt bytes, and the take order is reversed in each successive round.
5. **Equal space.** Any text over the word limit is cut at the limit, marked "[cut at N words]", before other judges see it.
6. **Reasons, not numbers.**
   - An objection must cite a packet table or a specific argument.
   - A judge who changes position must name the argument that changed it. "The others agree" is not a reason.
7. **No veto.** One objection demotes a claim from consensus to majority-with-dissent; it does not delete it. The dissent is filed verbatim.
8. **Mechanical conductor.** The conductor's own take (W) is in the set, so the conductor does no editing. Pooling, shuffling and counting are scripts whose inputs and outputs are hashed in the seminar record.
9. **Self-preference audit afterwards.** The sealed judge identities let us check whether claims favouring a take came from the same model family.
10. **Pre-committed seat rule.** If a seat is blocked (for example by a classifier), the round continues with two seats and this is disclosed. Nobody is re-asked.

## Rounds
Every round is an ordinary seminar with three seats and the full packet inline.

| Round | Each judge writes | Limit |
|---|---|---|
| R1 Assess | For each take: number checks (table, match or fail), strengths and errors. Then the judge's own finding on the study. | 1,200 |
| R2 Cross-examine | Responses to the anonymised R1 assessments: hold or change, with reasons, and numbers that fail against the packet. | 900 |
| R3 Propose claims | Up to 12 numbered claims for the joint finding: what the study shows, how the takes compare, and open uncertainties with the post-pilot work for each. | 900 |
| R4 Endorse | For each pooled claim: ENDORSE, AMEND (with new wording) or OBJECT (with a reason). | 900 |
| R5 Settle amendments | For each amended claim: pick one wording or OBJECT. | 600 |
| R6 Reflect | After the finding is fixed and the history above is revealed: what the results mean, and which review method produces the most rigorous findings, with what to change. | 900 |

## Output
- **Consensus:** claims all three endorse.
- **Majority:** claims two endorse, each with the dissent filed verbatim.
- **Appendix:** dropped claims.
- **Reflections:** the three R6 texts, side by side.

## Cost and time (estimate)
- Argon: about $0.10–0.15 per round, about $0.6–0.9 in total. Fable and Astra run on plan usage.
- Wall-clock: about 10 minutes per round, about 1–1.5 hours in total.
