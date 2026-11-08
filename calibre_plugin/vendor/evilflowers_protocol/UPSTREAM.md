# Vendored: evilflowers-protocol-python

- Repository: https://github.com/EvilFlowersCatalog/evilflowers-protocol-python
- Commit: `04ab5cdb594aad5bca25e80fc06942b4d2101345`

Only the stdlib layers are vendored. Calibre bundles its own Python without
`pydantic`, and `pydantic_core` is a compiled extension that cannot be imported
from a plugin ZIP, so the generated models (`schema.py`) and the overlay
(`forms.py`) stay upstream.

| File | Change |
| --- | --- |
| `errors.py` | verbatim |
| `_version.py` | verbatim |
| `transport.py` | `scripts/transport-no-pydantic.patch`: absolute imports made relative; `BaseModel`/`ValidationError` replaced by the `Model` (`from_dict`) and `Payload` (`to_dict`) protocols |

The plugin's models (`calibre_plugins.evilflowers.remote.models`) are plain
dataclasses that implement those protocols.

Refresh with:

```shell
python3 scripts/vendor_protocol.py /path/to/evilflowers-protocol-python
```
