"""Developer-mode example: connect once, then write normal Python.

This is the "code normally" flow that works in VS Code, Jupyter, PyCharm,
or a plain terminal:

  1. One machine runs a coordinator (and its own CPU/GPU joins the pool):
         gpumesh serve --token mysecret

  2. Friends join as workers (their CPU/GPU joins the pool):
         gpumesh join http://<coordinator-ip>:8000 --token mysecret

  3. Now, ANY of those machines can run this script. Functions marked
     with @mesh run on the whole pool; everything else stays normal Python.

Run it:
    python examples/dev_mode.py
"""

from gpumesh import mesh

# --- Connect ----------------------------------------------------------
# Auto-connects from the config saved by `gpumesh join` / `gpumesh serve`.
# If nothing is saved, connect explicitly:
#     mesh.connect("http://127.0.0.1:8000", token="mysecret")
# (If not connected, @mesh runs locally — your code never breaks.)


# --- Write normal functions; mark the heavy ones ----------------------
@mesh
def preprocess(chunk_id, rows):
    """Pretend-heavy function — runs on the best available device."""
    return {"chunk": chunk_id, "rows": rows, "rows_squared": rows * rows}


@mesh
def train(lr, epochs):
    """Another mesh function — imagine real training here."""
    return {"lr": lr, "epochs": epochs, "accuracy": 0.5 + lr * epochs / 100}


# --- Use them exactly like normal Python ------------------------------
if __name__ == "__main__":
    # Single call → runs somewhere in the pool (your machine + friends).
    print("single call:", preprocess(chunk_id=1, rows=10))

    # .map() → spread across EVERY machine in the pool at once.
    results = preprocess.map([
        {"chunk_id": i, "rows": 100 * (i + 1)} for i in range(6)
    ])
    print("map results:", results)

    # Sweep some hyperparameters over the whole pool.
    sweep = train.map([
        {"lr": 0.01, "epochs": 100},
        {"lr": 0.05, "epochs": 200},
        {"lr": 0.10, "epochs": 500},
    ])
    print("sweep:", sweep)

    print()
    print("Done. Your pool handled it — no job submission needed.")
