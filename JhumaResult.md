Jhuma’s result
Next step
Resubmit and reach the pass line of 80/100. The current result does not count as passed.

Move the schema out of the prompt text so the instructions and field meanings stand on their own.
Record in your session log the decisions you made on scope, model choice and uncertain source facts.
Measure one change against an honest baseline and record what the assistant contributed.
Feedback
Your application covers the core extraction path well, with structured output enforced at the provider request and extracted facts kept tied to the source messages. The main gap is the prompt, which still carries the full schema text alongside the task instructions. Your session log also did not show your own decisions about scope or model choice, so ownership of the work is hard to see from the record.

This challenge

What you built
35 of 36 met
Your request builder in intake/http_provider.py sends the schema through the structured output setting, which is the right place for it. The prompt builder in intake/prompts.py also pastes the full schema into the system message, so the instructions and the schema travel together. Keep the instructions and field meanings in the prompt and let the provider setting carry the schema alone. Your prompt does well to keep extracted facts tied to what the requester actually wrote.

How you directed the work
0 of 20 met
Your session logs did not show this yet. Include the AI session logs that cover your work when you submit.

Course modules

How large language models produce output
1 of 1 met
Your work shows a clear grasp of how output length is bounded and validated before any request goes out.

IRS policy on AI use and the approved tools
0 of 2 met
This submission did not show evidence for this module yet.

Prompt engineering
1 of 2 met
Your extraction uses zero-shot instructions with the structured output setting, so the model returns a direct JSON object without worked examples or step-by-step reasoning. The work did not use function calling, so the output contract relies on the schema setting rather than tool definitions. Adding a short comparison of the two approaches in your notes would show the difference clearly.

The working environment
2 of 4 met
Your commit history is checked by tools/validate_submission.py, which confirms each implementation followed its test commit. Your session did not show fetching remote changes or reviewing diffs before a commit, which are habits to build next. Your coding assistant applied patches and generated tests as you directed it, and the record shows that clearly.

The AI-assisted loop
2 of 2 met
Your session shows a dependable loop from directing the assistant through tests to a commit, backed by a validation gate that checks the repository before shipping.

Team repository, version control and peer review
0 of 2 met
This submission did not show evidence for this module yet.

AI-assisted Python
2 of 3 met
Your Python shows solid use of conditionals and comprehensions. You directed the assistant step by step through test-first work. The change was not measured against a baseline, so the record does not show how much the assistant added.

Reading a generated change
0 of 1 met
This submission did not show evidence for this module yet.

Test automation with AI
0 of 1 met
This submission did not show evidence for this module yet.

Reading a return as XML
0 of 1 met
This submission did not show evidence for this module yet.

Reading return XML in tested Python
0 of 1 met
This submission did not show evidence for this module yet.

Checkpoints
Of the challenge’s 56 judged checkpoints, by category. Skipped checkpoints are left out.

Artifact-What the submitted files show-35 of 36 met
Artifact: 35 met, 1 not met, 0 with no evidence
Ownership-How the work was directed, from the AI session logs-0 of 20 met
Ownership: 0 met, 0 not met, 20 with no evidence

Related capabilities
Cumulative program evidence at this report date, assessed separately from this challenge’s score.

How LLMs work, the approved tools, and the AI-assisted loop-7/15 checkpoints proven
AI-assisted Python development and AI-driven test automation-3/13 checkpoints proven
