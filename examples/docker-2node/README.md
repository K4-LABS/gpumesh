# 2-Node Docker Compose Example

This directory contains a self-contained, working example of a two-node `gpumesh` cluster (one coordinator and one worker) running in Docker.

## 🚀 Starting the Cluster

Start the cluster in detached mode from this directory:

```bash
docker compose up -d
```

(Wait a few seconds for the worker to connect to the coordinator).

## 🛠️ Submitting a Job

Once the cluster is running, you can submit the `grid_search.py` example to the containerized coordinator.

Run this command from the root of the repository:

```bash
export GPUMESH_URL=http://127.0.0.1:8732
export GPUMESH_TOKEN=my-secret-mesh-token

# If gpumesh is not in your PATH, use `python -m gpumesh`
python -m gpumesh submit examples/grid_search.py --payloads examples/payloads.json --wait
```

## ✅ Expected Output

A successful run will indicate that the tasks were distributed and completed across the mesh. You should see output exactly like this:

```plaintext
============================================================
  gpumesh installed successfully!
  version 2.0.0
============================================================

  Get started in one command:

    gpumesh setup

  This will detect your hardware and guide you
  through choosing coordinator or worker role.
  Or start a coordinator directly:
    gpumesh serve --token your-secret-token

============================================================

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

## ⚠️ Important Note on Ports

If you are writing your own client scripts, please note the port configuration differences:

- This Docker image defaults to port 8732.
- Running the coordinator locally on your host via `gpumesh serve` defaults to port 8000.

## 🛑 Teardown

To stop and remove the containers, run this from the `examples/docker-2node/` directory:

```bash
docker compose down
```
