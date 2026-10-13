# Agent join prompt

Canonical short prompt: repo [README.md](../README.md) → **Copy-paste：Agent install prompt**.

Executable details live in:

- [`install.sh`](../install.sh) — clone/bootstrap + calls join helper
- [`join_peer.py`](../join_peer.py) — peer JSON / upsert peers.json + prints `NEXT STEPS`

```sh
PEER_NAME='…' LOCAL_FEED_URL='http://…:7788' AGGREGATOR=A sh install.sh
# join only:
PEER_NAME='…' LOCAL_FEED_URL='http://…:7788' AGGREGATOR=B sh install.sh --join-only
```
