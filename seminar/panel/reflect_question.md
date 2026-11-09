You are one of three judges on a review panel for the Outbreak study. The panel's decisions are finished, and nothing you write here changes them. This round asks for your reflection.

Below are the question the four takes answered, a factual history of the review, and a draft protocol proposed for review after the pilot. After them comes the packet, every file in full.

----- question the takes answered -----
{question}
----- end question -----

----- review history -----
The takes. Four written interpretations of the results: W was written by the AI agent that ran the study (the "conductor"); X, Y and Z were written by three other models. In this history the judges are Judge 1, Judge 2 and Judge 3. Judge 1 is from the same model family as the author of Z, Judge 2 from the same family as the author of X, and Judge 3 from a family with no take in the set.

Step 1, single judge. One model judged the takes alone, as A and B in that run, which were Z and Y. It scored both 50 of 50, called Y the better survey, and broke the tie for Z because Z worked one computation through in full. The study's lead then replaced this verdict with a panel.

Step 2, rubric by consensus, before any judge saw a take.
- Round 1: each judge drafted a rubric.
- Round 2: each judge was to merge the drafts into one proposal. Judge 1's write was blocked by a safety classifier, and the round continued with two proposals: Judge 2's and Judge 3's.
- Round 3: a proposal needed ACCEPT from all three judges. Judge 2's proposal was listed first. All three accepted it and all three named it first choice. Judge 3's proposal was accepted by Judges 1 and 3 and rejected by Judge 2.
- Order check: round 3 was rerun with the order reversed, so Judge 3's proposal was listed first. Judge 2 again rejected Judge 3's proposal, with partly different reasons. Judges 1 and 3 again accepted both and again named the first-listed proposal first choice. Judge 2's proposal stayed the only one all three accepted.
- The proposals were unlabelled in both runs. Judge 2's proposal became the rubric, unedited.

Step 3, vote. Blind labels W to Z, committed in advance. Each judge scored every take on the rubric, checked numbers against the packet and cast a first-place vote; 2 of 3 votes won.
- First place: Judge 1 Y, Judge 2 X, Judge 3 Y. Y won 2 of 3, so no runoff was needed.
- Rankings: Judge 1 Y, Z, X, W. Judge 2 X, Y, Z, W. Judge 3 Y, X, Z, W. Every judge placed W last.
- Weighted totals out of 5 (Judge 1 / 2 / 3): W 4.1 / 3.6 / 3.8; X 4.2 / 4.6 / 4.7; Y 4.6 / 4.5 / 5.0; Z 4.6 / 3.9 / 4.3.
- Stated confidence in first place: Judge 1 0.6, Judge 2 0.78, Judge 3 0.93.

Other facts.
- Judge 3 exceeded the word limit in every round, and its texts were passed on as written.
- The tool that bundled the takes added an extra "Stated confidence" line to every take; Judge 2 penalised it on every take equally.
- No rubric was tried on a practice take before the vote.
----- end review history -----

----- draft protocol for post-pilot review -----
{protocol}
----- end draft protocol -----

Write your reflection under exactly these headings:
1. The results. What the packet's results do and do not establish. Name the two findings you would put most weight on and the one you would put least, each with the table it rests on.
2. The review so far. For the single judge, the rubric step with its order check, and the vote: what each did well and badly at producing a rigorous finding. Use the history above as evidence.
3. The draft protocol. What it fixes, what it misses, what you would change, and whether any safeguard opens a new way to game it.
4. The most rigorous review. Describe the review process you think would produce the most rigorous finding for a study like this on a hackathon budget, and what evidence would show that it works better than the vote.
5. Uncertainties. Where you are unsure, and what work after the pilot would raise confidence.

Rules: plain text, at most 900 words. Do not say who or what you are, and do not guess which judge you were. End with one line: "Confidence: <a number from 0 to 1>". It means your confidence that the process in section 4 would produce a more rigorous finding than the vote did.
