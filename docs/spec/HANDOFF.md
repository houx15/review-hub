# Handoff: building Review Engine

This file is for the coding agent (Claude Code) that builds Review Engine in this repo.
Read it fully, then read `review-engine-design.md` in this folder, before writing any code.

## What you are building

A set of agent skills plus a local review UI that help a person review AI-produced work
(research reports, research plans, product specs, writing). The design rationale, architecture,
file layout and roadmap are in `review-engine-design.md`. That document is the source of truth
for *what* to build; this file says *how to proceed*.

The user is a computational social science researcher. She will test the system by using
it in her real work, not on a fixed benchmark. Write documentation and user-facing text in
Chinese unless she asks otherwise; code, identifiers and comments may be in English.

## Hard constraints

- **Skills, not an API service.** Skills run inside the user's existing agent session. Do not add
  code that calls a model API directly, and do not track token or dollar cost. AI-side effort is
  measured as rounds, number of reviewer agents, and wall-clock time.
- **Files are the only interface.** Skills and the UI communicate only by reading and writing files
  in the workspace (see "工作区文件夹" in the design). No database, no daemon that the skills depend on.
- **Local first.** Nothing leaves the machine. No telemetry, no external services.
- **No finding without evidence.** Every finding must point to a concrete location in a source of
  truth (file + line, data file + key, command + output, quoted original text). Findings without
  evidence are dropped, not shown.
- **Stay inside this repo.** Do not modify the user's other projects. When testing on a real
  artifact from another project, copy it into a session folder; never edit the original.

## Phases and gates

Work only on the current phase. At the end of each phase, stop, summarise what you built and what
you learned, and wait for the user's explicit approval before starting the next one. Record decisions
in `docs/spec/DECISIONS.md` (create it) as you go: date, decision, reason, alternatives considered.

### Phase 0: contracts (start here)

1. **Propose the file schemas first, before any other code.** Draft JSON Schemas for `plan.json`
   and `map.json`, and record schemas for `findings.jsonl`, `feedback.jsonl`, `incidents.jsonl`
   and `metrics.json`. Put them in `schemas/`. Write a short `docs/spec/SCHEMAS.md` that explains
   each field with one realistic example per file. **Stop and ask the user to review the schemas.**
   Things the schemas must support (from the design):
   - Decision points use the QOC-E structure: question, options (with the chosen one marked),
     criteria, evidence; plus stage, upstream/downstream links, verifiability (V/J/J+V),
     load-bearing score, and status.
   - Claims carry a source type: computed from data, literature, stated by the user, model inference.
   - Findings carry severity (S1/S2/S3), stage, evidence pointer(s), origin tag (introduced this
     round / from source material or data / inherited from an upstream decision), and status.
   - Feedback events are anchored to a decision point or finding id **and** a text-quote anchor
     (exact text plus prefix/suffix, in the spirit of the W3C Web Annotation TextQuoteSelector),
     and carry one of the fixed reaction types: fixed / AI wrong / intentional / doesn't matter /
     this is a class of problem; or a choice among options; or a free-text answer.
   - Every record has an id, timestamp and session id; sign-off is tied to a hash of the artifact
     version it covers.
2. After approval, write `types/research-report/TYPE.md`: the stage chain from the design, and for
   each stage: what failure looks like, where the ground truth is, V or J, default checks, and when
   the human is involved. Keep it short and concrete, in the spirit of a good `REVIEW.md`.
3. Write the skill texts (`SKILL.md` for each skill in the design's table) as text only. They
   define inputs, outputs (which files, which schema), and steps. No UI yet.
4. Walk through one real artifact by hand with the user (she will pick it): produce the files a
   review would produce, by following the skill texts manually. Adjust schemas if the walk-through
   shows a gap. Phase 0 is done when the formats stop changing.

### Phase 1: self-review without UI

Implement `review-setup`, `review-start` and `review-run` so they work end to end in Claude Code.
Output is the session files plus a readable markdown summary (`summary.md`) that starts with a
count ("S1: 2, S2: 3" or "没有需要你处理的问题"), then the certificate including what was not checked.
Done when it runs on one of the user's real artifacts, she finds at least one finding valuable, and
false findings are no more than half of all findings.

### Later phases

Phases 2–4 (UI, sedimentation, extension) are described in the design. Do not start them until
The user approves the end of Phase 1. Do not scaffold the UI early.

## Open questions to raise with the user when they become relevant

Do not decide these yourself; ask when you reach them.

1. Where the workspace (records, sessions, ledger) lives: inside this repo under a git-ignored
   folder, or a separate location. Needed in Phase 0, step 1.
2. Which agents besides Claude Code to support first. Needed before Phase 4; do not build adapters
   before then.
3. Whether `review-aware` (review during generation) is in the first version.
4. Whether the UI must be reachable from her MacBook Pro while the repo and server run on the Mac
   mini (she often works from the MacBook while the Mac mini does the work). Needed before Phase 2.
5. Whether planted-error calibration is in the first version. It is valuable for the self-experiment
   but inserts errors into her real work, so it must be opt-in.

## Working style

- Small, reviewable steps; commit often with clear messages.
- When you are unsure what the user wants, ask one specific question instead of guessing.
- Prefer fewer, sharper findings. Noise costs her attention, which is the scarce resource this
  whole system exists to protect.
