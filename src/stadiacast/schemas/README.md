# Protobuf schemas

The receiver loads the `.descriptor` files (serialized `FileDescriptorProto`) into a
`DescriptorPool` at runtime (`stadiacast.protos`), so no `protoc` step is needed. The
`.proto` file next to each descriptor is the human-readable rendering of the same schema.

| schema | origin | package | authority |
|---|---|---|---|
| `webrtc_sdp` | descriptor embedded in the firmware | `chrome.cloudcast.proto.api.common` | the `.descriptor`; the `.proto` is a rendering |
| `input` | descriptor embedded in the firmware | `chrome.cloudcast.streaming` | the `.descriptor`; the `.proto` is a rendering |
| `yeti_clientanalytics` | descriptor embedded in the firmware | `chrome.cloudcast.proto.common` | the `.descriptor`; the `.proto` is a rendering |
| `discovery_api` | written by hand from the firmware's nanopb field tables | `gotham.discovery` | the `.proto`; compiled to the `.descriptor` |
| `rpc_message` | written by hand from the firmware's protobuf-lite classes | `gotham.rpc` | the `.proto`; compiled |
| `controller_message` | written by hand from the firmware's nanopb field tables | `gotham.display` | the `.proto`; compiled |
| `streamer_message` | written by hand from the firmware's nanopb field tables | `gotham.streamer` | the `.proto`; compiled |

For the hand-written schemas, field **numbers, wire types, sizes and nesting** are the
firmware's; every **name** is ours, chosen from how the firmware uses the field. Fields whose
use is not known yet are called `fN` / `requestN` / `messageN`. The `gotham.*` package names
are ours too.

The three schemas reproduced from embedded descriptors are Google's work, included solely so
that an independently created program can interoperate with the stock firmware; they are not
covered by the project's MIT License (see the repository's `NOTICE`). The four hand-written
schemas are ours and MIT-licensed.

## Recompiling

```sh
python -m stadiacast.schema_compiler            # rewrite the four hand-written .descriptor files
python -m stadiacast.schema_compiler --check    # exit 1 when one is stale (CI runs this)
```

`stadiacast.schema_compiler` is a small proto2 compiler covering exactly the subset these
files use. `tests/test_schemas.py` checks that the compiled bytes match the committed
descriptors and that every `.proto` rendering lists exactly the fields of its descriptor, so
the two never drift apart.
