"""The bundled protobuf schemas: every .descriptor loads, the hand-written ones compile from their .proto
source byte for byte, and every .proto rendering lists exactly the fields its descriptor has."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from google.protobuf import descriptor_pb2, descriptor_pool

from stadiacast.protos import DESCRIPTOR_FILES, Protos, default_proto_dir
from stadiacast.schema_compiler import FDP, HAND_WRITTEN, SCHEMA_DIR, compile_file, compile_proto
from stadiacast.schema_compiler import main as compiler_main

_FIELD = re.compile(r"^(?:(?:required|optional|repeated)\s+)?([\w.]+(?:<[^>]*>)?)\s+(\w+)\s*=\s*(\d+)")
_KEYWORDS = ("syntax", "package", "import", "option", "reserved", "extensions")


def proto_text_fields(text: str) -> dict[str, set[tuple[str, int]]]:
    """``{message full name: {(field name, number)}}`` read from .proto text, nesting-aware."""
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    out: dict[str, set[tuple[str, int]]] = {}
    stack: list[tuple[str, str]] = []  # (kind, name)
    for tok in re.finditer(r"\{|\}|[^{};]+(?=\{)|[^{};]+;", text):
        t = tok.group(0).strip()
        if t == "{":
            continue
        if t == "}":
            stack.pop()
            continue
        if t.endswith(";"):
            stmt = t[:-1].strip()
            if not stmt or stmt.split()[0] in _KEYWORDS or not stack:
                continue
            owner = next((name for kind, name in reversed(stack) if kind == "message"), None)
            kind = stack[-1][0]
            if kind in ("enum", "extend") or owner is None:
                continue
            m = _FIELD.match(stmt)
            if m:
                out.setdefault(owner, set()).add((m.group(2), int(m.group(3))))
            continue
        kind, _, name = t.partition(" ")
        name = name.strip()
        if kind == "message":
            parents = [n for k, n in stack if k == "message"]
            full = f"{parents[-1]}.{name}" if parents else name
            out.setdefault(full, set())
            stack.append((kind, full))
        else:
            stack.append((kind, name))
    return out


def descriptor_fields(fdp: descriptor_pb2.FileDescriptorProto) -> dict[str, set[tuple[str, int]]]:
    out: dict[str, set[tuple[str, int]]] = {}

    def walk(msgs, prefix=""):
        for m in msgs:
            full = f"{prefix}{m.name}"
            out[full] = {(f.name, f.number) for f in m.field}
            walk(m.nested_type, full + ".")

    walk(fdp.message_type)
    return out


def test_every_descriptor_loads_into_one_pool():
    p = Protos()
    assert p.ControllerToDisplay().DESCRIPTOR.full_name == "gotham.display.ControllerToDisplay"
    assert sorted(DESCRIPTOR_FILES) == sorted(f.name for f in Path(default_proto_dir()).glob("*.descriptor"))
    assert Path(default_proto_dir()) == SCHEMA_DIR


@pytest.mark.parametrize("name", HAND_WRITTEN)
def test_hand_written_schema_compiles_to_the_committed_descriptor(name):
    assert compile_file(SCHEMA_DIR / f"{name}.proto") == (SCHEMA_DIR / f"{name}.descriptor").read_bytes(), (
        f"{name}.descriptor is stale: run python -m stadiacast.schema_compiler"
    )


@pytest.mark.parametrize("descriptor", sorted(SCHEMA_DIR.glob("*.descriptor")), ids=lambda p: p.stem)
def test_proto_rendering_lists_exactly_the_descriptor_fields(descriptor: Path):
    fdp = descriptor_pb2.FileDescriptorProto()
    fdp.ParseFromString(descriptor.read_bytes())
    want = descriptor_fields(fdp)
    have = proto_text_fields(descriptor.with_suffix(".proto").read_text(encoding="utf-8"))
    assert have == want


SMALL_PROTO = """
syntax = "proto2";
package t;
import "nanopb.proto";
option java_package = "example";

