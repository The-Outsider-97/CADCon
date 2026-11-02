# CADCon — SLAI CAD Converter

CADCon is a local Windows-first CAD inspection and conversion application. The
current implementation is a **v1.0 development build**, not a completed release.
It uses SLAI v2.4's secure local application server, shared UI theme, and public
SGEO/SDRW/SLDC geometry and document APIs. CADCon-specific OBJ parsing, source
STL count semantics, analysis, and topology-sensitive triangle comparison are
kept separate from the native spatial implementation.

## Running

Install Python 3.12+ and Node.js. From the CADCon checkout:

```powershell
py -m pip install -r requirements.txt
npm install
$env:SLAI_ROOT = 'G:\GAIA\AI\SLAI'
npm run build
py run_cadcon.py --slai-root 'G:\GAIA\AI\SLAI'
```

From `SLAI/`, with CADCon cloned into `applications/CADCon/`:

```powershell
py -m applications.CADCon
```

CADCon first tries port `8770`. If that default port is already in use, it now
prints a diagnostic and automatically requests an available loopback port from
Windows. Pass `--port 0` to request an available port immediately, or
`--port 8771` to require a specific free port (an explicitly requested busy
port is an error). Use `--no-browser` to print the authenticated URL without
opening a browser.
No app-specific server or shared theme files need to be copied from SLAI.

The app **requires a local SLAI v2.4 checkout** to supply shared application
tools and native spatial documents. Its own repository remains the source of
truth for CADCon-specific features.

## Working features

- Secure local UI with Counter, Converter, Analysis & Report, Compare, and System.
- Dark/light mode, local drag-and-drop import, basic 2D SVG and interactive 3D
  software-rendered previews (50,000-triangle display cap; full-file analysis).
- OBJ triangle/planar-quad inspection and ASCII/binary STL inspection.
- SGEO, SDRW, GLB and supported SVG/DXF interchange through SLAI spatial APIs.
- The Converter target selector follows the runtime's directed capability
  registry and disables missing routes rather than implying universal exchange.
- Conversion output verification, source/target SHA-256, declared units,
  backend details and loss-policy handling through SLAI's exchange adapters;
  JSON and native SLDC analysis/comparison reports.
- Distinct source vertex records, coordinate-unique vertices and indexed edges.
- Basic deterministic comparison of triangle position sets, topology statistics,
  bounds and quantities. The triangle equivalence test normalizes supported
  unit declarations to metres and can recognize different vertex/face order.
- Findings with stable topology codes, counts, evidence, and explicitly listed
  checks that were not performed. Opt-in nearest-entity queries call SLAI's
  Spatial subsystem, not a replacement geometry model.
- Analysis downloads use the original model stem, e.g.
  `cadcon-analysis-hinge.sldc` / `cadcon-analysis-hinge.json`. The native SLDC
  report uses a cover with an embedded PNG rendered from CADCon's actual model
  preview, a managed table of contents, semantic chapters, structured tables,
  and page-break/keep-with-next metadata. SLAI's SLDC 1.1 serializer is the
  sole owner of the package and image assets; browser capture must complete
  before the SLDC export button is enabled.
- **Mesh Repair & Reconstruction** is available under Analysis & Report for
  triangle meshes. It compares source-index topology with exact or optional
  tolerance-welded topology, diagnoses boundary loops and non-manifold edges,
  and builds actual geometry for supported conservative strategies: vertex
  welding, separation of touching manifold shells, triangulated planar holes,
  and convex planar caps. Added faces are red in the interactive 3D preview;
  topology-only changes correctly have no red faces. Repaired candidates are
  independently checked and can be exported through native SLAI SGEO or OBJ.
  The selected candidate can also be captured as a separate native SLDC PNG
  asset inside the report's Findings section, with generated faces colored red.
  The source is never overwritten. Curvature-aware and volumetric remeshing
  remain disabled until verified backends are configured. These checks establish
  topological closure, **not** certified solid validity: self-intersections,
  coincident shell overlaps, intentional functional openings, and signed solid
  volume require further validation. CADCon optionally requests explanations
  from SLAI's public `AgentFactory` and `ReasoningAgent.perform_task` contract,
  with an eight-second budget and explicit fallback. An agent may be unavailable
  when its runtime dependencies or trained/configured capabilities are missing;
  only successfully returned responses are labeled AI-assisted. Deterministic
  repair and validation remain authoritative.

## Verified and conditional format matrix

Actual runtime availability is published by `GET /api/capabilities` and shown
under **System**. It may change with installed dependencies. Current paths:

