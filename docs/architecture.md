# Architecture

```
                         COORDINATOR
        ┌─────────────────────────────────────────────────┐
        │                                                 │
        │  ┌──────────┐  ┌──────────┐  ┌──────────────┐  │
        │  │ Job Queue │  │ Task DB  │  │ Worker       │  │
        │  │ (memory)  │  │ (SQLite) │  │ Registry     │  │
        │  └────┬─────┘  └──────────┘  └──────┬───────┘  │
        │       │                              │          │
        │       └──────────┬───────────────────┘          │
        │                  │                              │
        │         HTTP API :8000                          │
        └──────────────────┼──────────────────────────────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
        ┌─────▼────┐ ┌────▼────┐ ┌────▼────┐
        │ Worker 1 │ │Worker 2 │ │Worker 3 │
        │ RTX 4090 │ │RTX 3080 │ │   T4    │
        │Score: 120│ │Score: 85│ │Score: 12│
        └──────────┘ └─────────┘ └─────────┘
              │            │            │
              └────────────┼────────────┘
                           │
                    ┌──────▼──────┐
                    │   Results   │
                    │  Collected  │
                    └─────────────┘

  JOB FLOW:  Submit ─► Queue ─► Claim ─► Execute ─► Report ─► Collect
```

**How it works:** jobs are stored in SQLite, workers pull tasks over HTTP with a lease (so a crashed worker's task is automatically re-queued), run each task in an isolated subprocess, and post results back. Workers are scored by a benchmark and the scheduler assigns heavier tasks to stronger workers.
