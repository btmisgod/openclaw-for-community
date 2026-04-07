# Newsflow Workflow Redesign

## 1. Scope

This document replaces the earlier mixed-role, template-heavy workflow design for `newsflow-workflow`.

The target state is:

- clearer agent role boundaries
- separate product production flow and retrospective flow
- preserve the current machine-readable output fields that the server already recognizes
- reduce hard-coded writing templates that force agents to repeat the same conclusions across cycles

This document is the current source of truth for workflow redesign in this repository.

## 2. Design Principles

### 2.1 Preserve machine-readable fields

The server already recognizes the current structured output fields. Do not break that contract unless there is a deliberate migration plan.

The redesign goal is:

- keep machine-readable structure
- loosen human-readable expression

In plain language:

- the server still needs structured fields
- agent prose should not be forced into fixed wording, fixed examples, or fixed headings

### 2.2 Define output boundaries, not writing templates

Each workflow phase should define:

- phase goal
- actor
- required coverage
- gate condition
- downstream artifact

It should not define:

- exact sentence pattern
- exact heading names in the visible text
- fixed number of problems or suggestions unless the business rule really requires it
- canned example prose as the normal path

### 2.3 Separate responsibility domains

The previous design overloaded too many responsibilities onto the same agents. The redesign splits the work into:

- project management
- editorial integration
- section collection
- quality checking
- product evaluation
- retrospective moderation

### 2.4 Use artifacts and gates, not only chat turns

Important steps must produce real artifacts and real gate checks:

- approved material set
- draft
- proofread issues
- revised draft
- final report
- product usability report
- benchmark report
- cross-cycle comparison report
- retrospective plan
- retrospective report
- agent optimization instructions

## 3. Role Boundaries

## 3.1 Manager

Manager is responsible for:

- creating and starting each cycle
- injecting cycle goals, section assignment, standards, and priority
- reading last-cycle retrospective report and per-agent optimization instructions
- updating cycle-level standards based on previous optimization conclusions
- resolving disagreements
- moderating retrospective discussion
- aggregating tester reports into a retrospective plan
- identifying at least five concrete product problems and at least two concrete agent behavior problems
- forcing issue-focused discussion and responsibility clarification
- generating the final retrospective report
- generating agent-specific optimization instructions for the next cycle

Manager is not the primary:

- draft author
- proofreader
- product tester
- benchmark author

## 3.2 Editor

Editor is responsible for:

- integrating approved materials into the draft
- deciding main story / secondary story / brief allocation
- writing draft and revised draft
- producing final report
- ensuring structure, fields, links, dates, and images are complete and correctly placed
- updating editorial behavior based on retrospective optimization suggestions

Editor is not responsible for:

- material collection
- material pre-review
- product evaluation
- retrospective moderation

## 3.3 Worker-33 and Worker-xhs

Each worker is responsible for:

- collecting candidate materials for assigned sections
- submitting structured material packs
- participating in retrospective as a production participant
- proposing improvements to product, process, and peer behavior during retrospective
- updating own behavior based on retrospective optimization suggestions

Workers are not responsible for:

- deciding final page hierarchy
- producing final draft
- owning product evaluation reports
- moderating retrospective

## 3.4 Tester

Tester is responsible for:

- pre-editor material review
- draft proofreading
- final product usability testing
- benchmark analysis against external products
- cross-cycle comparison analysis against the previous cycle

Tester is the quality supervisor. Tester is not the producer of the final artifact.

## 4. Product Workflow (No Retrospective)

The product workflow ends at final publish. Retrospective starts after this chain is completed.

## 4.1 Project Start

Actor: `Manager`

Actions:

- create new cycle
- inject current-cycle goal, section assignment, delivery standard, priority
- read previous retrospective report and prior agent optimization instructions
- write effective optimization context into the new cycle

Outputs:

- cycle plan
- section assignment
- current standard set
- effective optimization context

## 4.2 Material Collection

Actors: `Worker-33`, `Worker-xhs`

Actions:

- collect candidate material pools by section
- each material item should include:
  - title
  - source
  - published time
  - original link
  - image candidates
  - short relevance note

Important rule:

- material stage does not pre-label main/secondary/brief
- editor decides final placement later

Outputs:

- section material packs

## 4.3 Material Review

Actor: `Tester`

Actions:

- review worker-submitted materials before editorial integration
- reject unqualified material
- pass qualified material into editorial stage

Review focus:

- timeliness
- relevance
- authenticity
- text-image consistency
- brief-to-source consistency
- material usability
- correct section ownership

Outputs:

- approved material set
- rejected material notes

## 4.4 Draft Integration

Actor: `Editor`

Actions:

- integrate approved material into draft
- decide main / secondary / brief structure

Output structure per section:

- 1 main story, 3 images, around 200 Chinese characters of brief text
- 2 secondary stories, 1 image each, around 100 Chinese characters each
- 7 briefs, around 50 Chinese characters each
- source / published time / original link retained as structured metadata

Outputs:

- `draft_v1`

## 4.5 Draft Proofreading

Actor: `Tester`

Actions:

- proofread `draft_v1`

Proofreading checks:

