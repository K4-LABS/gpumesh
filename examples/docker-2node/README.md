# Two-node Docker Compose example

A self-contained two-node `gpumesh` cluster, one coordinator and one worker, running in Docker.

## Starting the cluster

The compose file requires a token and refuses to start without one. Generate a real one rather than inventing a memorable string. The token is the only thing between anyone who can reach the port and code execution as you.

```bash
export GPUMESH_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
docker compose up -d
```

Give the worker a few seconds to connect.

The coordinator publishes on `127.0.0.1` only, so nothing outside this machine can reach it. To let other machines join, set `GPUMESH_BIND=0.0.0.0`, and read the warning the coordinator prints before you do.

## Submitting a job

Submit the `grid_search.py` example to the containerized coordinator. Run this from the root of the repository, in the same shell that has `GPUMESH_TOKEN` set:

```bash
export GPUMESH_URL=http://127.0.0.1:8732

# If gpumesh is not on your PATH, `python -m gpumesh` works the same way
python -m gpumesh submit examples/grid_search.py --payloads examples/payloads.json --wait
```

## Expected output

Six tasks, spread across the mesh and finishing:

```plaintext
[OK] Submitted job dd72a3c2a1cf
Job: examples/grid_search.py (dd72a3c2a1cf)
  Status: finished
  Counts: {'done': 6}

  ✓ task 8f72afe98d07 [done]  cost=1.0  worker=57d91a7f546e
    result: {"lr": 0.01, "epochs": 100, "l2": 0.0, "val_accuracy": 0.948, "weights": [0.2271, -0.2757, 0.0019]}
  ✓ task 994661b4303e [done]  cost=2.0  worker=57d91a7f546e
    result: {"lr": 0.05, "epochs": 200, "l2": 0.0, "val_accuracy": 0.948, "weights": [1.1511, -1.4807, 0.0292]}
  ✓ task 7934837a129b [done]  cost=2.0  worker=57d91a7f546e
    result: {"lr": 0.1, "epochs": 200, "l2": 0.001, "val_accuracy": 0.948, "weights": [1.6146, -2.0946, 0.0232]}
  ✓ task f194479ad4fc [done]  cost=5.0  worker=57d91a7f546e
    result: {"lr": 0.1, "epochs": 500, "l2": 0.0, "val_accuracy": 0.948, "weights": [2.3552, -3.1832, -0.0092]}
  ✓ task d9fc50a2d602 [done]  cost=5.0  worker=57d91a7f546e
    result: {"lr": 0.2, "epochs": 500, "l2": 0.001, "val_accuracy": 0.948, "weights": [2.9428, -3.9689, -0.0151]}
  ✓ task 9a3124f9b8d9 [done]  cost=10.0  worker=5718d2bb6fba
    result: {"lr": 0.3, "epochs": 1000, "l2": 0.01, "val_accuracy": 0.952, "weights": [2.1907, -2.9448, 0.0191]}
```

Task and worker ids will differ on your run. The `val_accuracy` figures will not, because `grid_search.py` is deterministic.

## A note on ports

The two defaults differ, and it catches people out when they write their own client scripts.

- The Docker image listens on **8732**.
- `gpumesh serve` on the host defaults to **8000**.

Both are only defaults. Pass `--port` to use whatever you like, and point workers at the same number the coordinator is listening on.

## Teardown

From this directory:

```bash
docker compose down
```
