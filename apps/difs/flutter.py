"""Exact image data for symbolication of obfuscated Flutter (Dart) stacks.

An Android Flutter app built with ``--obfuscate --split-debug-info`` sends
Dart frames as bare ``instruction_addr`` values: no ``image_addr`` per frame
and no ``debug_meta`` entry for the Dart snapshot (libapp.so). Exact base
recovery matters here because the snapshot's symbol file is a 10-20+ MB ELF:
the probing fallback (``_estimate_image_base``) never scans far enough to
reach the base, and even where it reaches one, any page-aligned slide scores
just as well while mapping every frame to unrelated functions. Plausible but
wrong names are worse than unresolved addresses.

When the SDK cannot parse the Dart stack itself, it sends a raw textual dump
whose trailing pseudo-frames carry the exact snapshot parameters (hex, no
``0x`` prefix):

    isolate_instructions: 789379ca80, vm_instructions: 7893786000
    isolate_dso_base: 7893236000, vm_dso_base: 7893236000
    build_id: 'b7188509241222671ab8064247a69c76'
    android arch: arm64 comp: yes sim: no

These values are constant for the lifetime of the process, so a client can
also capture them once at startup (any StackTrace in an obfuscated build
carries the footer) and attach them to every event as a ``dart_image``
context — including events where the SDK parsed the stack and dropped the
footer. The context is read first; the textual footer is the fallback that
covers background isolates and builds that don't set the context. Context
values are hex strings as in the footer; a ``0x`` prefix is tolerated.

The build_id equals the code_id of the matching ``app.<platform>-<arch>.symbols``
upload, which lets the DIF query pick the exact file instead of trying every
file in the project.
"""

import re

# Frames of the textual footer: itself plus the "*** *** ***" separator.
_FOOTER_FRAME_RE = re.compile(
    r"^(?:isolate_dso_base:|isolate_instructions:|build_id:|pid:|android arch:|\*{3})"
)
_DSO_BASE_RE = re.compile(
    r"isolate_dso_base:\s*([0-9a-fA-F]+),\s*vm_dso_base:\s*([0-9a-fA-F]+)"
)
_INSTR_RE = re.compile(
    r"isolate_instructions:\s*([0-9a-fA-F]+),\s*vm_instructions:\s*([0-9a-fA-F]+)"
)
_BUILD_ID_RE = re.compile(r"build_id:\s*'([0-9a-fA-F]+)'")
_ARCH_RE = re.compile(r"arch:\s*(\S+)")

# Keys of the parsed info dict that hold image addresses.
_IMAGE_INFO_ADDR_KEYS = (
    "isolate_dso_base",
    "vm_dso_base",
    "isolate_instructions",
    "vm_instructions",
)


def _parse_hex(value) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip().strip("'"), 16)
        except ValueError:
            return None
    return None


def _iter_frames(event_json):
    for value in (event_json.get("exception") or {}).get("values") or []:
        for frame in (value.get("stacktrace") or {}).get("frames") or []:
            if frame:
                yield frame


def _info_from_contexts(event_json) -> dict:
    """Snapshot parameters from a ``contexts.dart_image`` dict, if present."""
    image = (event_json.get("contexts") or {}).get("dart_image")
    if not isinstance(image, dict):
        return {}
    info = {}
    for key in _IMAGE_INFO_ADDR_KEYS:
        parsed = _parse_hex(image.get(key))
        if parsed is not None:
            info[key] = parsed
    build_id = image.get("build_id")
    if isinstance(build_id, str) and build_id.strip():
        info["build_id"] = build_id.strip().strip("'").lower()
    arch = image.get("arch")
    if isinstance(arch, str) and arch.strip():
        info["arch"] = arch.strip()
    return info


def parse_dart_image_info(event_json) -> dict | None:
    """Extract the Dart snapshot's base addresses, build_id and arch.

    Sources, by priority: the ``contexts.dart_image`` context (attached by
    the client app) and the pseudo-frames of the textual stack footer.
    Returns a dict of image addresses (ints), build_id (hex, no dashes) and
    arch (str), or None when the event carries neither source.
    """
    info = _info_from_contexts(event_json)
    for frame in _iter_frames(event_json):
        function = frame.get("function") or ""
        m = _DSO_BASE_RE.search(function)
        if m and "isolate_dso_base" not in info:
            info["isolate_dso_base"] = int(m.group(1), 16)
            info["vm_dso_base"] = int(m.group(2), 16)
        m = _INSTR_RE.search(function)
        if m and "isolate_instructions" not in info:
            info["isolate_instructions"] = int(m.group(1), 16)
            info["vm_instructions"] = int(m.group(2), 16)
        m = _BUILD_ID_RE.search(function)
        if m and "build_id" not in info:
            info["build_id"] = m.group(1).lower()
        # "os: android arch: arm64 ..." is the only textual arch source;
        # Flutter events have no contexts.device.arch to fall back on.
        if frame.get("abs_path") == "os:" and "arch" not in info:
            m = _ARCH_RE.search(function)
            if m:
                info["arch"] = m.group(1)
    return info or None


def dart_image_base(info: dict, instruction_addr: int | None) -> int | None:
    """The snapshot base an instruction address belongs to, or None.

    Application (isolate) code addresses count from isolate_instructions /
    isolate_dso_base, VM code from vm_instructions / vm_dso_base. An address
    below both boundaries belongs to another image (libflutter.so, system
    libraries) that the snapshot's DIF cannot resolve.
    """
    if not info or instruction_addr is None:
        return None
    iso_base = info.get("isolate_dso_base")
    iso_start = info.get("isolate_instructions")
    if iso_base and iso_start and instruction_addr >= iso_start:
        return iso_base
    vm_base = info.get("vm_dso_base")
    vm_start = info.get("vm_instructions")
    if vm_base and vm_start and instruction_addr >= vm_start:
        return vm_base
    # Bounds not provided: when the two bases are equal, either serves. A
    # lookup at an offset outside the file simply misses — it cannot yield a
    # false name the way a mis-estimated base can.
    if iso_base and iso_base == vm_base:
        return iso_base if instruction_addr >= iso_base else None
    return None


def is_dart_footer_frame(frame) -> bool:
    """A pseudo-frame of the textual footer (bases, build_id, pid, "***")."""
    return bool(_FOOTER_FRAME_RE.match((frame or {}).get("function") or ""))


def has_dart_frames(frames) -> bool:
    """True when any frame is marked platform 'dart' (a Dart stack)."""
    return any(f.get("platform") == "dart" for f in (frames or []) if f)