- consistency with approved materials
- correctness of title / source / link / published time
- correct image display
- correct section placement
- correct main / secondary / brief placement
- missing items, wrong mapping, duplication

Outputs:

- proofread issues

Important rule:

- this phase checks draft correctness
- this phase is not product usability testing

## 4.6 Draft Revision

Actor: `Editor`

Actions:

- revise the draft according to tester issues

If issues remain, enter a loop:

1. tester raises issue
2. editor revises
3. tester rechecks
4. continue until blocker count is zero

Outputs:

- revised draft

## 4.7 Final Publish

Actors:

- `Editor`: final artifact handoff
- `Manager`: publish gate approval

Publish gate conditions:

- all blockers closed
- recheck passed
- structure complete
- required metadata complete
- image / link / source / published time all present

Outputs:

- final report

## 5. Retrospective Preparation

These steps happen after final publish and before retrospective discussion.

## 5.1 Product Usability Report

Actor: `Tester`

Actions:

- evaluate the final product from a unified reader / product-experience perspective
- write a formal product usability report

The report must make clear:

- the most visible problems in the final product
- what hurts reading continuity most
- what should be fixed first
- why those problems matter to the reader

Outputs:

- product usability report

## 5.2 Benchmark Report

Actor: `Tester`

Actions:

- collect similar products from the web
- compare this cycle's final product against them
- point out the most important gaps only

Outputs:

- benchmark report

## 5.3 Cross-Cycle Comparison Report

Actor: `Tester`

Actions:

- compare this cycle's final product with:
  - previous cycle final product
  - previous cycle retrospective report

Judgment scope:

- which problems improved
- which did not improve
- what became worse
- which prior optimization suggestions were not actually implemented

Outputs:

- cross-cycle comparison report

## 6. Retrospective Workflow

## 6.1 Retrospective Plan

Actor: `Manager`

Inputs:

- product usability report
- benchmark report
- cross-cycle comparison report
- actual project execution evidence
- task history
- proofreading and publish evidence

Actions:

- aggregate tester's three reports
- combine them with actual project evidence
- produce a retrospective plan

Plan requirements:

- at least five product problems
- at least two agent behavior problems
- every problem must be evidence-backed
- product problems must point to a concrete object
- agent behavior problems must point to a concrete agent and a concrete behavior

Examples of concrete product objects:

- a section
- a main story
- an image group
- a summary
- a structure issue

Examples of concrete agent behavior issues:

- worker-33 made main-story judgment too late in technology
- editor failed to apply last cycle's lead-sentence rule in secondary placement

Priority classification should be explicit:

- `P0`: must fix immediately
- `P1`: next-cycle high priority
- `P2`: tracked observation

Outputs:

- retrospective plan

Important rule:

- manager may infer and prioritize
- manager may not invent unsupported issues

## 6.2 Retrospective Discussion

Actor: `Manager` moderates

Participants:

- `Editor`
- `Worker-33`
- `Worker-xhs`
- `Tester`

Actions:

- manager opens one concrete issue at a time from the retrospective plan
- participants discuss that issue
- manager pushes until disagreement, cause, ownership, and next action are clear
- only then move to the next issue
- continue until retrospective time ends

Discussion rules:

- not round-robin reporting
- not checklist recitation
- discussion must stay tied to concrete evidence and concrete issues
- participants may criticize product issues, process issues, and peer behavior
- participants may disagree with manager if they have evidence
- summary does not start before discussion time ends

Outputs:

- retrospective thread

## 6.3 Retrospective Report

Actor: `Manager`

Inputs:

- retrospective plan
- retrospective thread
- tester's three reports
- actual execution outcomes

Actions:

- generate the formal retrospective report

The report should include:

- core product problems
- core execution problems
- root causes
- accepted improvements
- rejected suggestions and reasons
- next-cycle action items
- per-agent responsibility allocation

Outputs:

- retrospective report

## 6.4 Agent Optimization Instructions

Actor: `Manager`

Actions:

- generate targeted optimization instructions for each participating agent
- ensure the instructions are written into next-cycle context and rules

Outputs:

- agent-specific optimization instructions

## 6.5 Enter Next Cycle

Actor: `Manager`

Actions:

- inject the optimization instructions into the next cycle
- start next cycle

Outputs:

- new cycle start

## 7. Output Contract Principle

The workflow should specify what an agent's output must cover, but should not prescribe the exact wording.

Correct approach:

- keep server-readable fields stable
- define output goal and required coverage
- allow free natural-language expression in the human-readable portion

Incorrect approach:

- fixed visible headings everywhere
- fixed sentence examples
- fixed wording templates
- using fallback example prose as the normal path

In plain language:

- the server needs structured fields
- the agent should not be forced to sound like a form letter

## 8. Migration Priority

Recommended implementation order:

1. keep the current machine-readable output contract stable
2. enforce strict proofread recheck validators for real issue types
3. move product-test semantics to unified reader perspective
4. reduce rigid local prose templates in product test / product report / retrospective
5. align retrospective moderation with evidence-backed issue-by-issue discussion
6. write manager-issued optimization instructions back into next-cycle context
