# multisim-mcp

An MCP server that lets an AI agent build and simulate circuits in Multisim 14 — no mouse, no GUI automation.

## What it does

The agent describes a circuit (components, values, nets), and the server:

1. **Builds the design file** (`.ms14`) directly, including symbols, wires, probes, and virtual instruments.
2. **Runs the simulation** through Multisim's COM automation interface in a hidden instance.
3. **Returns numbers and a schematic image**: operating point, AC sweep, transient waveforms, PNG/JPG of the drawing.

Everything is structured JSON in, structured JSON out, with typed error codes so the agent can correct itself.

## Why not just use the COM API

Multisim's automation interface can open, modify, simulate, and export a design — but it has no method to *place* a component, draw a wire, or add a probe. The design has to exist first.

So the server builds designs itself: a `.ms14` file is a compressed XML document describing the schematic. The server generates that XML from a plain description, then hands the file to Multisim for simulation.

Two paths, one pipeline:

```
spec (JSON)  ──build──▶  .ms14  ──COM──▶  simulation  ──▶  results (JSON)
```

## Requirements

- Windows
- Multisim 14.0 or later
- Two Python installs:
  - **64-bit** for the MCP host (this is what your MCP client launches)
  - **32-bit** for the COM bridge — Multisim registers its automation server only in the 32-bit registry view

`runtime/` is a local scratch location and is not part of the repo. Pick one
of these two routes — the differences matter:

**Route A — installer Python (has the `py` launcher and pip):**

```bat
py -3.11-32 -m pip install pywin32
set RUNTIME_PY32=C:\Path\To\Python311-32\python.exe
```

**Route B — 32-bit embeddable package** (registers no `py` launcher and ships
no pip). Unpack it into `<repo>\runtime\py32\` so the default path resolves,
then bootstrap pip inside it and install pywin32:

```bat
runtime\py32\python.exe get-pip.py
runtime\py32\python.exe -m pip install pywin32
```

Without a 32-bit interpreter at the configured path the COM bridge exits with
`NO_PY32`; without `pywin32` it fails on `import pythoncom`.

## Install

```bat
git clone https://github.com/Aruvelut111/multisim-mcp
cd multisim-mcp
pip install -e .
```

Then bootstrap the donor/template pack from .ms14 files on your machine
(NI sample designs, your own saved circuits — anything you have the
right to use). NI-derived design files are **not** distributed with this
repository:

```bat
python tools\bootstrap_donors.py --scan "C:\path\with\.ms14\designs"
```

This produces `data\blank.ms14`, `data\donors\*.ms14` (probe donor, the
three instrument donors, one donor per component family) and
`data\SOURCES.md` documenting where each file came from.

## Configure your MCP client

```json
{
  "mcpServers": {
    "multisim": {
      "command": "python",
      "args": ["-m", "multisim_mcp"]
    }
  }
}
```

Environment variables (all optional):

| Variable | Default | Purpose |
|---|---|---|
| `RUNTIME_PY32` | `<root>/runtime/py32/python.exe` | 32-bit interpreter for the COM bridge |
| `MSIM_BLANK` | `<root>/data/blank.ms14` | empty schematic used as the build base |
| `MSIM_DONORS` | `<root>/data/donors` | donor designs to clone component symbols from |
| `MSIM_CACHE` | `<root>/.cache` | extracted blank XML and other build caches |
| `MSIM_NO_REAP` | unset | set to `1` to stop the server from cleaning up orphaned Multisim simulation-server processes |

## Tools

### Building

| Tool | Purpose |
|---|---|
| `msim_new_circuit` | Start an empty design |
| `msim_add_part` | Place a component (`refdes`, `kind`, `value`, `x`, `y`) |
| `msim_connect` | Create a net from a list of pins, optionally with a probe |
| `msim_save` | Write the design to `.ms14` |
| `msim_build` | Do all of the above from one spec dict |
| `msim_list_parts` | List available component kinds |
| `msim_list_instruments` | List available virtual instruments and their pin names |
| `msim_add_instrument` | Place an oscilloscope, multimeter, function generator, or a custom instrument |
| `msim_configure_instrument` | Set function-generator panel values (frequency, amplitude, offset) |
| `msim_set_model` | Swap a BJT/diode model parameters and display name |
| `msim_delete_part` | Remove a component entirely (pins, model, symbol, wires) |
| `msim_disconnect_pin` | Disconnect one pin from its net (e.g. to detach a load) |
| `msim_read_design` | Read a .ms14's full structure without launching Multisim |
| `msim_edit_open` | Load an existing .ms14 into the editing session |
| `msim_rotate_part` | Rotate a component symbol 180° (unwired parts) |
| `msim_add_probe` | Add a voltage probe to an existing net |
| `msim_report_bom` | Bill of materials: refdes, part, annotated value |

### Simulating

| Tool | Purpose |
|---|---|
| `msim_open` | Open a `.ms14` in a hidden Multisim instance |
| `msim_inspect` | List components, probes, variants, sim state |
| `msim_get_value` / `msim_set_value` | Read or change an RLC value |
| `msim_run_dc` | DC operating point |
| `msim_run_ac` | AC sweep (decade / octave / linear) |
| `msim_run_tran` | Transient analysis |
| `msim_get_output` | Read waveform data for a probe |
| `msim_spice_run` | Run a raw SPICE netlist through the built-in engine (no probes needed) |
| `msim_export_image` | Export the schematic as PNG/JPG/BMP |
| `msim_save_as` | Save the open design elsewhere |
| `msim_close` | Shut down the hidden instance |
| `msim_status` | Check runtime and session state |

## Example

```python
spec = {
    "path": r"C:\work\rc_lpf.ms14",
    "components": [
        {"ref": "V1", "kind": "signal_ac",  "value": 1,    "at": [180, 360]},
        {"ref": "R1", "kind": "resistor",   "value": 1000, "at": [360, 360]},
        {"ref": "C1", "kind": "capacitor",  "value": 1e-6, "at": [540, 360]},
        {"ref": "GND1", "kind": "ground",                  "at": [540, 540]},
    ],
    "nets": [
        {"name": "in",  "pins": ["V1.1", "R1.1"]},
        {"name": "out", "pins": ["R1.2", "C1.1"], "probe": True},
        {"name": "0",   "pins": ["C1.2", "GND1.1"]},
        {"name": "0",   "pins": ["V1.2", "GND1.1"]},
    ],
}

