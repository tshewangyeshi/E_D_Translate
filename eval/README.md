# Frozen evaluation set (S10.1, NFR-202)

`eval/frozen/` holds real government text and is **git-ignored**: this
repository is public. Move it to a private repository or protected path once
the pilot has one.

| File | What |
|---|---|
| `frozen/segments.jsonl` | 800 segments from the G2C portal snapshot, all three tiers (`proposed_tier`, to be confirmed by a person), one `reference` slot each for a human Dzongkha translation |
| `frozen/trend.csv` | one line per nightly run: tag integrity, entity preservation, chrF++ |
| `frozen/last-run.jsonl` | the last run's outcome per segment, for reading why one fell back |

## Rules

- **Frozen.** Never used to tune the masker, the prompts or the glossary. A
  pattern change driven by a segment here makes the set no longer a fair
  test; build a new one instead and say so in the commit.
- **Rebuilt only on purpose:** `python tools/build_eval_set.py` (from the
  snapshot made by `tools/g2c_demo/scrape.py`). Record the date and reason.
- **References** come from DCDD or GovTech translators. Until they exist,
  chrF++ is reported as missing, not as zero.

## Running

```bash
python tools/nightly_gate.py --limit 40   # a sample, sparing the quota
python tools/nightly_gate.py              # the whole set: the nightly run
```

It fails (exit 1) when tag integrity is below 99% (NFR-200) or anything that
would be served lacks one of its protected values (NFR-201). Scheduling it
nightly needs a host with the GovTech credentials: the VM, once it is ready.

First sample run, 2026-10-06, 40 segments against staging: 33 served,
tag integrity 100%, entity preservation 100%; 7 refused (values at the start
of a sentence, list numbers glued to text). After fixing both, the same 40:
39 served, 100% / 100%.
