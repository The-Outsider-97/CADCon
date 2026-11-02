# CADCon user guide — development build

1. Start CADCon and open the authenticated URL printed in the terminal.
2. Choose **Counter** to import an OBJ, STL or a supported SLAI spatial document.
   The vertex, edge and polygon definitions are shown with the results. Drag the
   3D preview to orbit, scroll to zoom, or double-click to reset the camera.
3. Choose **Converter**, select an imported file, an available target and a
   loss policy. CADCon validates the output, then starts a browser download and
   keeps a manual download button available. Failed conversions show a persistent
   error in the panel. A missing backend or unsupported format pair returns an
   error rather than a fake file.
4. Choose **Analysis & Report** to inspect geometry and download JSON or SLDC.
   CADCon captures the actual model as a PNG for the SLDC cover before enabling
   **Export SLDC**. If capture fails, JSON export still works; use **Retry
   screenshot** to try again. Reports are downloaded as
   `cadcon-analysis-{model-stem}.json` or `.sldc`, with the original model name
   minus its extension. Native SLDC reports contain a cover, linked table of
   contents, geometry summary, validation coverage, findings, and provenance.
   Optional AI Analysis appears only with evidence-backed agent output.
   For an open or non-manifold triangle mesh, open **Mesh Repair &
   Reconstruction** below the analysis warning. Choose the welding tolerance
   (zero means exact coordinates), click **Analyze Repair Options**, review
   both source-index and reconstructed topology, and then click **Generate
   Repair Variants**. The candidate viewer colors genuinely added faces red;
   topology-only changes add no red surfaces. Candidate buttons show measured
   status and provide verified **Export SGEO** and **Export OBJ** actions.
   Selecting a candidate captures its 3D repair preview for the native SLDC
   report, including red generated surfaces where present.
   Export never overwrites the source. A topologically watertight mesh may
   still contain untested self-intersections or overlapping shells; CADCon
   does not certify solid volume based on these checks alone.
5. Choose **Compare** and select two imported files to compare numeric and
   triangle topology properties; export its report as JSON or SLDC.
6. Choose **System** to see actual conversion backend availability. The theme
   toggle remains at the bottom of the sidebar.

The 3D preview displays up to 50,000 triangles. During interaction CADCon
temporarily renders a lighter, evenly distributed subset; the full preview
returns when movement stops. The preview is not a
precision CAD viewport; analysis always uses the full accepted file. For
unitless inputs, the current default user assumption is millimeters. Exact
STEP/ACIS conversion, freeform geometric comparison, complete batch jobs and
The optional SLAI Reasoning Agent analyzes structured topology evidence when
its existing factory can create a working agent. It does not alter the mesh;
the deterministic repair engine remains authoritative if the agent is
unavailable or disabled. Curvature-surface fairing and volumetric remeshing
require verified optional backends.
