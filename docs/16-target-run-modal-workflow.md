# Target / Run modal workflow

## Status

Accepted interaction contract for the target-first command deck.

## Domain model

```text
Target (durable problem record)
├── metadata and reusable operator context
├── Direction A
│   ├── Run 1 (independent agent swarm)
│   └── Run 2 (independent agent swarm)
└── Direction B
    └── Run 3 (independent agent swarm)
```

- A **Target** is the durable record for one challenge or engagement. It owns the problem description, address, mode, category, flag settings, reusable hints, attachments, directions, and verified-knowledge projection.
- A **Direction** is a reusable business-level exploration branch. It is not the scheduler's canonical worker class. The selected primary direction is an initial focus; Reason may dispatch workers to other directions when evidence warrants it.
- A **Run** is one independent agent swarm attempt. It belongs to exactly one Target and may select one Target direction as its primary focus.
- Run execution and evidence remain authoritative in the append-only event log and SharedGraph. Target data is operator context and never bypasses the provenance gate.

## New Target modal

Every `New target` entry opens a modal. It must not mint a draft Run.

Required fields:

- Target name
- Problem description

Optional/configuration fields:

- mode: CTF or pentest
- category: web, pwn, crypto, reverse, or misc
- target address
- flag-format hint
- single-flag or multi-flag behavior and expected count
- initial primary direction and its description

Creation is atomic from the operator's perspective. `POST /api/targets` accepts the optional `primary_direction` object and persists the Target plus its first direction in one store flush. A failed Target request leaves no UI-selected Target. A successful request selects the durable Target but does not create a Run.

## Target editing

`Target details` remains a modal for quick inspection and editing. The operator can edit:

- name
- mode
- category
- problem description
- target address
- flag-format hint
- single/multi-flag settings
- primary direction
- additional directions
- reusable Target hints

Edits affect future Run launches. They do not rewrite historical Run event logs or accepted evidence.

## New Run modal

Every `New run` entry on a Target opens a modal. The modal creates and launches one independent agent swarm.

The primary step contains:

- immutable Target identity preview
- selected primary direction, defaulting to the Target primary direction
- per-Run instruction, initially populated from the Target description
- Target-hint inclusion control
- inline creation of an additional Target direction

Advanced steps contain:

- worker profile
- Reason, Worker, Review, and Titler provider/model bindings
- Review policy switch

Bindings are frozen at launch. Credentials remain server-side and are never copied into Target or Run public metadata.

## Run context contract

A Target-bound `/api/runs/{run_id}/start` composes the worker-visible context in this order:

```text
[RUN INSTRUCTION]
<operator text for this Run>

[TARGET DESCRIPTION]
<latest durable Target description>

[TARGET DIRECTIONS]
Primary is the initial focus; Reason may dispatch workers to any listed direction.
- <selected direction> [PRIMARY]: <description>
- <other direction>: <description>

[READ-ONLY VERIFIED FACTS FROM PRIOR RUNS]
...

[OPERATOR HINTS FROM TARGET CARD]
...
```

The backend also fills missing challenge identity from the Target:

- name
- category
- target address
- mode
- flag-format hint
- expected flag count and multi-flag mode
- Target attachments

The per-Run instruction is intentionally independent from the Target description. Editing it never mutates the Target. Updating the Target affects future Runs, while already-started Runs keep their remembered dispatch snapshot.

## Direction semantics

Target directions are business-level exploration branches such as `Authentication bypass` or `Binary protocol analysis`.

They must not be written directly into `Challenge.direction`, because that field is the canonical low-level worker-routing class (`web`, `pwn`, `crypto`, and similar registered directions). Instead, Target directions are included in the Reason context. Reason remains free to emit typed intents for any valid worker class and route.

## UI rules

- Use modals for create/edit operations; keep the main workspace quiet.
- Modal titles are 20px and section headings are 10-12px.
- Target creation and Run launch have one obvious primary action.
- Advanced provider/model configuration is not shown in the first Run step.
- Escape and backdrop click close an idle modal; neither closes during submission.
- Mobile layouts use one column, a bottom-aligned modal, and no horizontal overflow.
- A Target with zero Runs remains visible and editable.

## Acceptance criteria

1. Clicking `New target` always opens the Target modal and creates no Run before submit.
2. A Target can be created with an initial primary direction in one request.
3. After Target creation, its workspace is selected and still shows zero Runs.
4. Clicking `New run` always opens the Run modal for the selected Target.
5. The Run instruction defaults to the Target description and remains independently editable.
6. The selected Target primary direction is preselected.
7. Reason receives the selected direction and the other Target directions as context.
8. The launched Run receives the latest Target metadata plus the per-Run instruction.
9. Target edits affect future Run launches without rewriting prior Run history.
10. The provenance gate, append-only graph, binding secrecy, and flag-completion semantics remain unchanged.
