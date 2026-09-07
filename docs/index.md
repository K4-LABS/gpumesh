# gpumesh

**Borrow your friends' GPUs.** A distributed compute mesh that lets you share GPU
power across machines on your network, with one decorator, one CLI command, or a
Python API.

Start a **coordinator** on one machine. Join **workers** from every other machine
you have, laptops and desktops and servers, anything with Python. Then run code
across all of them as if they were one device.

```bash
pip install gpumesh          # or gpumesh[all] for every optional extra
gpumesh serve                # coordinator, on 127.0.0.1:8000
gpumesh join <URL> <TOKEN>   # worker, on every other machine
```

```python
from gpumesh import accelerate

@accelerate
def train(config):
    ...

results = train.map(configs)   # runs across the mesh
```

The [README on GitHub](https://github.com/K4-LABS/gpumesh#readme) has the
quickstart, the security model and the limitations. Read that first if you are
new. Each page below takes one topic and goes further than the README does.

```{toctree}
:maxdepth: 2
:caption: Guides

architecture
cli
protocol
mcp
windows
troubleshooting
```

```{toctree}
:maxdepth: 2
:caption: Reference

api
stability
why-not-ray-or-dask
```

```{toctree}
:maxdepth: 1
:caption: Project

Security policy <https://github.com/K4-LABS/gpumesh/blob/master/SECURITY.md>
Threat model <https://github.com/K4-LABS/gpumesh/blob/master/THREAT_MODEL.md>
Contributing <https://github.com/K4-LABS/gpumesh/blob/master/CONTRIBUTING.md>
Changelog <https://github.com/K4-LABS/gpumesh/blob/master/CHANGELOG.md>
Source on GitHub <https://github.com/K4-LABS/gpumesh>
```
