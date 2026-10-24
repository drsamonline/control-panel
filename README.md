# control-panel

**Every workflow in every repository — what runs, when, what calls what, and what is connected to nothing.**

Nothing in this system knew its own shape. There were ~112 workflows across ~64 repositories: reusable
ones, callers, schedules, dispatches — and no single place that answered *"what runs, and what calls
what?"*. This is that place.

It is also the control surface: **every workflow has a Run button**, and they are grouped into the four
stages they actually form — from an idea to a shipped app to the system repairing itself.

## What it holds

| file | what it is |
|---|---|
| `docs/inventory.json` | every workflow, parsed: its triggers, cron, jobs, `needs`, steps, permissions, and every reusable call it makes |
| `docs/plan.json` | the four stages, the routes through them, and the workflows that fit in no stage |
| `docs/index.html` | the panel — the pipeline, the routes, the Run buttons, and what is broken |
| `tools/fetch.py` | pull every repo's workflows into a tree (no secret needed for public repos) |
| `tools/scan.py` | parse the tree into the inventory, and work out who calls whom |
| `tools/plan.py` | turn the inventory into the stages and the routes |

## The four stages

1. **Scratch** — an idea becomes a repo. `intake`, `new-app`, `adopt`.
2. **Build** — a repo becomes an artefact. Every `test`, `ci`, `lint`, and the app pipeline.
3. **Deliver** — an artefact becomes public. `release`, `pages`, `shot`, `app-deliver`, the refreshers.
4. **Repair** — the system keeps itself honest. `self-diagnose`, `self-heal`, `health`, `audit`, `backup`,
   `repo-learning`, `backlog-status`.

## The routes — how little you have to do

| route | you do | then, on its own |
|---|---|---|
| **A new app** | describe it in one line | spec → repo → guards → landing page → three-platform build → release → demo → screenshot |
| **Change an app** | push, or merge a PR | test → build → release → demo → screenshot |
| **Repair** | nothing | diagnose → heal → audit → learn → back up |
| **Ship on demand** | press Run | three builds → release → demo → screenshot |

**The everyday routes ask for at most one thing, and usually nothing.**

## How Forge embeds

Three layers, three jobs:

- **Forge** (private) is the **brain** — the memory (backlog, run log, registry, rules), the tools, and the agent's workspace.
- **This panel** (public) is the **map** — what runs, what calls what, what is missing, and the routes.
- **The dashboard** (public) is the **vitals** — what the system *is*: the products, their health, the conformance scores.

One-way and checkable: **Forge writes the memory → the tools derive the facts → the panel and the dashboard publish them.**
Anyone can run the tools and get the same numbers.

## Why this is the commercial product

You are not selling an app. You are selling **a system that governs itself and can prove it** — one that audits its own
drift, catches its own silent failures, backs up before every merge, and shows its work in public, with the numbers
generated from the repositories rather than typed into a slide. The licence sells **Forge**; the apps stay free, which is
what makes the map worth reading.

## What is missing (tools/gaps.py)

Every check is a rule with a reason — `permissions`, `timeout-minutes`, `concurrency`, drift, orphans, un-runnable
workflows. **DRIFT is the one that already cost a release.**

## The chosen routes (tools/simulate.py)

Candidates are **scored**, not asserted: inputs, hand-actions, how many things must go right, **the chance the output is
wrong**, stage coverage, resumability, and whether the route leans on a hand-copied workflow. The winner per scenario is
the one the numbers choose.

## The Run buttons

Each opens that workflow's page on GitHub — the real dispatch. Nothing here needs a token in a browser,
which is why the panel can be public.

## How it stays true

`scan.yml` runs daily: it re-fetches every workflow, rebuilds the inventory and the plan, runs both
tools' own tests first, and commits the fresh data. The page reads that data, so it cannot drift from
what the workflows actually are.

## Licence

MIT — see `LICENSE`. © 2026 Dr Sohil Momin.
