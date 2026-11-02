# CADCon user guide — development build

1. Start CADCon and open the authenticated URL printed in the terminal.
2. Choose **Counter** to import an OBJ, STL or a supported SLAI spatial document.
   The vertex, edge and polygon definitions are shown with the results. Drag the
   3D preview to orbit, scroll to zoom, or double-click to reset the camera.
3. Choose **Converter**, select an imported file, an available target and a
   loss policy. Download the output only after CADCon validates it. A missing
   backend or unsupported format pair returns an error rather than a fake file.
4. Choose **Analysis & Report** to inspect geometry and download JSON or SLDC.
5. Choose **Compare** and select two imported files to compare numeric and
   triangle topology properties; export its report as JSON or SLDC.
6. Choose **System** to see actual conversion backend availability. The theme
   toggle remains at the bottom of the sidebar.

The 3D preview is bounded and may show a subset of a large mesh. It is not a
precision CAD viewport; analysis always uses the full accepted file. For
unitless inputs, the current default user assumption is millimeters. Exact
STEP/ACIS conversion, freeform geometric comparison, complete batch jobs and
SLAI Agent inference are not yet implemented.
