# Data-flow video

A Remotion composition that walks through `BatchRunner.run()` in the order the
code actually executes it. Every figure on screen is real output from
`python -m recouper.cli run --no-llm` on seed 42 — the numbers in
`src/theme.ts` are copied from `runs/track.json`, not invented for the video.

```bash
npm install
npm start     # studio, with a scrubber
npm run build # out/dataflow.mp4 — 55s, 1920x1080, ~1 min to render
npm run still # a single frame, for slides
```

## Scenes

| # | Stage | Source it describes |
|---|---|---|
| 01 | Extract, and deduplicate | `detect/score.py` |
| 02 | Classify the failure | `detect/classify.py` |
| 03 | Split treated / control | `runner.py::_assign_arms` |
| 04 | Propose, then gate | `agent/plan.py` → `policy/engine.py` |
| 05 | Append to the chain | `audit/ledger.py` |
| 06 | Measure the difference | `eval/replay.py` → `eval/metrics.py` |

Scene 04 is the one that matters and gets the most time: it is the only place
the project's central claim is visible as a mechanism rather than a sentence.

## Keeping it honest

If the pipeline changes, re-run the CLI and update `src/theme.ts` from the new
`runs/track.json`. A video whose numbers have drifted from the code is worse
than no video.