msim_build(spec)
msim_open(spec["path"])
msim_run_ac(num_points=100, start=1, stop=1e5)
data = msim_get_output("V(PR1)")
```

Result: a 501-point frequency response. Measured corner frequency 158.5 Hz against a theoretical 159.2 Hz.

## Pin names

Pins are addressed as `"REFDES.PIN"`. Pin names come from the component symbol:

| Component | Pins |
|---|---|
| Resistor, capacitor, inductor, DC source | `"1"`, `"2"` |
| Transistor | `"C"`, `"B"`, `"E"` |
| Ground | `"1"` |
| Op-amp (ideal) | `"IN+"`, `"IN-"`, `"OUT"` |

Writing `Q1.1` for a transistor raises `BAD_PIN` rather than silently connecting the wrong leg.

| Instrument | Pins |
|---|---|
| Oscilloscope `XSC` | `"1"`, `"2"`, `"4"`, `"3"`, `"5"`, `"6"` (A, B, Ext Trig) |
| Multimeter `XMM` | `"2"`, `"1"` |
| Function generator `XFG` | `"1"`, `"2"`, `"3"` (+, common, -) |

Pin numbering follows the symbol, not the physical order. Use `msim_list_instruments()` to read the list back instead of guessing.

## Instruments

`msim_add_instrument()` places an instrument on the schematic. Call it after adding the ordinary parts, then wire it with `msim_connect()` like any other component:

```python
msim_list_instruments()
msim_add_instrument("oscilloscope")                     # XSC1, auto-placed to the right
msim_add_instrument("function_generator", x=1200, y=160)
msim_connect("in",  ["XFG1.1", "R1.1"])
msim_connect("gnd", ["XFG1.2", "XSC1.3", "GND1.1"])
msim_connect("out", ["R1.2", "XSC1.1"], probe=True)
```

Built-in kinds: `oscilloscope`, `multimeter`, `function_generator`, plus short aliases (`scope`, `dmm`, `fgen`, `xsc`, `xmm`, `xfg`). Designators are assigned automatically and can be overridden with `refdes`.

Other instruments (Bode plotter `XBP`, logic analyser `XLA`, current probe `XCP`, ...) work through a donor design:

```python
msim_list_instruments(donor=r"C:\designs\with_bode_plotter.ms14")
msim_add_instrument("custom", donor=r"C:\designs\with_bode_plotter.ms14",
                    donor_refdes="XBP1")