enum Kind {
  option allow_alias = true;
  A = 0;
  B = 1;
  C = 1 [deprecated = true];
}

message Outer {
  required string name = 1;
  optional int32 n = 2 [default = 7];
  repeated uint32 xs = 3 [packed = true];
  optional Kind kind = 4;
  optional Inner inner = 5;
  oneof body {
    bytes raw = 6;
    .t.Outer.Inner msg = 7;
  }
  extensions 100 to max;
  reserved 50;
  message Inner {
    optional bool flag = 1 [deprecated = true];
  }
}

extend Outer {
  optional string ext = 100;
}
"""


def test_compiler_covers_the_proto2_subset_the_schemas_use():
    fdp = compile_proto(SMALL_PROTO, "small.proto")
    assert (fdp.name, fdp.package, fdp.syntax) == ("small.proto", "t", "proto2")
    (kind,) = fdp.enum_type
    assert kind.options.allow_alias is True
    assert [(v.name, v.number) for v in kind.value] == [("A", 0), ("B", 1), ("C", 1)]
    (outer,) = fdp.message_type
    fields = {f.name: f for f in outer.field}
    assert fields["name"].label == FDP.LABEL_REQUIRED
    assert fields["name"].type == FDP.TYPE_STRING
    assert fields["n"].default_value == "7"
    assert fields["xs"].label == FDP.LABEL_REPEATED
    assert fields["xs"].options.packed is True
    assert (fields["kind"].type, fields["kind"].type_name) == (FDP.TYPE_ENUM, ".t.Kind")
    assert (fields["inner"].type, fields["inner"].type_name) == (FDP.TYPE_MESSAGE, ".t.Outer.Inner")
    assert [d.name for d in outer.oneof_decl] == ["body"]
    assert fields["raw"].oneof_index == 0
    assert fields["msg"].oneof_index == 0
    assert fields["msg"].type_name == ".t.Outer.Inner"
    assert not fields["name"].HasField("oneof_index")
    assert [(r.start, r.end) for r in outer.extension_range] == [(100, 536870912)]
    (inner,) = outer.nested_type
    assert inner.field[0].options.deprecated is True
    (ext,) = fdp.extension
    assert (ext.name, ext.number, ext.extendee) == ("ext", 100, ".t.Outer")
    # the serialized form round-trips through a real pool
    pool = descriptor_pool.DescriptorPool()
    pool.AddSerializedFile(fdp.SerializeToString())
    assert pool.FindMessageTypeByName("t.Outer").fields_by_name["kind"].enum_type.full_name == "t.Kind"


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("message M { map<string, int32> m = 1; }", SyntaxError),
        ("message M { optional Nope n = 1; }", NameError),
        ("message M { optional .other.T n = 1; }", NameError),
        ("service S {}", SyntaxError),
        ("message M { optional int32 n 1; }", SyntaxError),
        ("message M { optional int32 n = 1; optional int32 m = 1; }", TypeError),  # the pool rejects a duplicate number
    ],
)
def test_compiler_rejects_what_it_does_not_support(text, error):
    with pytest.raises(error):
        compile_proto(text, "bad.proto")


def test_compiler_command_line_writes_and_checks(tmp_path, capsys):
    src = tmp_path / "small.proto"
    dst = tmp_path / "small.descriptor"
    src.write_text(SMALL_PROTO, encoding="utf-8")
    assert compiler_main([str(src)]) == 0
    assert dst.read_bytes() == compile_file(src)
    assert "written" in capsys.readouterr().out
    assert compiler_main([str(src), str(dst), "--check"]) == 0
    assert "up to date" in capsys.readouterr().out
    dst.write_bytes(b"stale")
    assert compiler_main([str(src), str(dst), "--check"]) == 1
    assert "STALE" in capsys.readouterr().out
    assert compiler_main([str(src), str(dst)]) == 0
    assert dst.read_bytes() == compile_file(src)
