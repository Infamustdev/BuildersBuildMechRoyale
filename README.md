# oval-sentinel: a Mech Royale bot

Python, standard library only. Speaks `mechfight/1` on stdin/stdout.

## Layout

| File | What it does |
| --- | --- |
| `bot.py` | Entry point. Judgement system and the movement, aiming, shooting and loot state machines. |
| `tuning.py` | Every number the judgement system uses. Tune here. |
| `worldmap.py` | Top-down map from `/arena`: ground and building heights, line of sight, A* paths, cover, roofs. |
| `tracker.py` | Enemy tracks, sound "heat" map of likely enemy positions, and remembered items. |
| `combat.py` | Lead aiming, hit probability (the 65% rule), weapon triggers, and damage models. |
| `mechfight.py` | The kit's protocol wrapper (unchanged, except that it calls `on_end`). |
| `tools/replay.py` | Offline harness: replays the recorded match, prints states and timing. |
| `check/`, `arena/`, `mechfight.schema.json`, `protocol.md` | From the official kit. |

## Test locally

```sh
python check/check.py -- python -u bot.py     # official conformance check
python tools/replay.py --every 10             # decisions on the recorded match
docker build -t my-bot .
python check/check.py --map-dir /arena -- docker run --rm -i --network none --cpus 1 --memory 512m --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 -v "$PWD/arena:/arena:ro" my-bot
```

## Dependencies

None beyond the Python 3.13 standard library. `mechfight.py`, `check/`, `arena/` and the schema come from the
organizers' `mechfight-kit`.

License: MIT.
