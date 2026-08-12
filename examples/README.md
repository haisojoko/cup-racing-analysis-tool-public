# Example memory files (fictional)

Everything in this folder is **made up**. The drivers (Nova, Vega, Comet), the
pundit (Scoop), and the interviewer (Ivy) are fictional and do not correspond to
any real person. These files exist only to show the *format* the tool reads and
writes for its qualitative features, since the real league's profiles and
sources are private and not shipped in this repo.

The four source/profile types the tool understands:

| File | Type | Lives at (runtime) |
|---|---|---|
| `sample-profile.md` | driver profile (built over time) | `memory/profiles/<driver>.md` |
| `sample-interview.md` | interview transcript/summary | `memory/sources/interviews/` |
| `sample-pundit-take.md` | biased media voice, with a stated lean | `memory/sources/punditry/` |
| `sample-race-event.md` | race-day report | `memory/sources/events/` |

## Try the qualitative features

The `memory/profiles/` and `memory/sources/` directories are gitignored (they
hold private, runtime-generated data), so a fresh clone starts empty. To see the
psychology / source features work, copy these fictional files in:

```sh
mkdir -p memory/profiles memory/sources/interviews memory/sources/punditry memory/sources/events
cp examples/sample-profile.md      memory/profiles/nova.md
cp examples/sample-interview.md    memory/sources/interviews/
cp examples/sample-pundit-take.md  memory/sources/punditry/
cp examples/sample-race-event.md   memory/sources/events/
```

Then ask the tool something like *"Give me a psychological read on Nova"* — it
will combine these qualitative sources with the quantitative signals from the
race data. (The stat features — `query_sql`, detectors, season context — work
against the real race data in `data/` without any of this.)