| Format | Import | Export | Implementation and limits |
| --- | --- | --- | --- |
| STL | Yes | Yes with SLAI | Binary/ASCII source read, binary output, unitless triangle mesh |
| OBJ | Yes | Yes | CADCon subset: triangles and convex planar quads; geometry only |
| SGEO | Yes with SLAI | Yes with SLAI | SLAI authoritative native 3D document API |
| GLB | Yes with SLAI | Yes with SLAI | SLAI supported glTF 2.0 subset; 3D mesh representations |
| SDRW | Yes with SLAI | Yes with SLAI | SLAI authoritative native 2D drawing API |
| SVG | Yes with SLAI | Yes with SLAI | Restricted safe vector subset |
| DXF | With `ezdxf` | With `ezdxf` | Supported vector entities through SLAI SDRW |
| DWG | With ODA configured | With ODA configured | Optional external ODA executable; limited to covered entities |
| IFC | With IfcOpenShell | No | Optional import to a geometry-only 3D scene |
| STEP/STP, SAT, FBX | No | No | No verified precision/proprietary backend installed |

When available, **3D routes** connect distinct supported inputs/outputs among
STL, OBJ, SGEO and GLB; IFC is an optional *input only*. **2D routes** connect
distinct SDRW, SVG and DXF formats; DWG joins them only when its explicitly
configured backend is healthy. Strict policy may refuse a specific conversion
because it would lose precision, metadata or semantics.

## Limitations and fidelity

- STL and OBJ are unitless. CADCon exposes a declared input-unit selector
  (millimetres by default) and remembers it in browser preferences; no units
  are inferred from the files. Export to a unitless target scales physical
  coordinates when the source document has explicit units.
- OBJ import supports triangles and convex planar quads only. Material, texture,
  grouping and authored normals are not preserved by CADCon's OBJ adapter.
- GLB is limited to the SLAI-supported glTF 2.0 subset; exact B-reps and NURBS
  are tessellated. SVG import is SLAI's deliberately restricted safe subset.
- DXF requires `ezdxf`. DWG requires a configured, licensed ODA executable
  (`--dwg-executable`). IFC requires compatible IfcOpenShell. Full STEP, SAT,
  FBX, glTF JSON and other listed candidate formats are **not implemented**.
- CADCon currently provides **source-level topology counting**. Unindexed STL
  source records count three vertices/edges per triangle even if positions
  coincide, matching the reporting convention of the reference BIMAP extraction.
  Solid-volume certification is withheld for unclosed or nonmanifold topology.
- Comparison does not yet implement rigid registration, spatial difference
  heatmaps or surface-distance equivalence proof. Same counts/bounds alone are
  never treated as proof of equivalence.
- For native or precision sources, the visible triangle counts are from SLAI
  tessellation and **must not be interpreted as exact B-rep face/edge counts**.
  Only the checks enumerated as executed in a report are certified as run.
- The SLAI Spatial subsystem is wired to an optional nearest-entity query
  using native SGEO/SDRW agent DTOs. The top-level multi-agent orchestrator and
  remaining Language/Reasoning/Quality agents are not yet connected; System
  explicitly reports this incomplete state.
- The AI Analysis report chapter is omitted unless a verified, evidence-linked
  agent result is present. No AI prose is simulated. The cover PNG may use a
  50,000-triangle subset for large sources, while all numeric analysis uses the
  complete model. The PDF/SLDC editor layout depends on the installed SLAI
  document renderer honoring native page breaks and keep-with-next styles.
- Batch conversion, job persistence, advanced geometry measurement and complete
  animation/assembly semantics remain development work.
- The side-by-side compare previews are individually navigable; synchronized
  camera controls, transparency overlays and difference heatmaps are not yet
  implemented. The renderer is a deliberately bounded software 3D preview;
  interaction uses an evenly distributed lower-detail rendering pass, then
  redraws the full preview after motion settles.

No unsupported format pair is advertised as a verified conversion. Source
files are never overwritten by conversion. All operations use the shared
loopback-only, authenticated SLAI HTTP server.

## Tests

```powershell
py -m pip install pytest
py -m pytest -q tests
npm run build
```

For the standalone checkout, ensure `SLAI_ROOT` points to the v2.4 tree or set
`PYTHONPATH` to that root when running native format tests.

## Package structure

`engine.py` provides bounded geometry inspection, export routing and analysis;
`app.py` is the thin shared-server composition root; `src/client/` holds the
TypeScript/CSS UI. `tools/build.mjs` delegates to SLAI's shared build tool.

See `doc/USER_GUIDE.md` for the operation flow.
