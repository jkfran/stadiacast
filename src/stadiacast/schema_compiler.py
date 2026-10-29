"""Compile the hand-written ``.proto`` schemas into the serialized ``FileDescriptorProto`` files the receiver loads.

``protoc`` is not needed: this is a small proto2 compiler for the subset the hand-written schemas
use (syntax, package, import/option (ignored), message / nested message, enum, oneof, extend /
extensions, required/optional/repeated scalar, message and enum fields, field options
``[default = X, packed = true, deprecated = true]``, reserved).

    python -m stadiacast.schema_compiler            # recompile every hand-written schema
    python -m stadiacast.schema_compiler --check    # fail if a .descriptor is out of date
    python -m stadiacast.schema_compiler SRC.proto DST.descriptor

The firmware-extracted schemas (``input``, ``webrtc_sdp``, ``yeti_clientanalytics``) are not
compiled from text: their descriptors came out of the firmware and the ``.proto`` next to them
is a rendering for reading (see ``schemas/README.md``).
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from google.protobuf import descriptor_pb2, descriptor_pool

FDP = descriptor_pb2.FieldDescriptorProto
SCALARS = {
    "double": FDP.TYPE_DOUBLE,
    "float": FDP.TYPE_FLOAT,
    "int64": FDP.TYPE_INT64,
    "uint64": FDP.TYPE_UINT64,
    "int32": FDP.TYPE_INT32,
    "fixed64": FDP.TYPE_FIXED64,
    "fixed32": FDP.TYPE_FIXED32,
    "bool": FDP.TYPE_BOOL,
    "string": FDP.TYPE_STRING,
    "bytes": FDP.TYPE_BYTES,
    "uint32": FDP.TYPE_UINT32,
    "sfixed32": FDP.TYPE_SFIXED32,
    "sfixed64": FDP.TYPE_SFIXED64,
    "sint32": FDP.TYPE_SINT32,
    "sint64": FDP.TYPE_SINT64,
}
LABELS = {"required": FDP.LABEL_REQUIRED, "optional": FDP.LABEL_OPTIONAL, "repeated": FDP.LABEL_REPEATED}
TOKEN = re.compile(
    r'\s+|//[^\n]*|/\*.*?\*/|"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|\.?[A-Za-z_][A-Za-z0-9_.]*|-?\d+(?:\.\d+)?|[{}\[\]=;,<>()]',
    re.DOTALL,
)


def tokenize(text: str) -> list[str]:
    out, pos = [], 0
    while pos < len(text):
        m = TOKEN.match(text, pos)
        if not m:
            raise SyntaxError(f"unexpected character {text[pos]!r} at offset {pos}")
        tok = m.group(0)
        pos = m.end()
        if tok.isspace() or tok.startswith(("//", "/*")):
            continue
        out.append(tok)
    return out


class Parser:
    def __init__(self, text: str):
        self.toks = tokenize(text)
        self.i = 0
        self.file = descriptor_pb2.FileDescriptorProto()
        self.file.syntax = "proto2"
        self.types: dict[str, str] = {}  # fully-qualified name -> "message" | "enum"
        self.pending: list[tuple[FDP, str, str]] = []  # (field, type text, scope)
        self.pending_extendee: list[tuple[FDP, str, str]] = []  # (extension field, extendee text, scope)

    # ---- token helpers --------------------------------------------------------
    def peek(self, k: int = 0) -> str:
        return self.toks[self.i + k] if self.i + k < len(self.toks) else ""

    def next(self) -> str:
        tok = self.peek()
        self.i += 1
        return tok

    def expect(self, tok: str) -> None:
        got = self.next()
        if got != tok:
            raise SyntaxError(f"expected {tok!r}, got {got!r} (token #{self.i})")

    def skip_statement(self) -> None:
        depth = 0
        while True:
            tok = self.next()
            if tok == "{":
                depth += 1
            elif tok == "}":
                depth -= 1
                if depth <= 0:
                    return
            elif (tok == ";" and depth == 0) or tok == "":
                return

    # ---- grammar ---------------------------------------------------------------
    def parse(self) -> descriptor_pb2.FileDescriptorProto:
        while self.peek():
            tok = self.peek()
            if tok == "syntax":
                self.next()
                self.expect("=")
                self.file.syntax = self.next().strip("\"'")
                self.expect(";")
            elif tok == "package":
                self.next()
                self.file.package = self.next()
                self.expect(";")
            elif tok in ("import", "option"):
                self.skip_statement()
            elif tok == "extend":
                self.extend(self.file.extension, self.file.package)
            elif tok == "message":
                self.message(self.file.message_type.add(), self.file.package)
            elif tok == "enum":
                self.enum(self.file.enum_type.add(), self.file.package)
            elif tok == ";":
                self.next()
            else:
                raise SyntaxError(f"unexpected top-level token {tok!r}")
        self.resolve()
        return self.file

    def message(self, msg: descriptor_pb2.DescriptorProto, scope: str) -> None:
        self.expect("message")
        msg.name = self.next()
        full = f"{scope}.{msg.name}" if scope else msg.name
        self.types[full] = "message"
        self.expect("{")
        while self.peek() != "}":
            tok = self.peek()
            if tok == "message":
                self.message(msg.nested_type.add(), full)
            elif tok == "enum":
                self.enum(msg.enum_type.add(), full)
            elif tok == "oneof":
                self.next()
                idx = len(msg.oneof_decl)
                msg.oneof_decl.add().name = self.next()
                self.expect("{")
                while self.peek() != "}":
                    self.field(msg.field, full, oneof_index=idx)
                self.expect("}")
            elif tok == "extensions":
                self.next()
                r = msg.extension_range.add()
                r.start = int(self.next())
                r.end = r.start + 1
                if self.peek() == "to":
                    self.next()
                    hi = self.next()
                    r.end = (536870911 if hi == "max" else int(hi)) + 1
                self.expect(";")
            elif tok == "extend":
                self.extend(msg.extension, full)
            elif tok in ("option", "reserved"):
                self.skip_statement()
            elif tok == ";":
                self.next()
            else:
                self.field(msg.field, full)
        self.expect("}")

    def extend(self, target: Any, scope: str) -> None:
        """`extend Name { fields }`: fields land in `target` with extendee resolved later."""
        self.expect("extend")
        extendee = self.next()
        self.expect("{")
        while self.peek() != "}":
            if self.peek() == ";":
                self.next()
                continue
            self.field(target, scope, extendee=extendee)
        self.expect("}")

    def field(self, target: Any, scope: str, oneof_index: int | None = None, extendee: str | None = None) -> None:
        f = target.add()
        if extendee is not None:
            self.pending_extendee.append((f, extendee, scope))
        if self.peek() in LABELS:
            f.label = LABELS[self.next()]
        else:
            f.label = FDP.LABEL_OPTIONAL
        type_text = self.next()
        if type_text == "map":
            raise SyntaxError("map fields are not supported")
        f.name = self.next()
        self.expect("=")
        f.number = int(self.next())
        if oneof_index is not None:
            f.oneof_index = oneof_index
        if self.peek() == "[":
            self.next()
            while self.peek() != "]":
                key = self.next()
                self.expect("=")
                val = self.next()
                if key == "default":
                    f.default_value = val.strip("\"'")
                elif key == "packed":
                    f.options.packed = val == "true"
                elif key == "deprecated":
                    f.options.deprecated = val == "true"
                if self.peek() == ",":
                    self.next()
            self.expect("]")
        self.expect(";")
        if type_text in SCALARS:
            f.type = SCALARS[type_text]
        else:
            self.pending.append((f, type_text, scope))

    def enum(self, en: descriptor_pb2.EnumDescriptorProto, scope: str) -> None:
        self.expect("enum")
        en.name = self.next()
        full = f"{scope}.{en.name}" if scope else en.name
        self.types[full] = "enum"
        self.expect("{")
        while self.peek() != "}":
            if self.peek() in ("option", "reserved"):
                if self.peek(1) == "allow_alias":
                    en.options.allow_alias = True
                self.skip_statement()
                continue
            v = en.value.add()
            v.name = self.next()
            self.expect("=")
            v.number = int(self.next())
            if self.peek() == "[":
                while self.next() != "]":
                    pass
            self.expect(";")
        self.expect("}")

    def resolve(self) -> None:
        for f, type_text, scope in self.pending:
            full = self.lookup(type_text, scope)
            f.type_name = "." + full
            f.type = FDP.TYPE_MESSAGE if self.types[full] == "message" else FDP.TYPE_ENUM
        for f, extendee, scope in self.pending_extendee:
            f.extendee = "." + self.lookup(extendee, scope)

    def lookup(self, type_text: str, scope: str) -> str:
        if type_text.startswith("."):
            name = type_text[1:]
            if name in self.types:
                return name
            raise NameError(f"unknown type {type_text}")
        parts = scope.split(".") if scope else []
        while True:
            cand = ".".join([*parts, type_text])
            if cand in self.types:
                return cand
            if not parts:
                raise NameError(f"unknown type {type_text!r} referenced from {scope!r}")
            parts.pop()


def compile_proto(text: str, name: str) -> descriptor_pb2.FileDescriptorProto:
    fdp = Parser(text).parse()
    fdp.name = name
    # Validation: the pool rejects duplicate numbers, bad type references, etc.
    descriptor_pool.DescriptorPool().AddSerializedFile(fdp.SerializeToString())
    return fdp


#: Schemas written by hand from the firmware's nanopb / protobuf-lite tables (compiled from text).
HAND_WRITTEN = ("discovery_api", "rpc_message", "controller_message", "streamer_message")
SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"


def compile_file(src: Path) -> bytes:
    """The serialized FileDescriptorProto of one ``.proto`` file."""
    return compile_proto(src.read_text(encoding="utf-8"), src.name).SerializeToString()


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", nargs="?", type=Path, help="a .proto file (default: every hand-written schema)")
    ap.add_argument("dst", nargs="?", type=Path, help="the .descriptor to write (default: next to src)")
    ap.add_argument("--check", action="store_true", help="only compare; exit 1 when a descriptor is stale")
    a = ap.parse_args(argv)
    pairs = [(a.src, a.dst or a.src.with_suffix(".descriptor"))] if a.src else []
    if not pairs:
        pairs = [(SCHEMA_DIR / f"{n}.proto", SCHEMA_DIR / f"{n}.descriptor") for n in HAND_WRITTEN]
    stale = 0
    for src, dst in pairs:
        data = compile_file(src)
        current = dst.read_bytes() if dst.exists() else None
        if a.check:
            state = "up to date" if current == data else "STALE"
            stale += state == "STALE"
            print(f"{dst}: {state}")
            continue
        if current != data:
            dst.write_bytes(data)
        fdp = descriptor_pb2.FileDescriptorProto()
        fdp.ParseFromString(data)
        msgs = sum(1 + len(m.nested_type) for m in fdp.message_type)
        print(f"{dst}: package {fdp.package!r}, {msgs} message(s) ({'unchanged' if current == data else 'written'})")
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