```

A donor is any `.ms14` containing that instrument. The server clones the symbol and its pins; you still have to connect them.

**Where the numbers come from.** There are two paths:

1. **Probes** — `msim_run_tran()` + `msim_get_output()` on a probe. The output name is assigned by Multisim (`V(PR1)`...), and the mapping from probe number to net is not exposed over the automation interface, so prefer the netlabel approach below when exact per-net readings matter.
2. **The built-in SPICE engine** — `msim_spice_run(netlist, commands, outdir)` sends a raw SPICE deck straight through the simulator's command channel (`source` → `op/tran/ac` → `write` → ASCII raw). It needs no probes or instruments, returns every node by the name you chose in the netlist, and is the deterministic way to get numbers. Open any design first (`msim_open(blank.ms14)` is enough) to provide the COM context.

```python
msim_open(r"C:\path\to\blank.ms14")
r = msim_spice_run(NETLIST_TEXT, ["op", "tran 0 5m 0 2u"],
                   r"C:\out\spice")     # no spaces in outdir
```

Instrument panels are part of the schematic and show up in exported images; panel settings for the function generator are set with `msim_configure_instrument()`.

## Error codes

| Code | Meaning |
|---|---|
| `BAD_ARGS` | Malformed input |
| `BAD_PIN` / `BAD_PIN_REF` | Pin not found or malformed reference |
| `PIN_CONFLICT` | Pin already belongs to another net (would short) |
| `NO_PART` | Reference designator not in the design |
| `DUP_REFDES` | Reference designator already used |
| `PROBE_OFF_WIRE` | `probe_at` is not on a horizontal wire segment |
| `PROBE_NO_HORIZONTAL` | that net has no horizontal wire segment; move a component so the wire has one |
| `BAD_PATH` | output file must end in `.ms14` |
| `NO_SESSION` | No circuit being built |
| `NO_PY32` / `NO_BLANK` | 32-bit runtime or blank design not found |
| `WORKER_TIMEOUT` / `WORKER_DIED` / `WORKER_DESYNC` | COM bridge restarted; `error.detail` carries the worker's stderr when available |
| `COM_ERROR` | Multisim rejected the operation |

## How circuits stay consistent

Two things are easy to get wrong when editing `.ms14` files directly:

**A component lives in two trees.** The electrical tree (`Elements`) drives simulation; the drawing tree (`Objects`) drives what you see. Both must be updated, and they are linked by ID.

**A value lives in five places.** A resistor's value appears in the parameter list, a human-readable string, an authoritative string copy, a display cache, and the rendered text object. Change one and the others go stale. The build layer updates all five.

## Development

```bat
python tests/test_regression.py
python tests/test_instruments.py
```

`test_regression.py` covers UTF-8 paths, pin conflicts, transistor pin resolution, probe placement validation, duplicate designators, file extension checks, and a DC measurement. `test_instruments.py` covers instrument placement, wiring, file validity, exported images, and simulation with instruments present. Both need Multisim installed locally for the COM checks.

## Known limitations

- **Instrument front panels** are not driven over the automation interface. Instruments can be placed and wired, but their panel settings and on-screen readings are not exposed as tools; use probes for numeric results.
- **Transient analysis** requires at least one probe; without one there is nothing to record.
- **`.ms7` files** cannot be opened through COM; only `.ms14`.
- **Schematic layout** is basic. Components are placed at the coordinates you give; there is no auto-router that produces publication-quality schematics.
- **Hierarchical subcircuits** (sheet blocks / macro components) are not cloned from donor designs. Ordinary components and instruments are.
- **No live attachment to a Multisim window you opened yourself.** The COM interface always creates a separate hidden instance and Multisim registers nothing in the ROT (verified), so agent-vs-GUI collaboration is file-based: you Ctrl+S, the agent reads your file with `msim_read_design` (your file is never modified), and produces a new `.ms14` for you to open.
- **`IMultisimCircuit.ReportNetlist` crashes** (0xC0000005) on any circuit with any arguments — an NI-side defect, verified against the stock blank design. Netlist inspection goes through `msim_spice_run` instead.

## License

MIT — see [LICENSE](LICENSE).
