# API reference

Generated from the docstrings in the source. Everything in `gpumesh.__all__`
appears here, and nothing else is a supported import path. See
[Stability and compatibility](stability.md) for what that guarantee covers.

The other submodules (`gpumesh.server`, `gpumesh.worker`, `gpumesh.db` and the
rest) are implementation, so this page skips them. Read
[Architecture](architecture.md) for how they fit together and
[The gpumesh protocol](protocol.md) for the wire format between them.

Two names in `__all__` are missing below on purpose. IPython calls
`load_ipython_extension` and `unload_ipython_extension`; you never do. What you
type is `%load_ext gpumesh`, and the README documents the magics it registers.

## The package

```{eval-rst}
.. automodule:: gpumesh
   :members:
   :show-inheritance:
```

## PyTorch helpers

`gpumesh.torch` places local models on devices the current process can see. It
does not shard a model across machines. [Why not Ray or Dask?](why-not-ray-or-dask.md)
says what gpumesh does and does not try to do. Requires the `gpu` extra
(`pip install gpumesh[gpu]`).

```{eval-rst}
.. automodule:: gpumesh.torch
   :members:
   :show-inheritance:
```
