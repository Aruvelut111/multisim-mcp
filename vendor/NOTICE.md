# Third-party code

`msmodel.py` and `msbuild.py` are kept here so the package works out of the
box. They implement the schematic object model (two parallel trees, pin
geometry, value mirrors) and the assembly layer (cloning symbols from donor
designs, routing wires, placing probes), and are distributed under the same
MIT license as the rest of this repository.

`server.py` only uses a few entry points from this directory:

- `msmodel.Circuit.from_xml_file(path)`
- `msbuild.Assembler(base_path=...)`
- `Assembler.add_part(family, part, refdes, value, x, y)`
- `Assembler.connect(pins, name=..., tap=...)` / `Assembler.connect_more(...)`

Everything else (compression container, COM bridge, MCP surface) is
implemented in `multisim_mcp/`.
