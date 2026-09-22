from __future__ import annotations

import json
from pathlib import Path


FILES = [
    Path("notebooks/v2_standardized_0326_vs_0827.ipynb"),
    Path("notebooks/v2_standardized_0326_vs_0827_executed.ipynb"),
    Path("notebooks/v2_standardized_0603_vs_0827.ipynb"),
    Path("notebooks/v2_standardized_0603_vs_0827_executed.ipynb"),
]
REPORT = Path("reports/WR503_vibration_standardized_report_v3.pdf")


for path in FILES:
    notebook = json.loads(path.read_text(encoding="utf-8"))
    errors = [
        output
        for cell in notebook.get("cells", [])
        for output in cell.get("outputs", [])
        if output.get("output_type") == "error"
    ]
    print(
        f"{path}: bytes={path.stat().st_size}, "
        f"nbformat={notebook['nbformat']}.{notebook['nbformat_minor']}, "
        f"cell_errors={len(errors)}"
    )

pdf = REPORT.read_bytes()
if not pdf.startswith(b"%PDF-") or not pdf.rstrip().endswith(b"%%EOF"):
    raise ValueError(f"Invalid PDF structure: {REPORT}")
print(
    f"{REPORT}: bytes={REPORT.stat().st_size}, "
    f"page_objects={pdf.count(b'/Type /Page')}, header={pdf[:8]!r}, eof=OK"
)
