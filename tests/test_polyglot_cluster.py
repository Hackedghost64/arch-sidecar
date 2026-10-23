import pytest
from pathlib import Path
from arch_sidecar.cluster import build_repository_dependency_graph, predict_change_impact


def test_cluster_polyglot_dependency_graph(tmp_path: Path):
    # Setup mixed repo with Go, Rust, TS, and Python
    (tmp_path / "app.py").write_text("import helper\n")
    (tmp_path / "helper.py").write_text("def help(): pass\n")

    # TypeScript
    (tmp_path / "index.ts").write_text('import { ui } from "./ui";\n')
    (tmp_path / "ui.ts").write_text("export const ui = 1;\n")

    # Go
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "service.go").write_text('package pkg\nimport "./sub"\n')
    sub = pkg / "sub"
    sub.mkdir()
    (sub / "sub.go").write_text("package sub\nfunc Sub() {}\n")

    # Rust
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.rs").write_text("use crate::worker::Work;\n")
    (src / "worker.rs").write_text("pub struct Work;\n")

    graph = build_repository_dependency_graph(str(tmp_path))

    # All modules registered
    assert "app.py" in graph.modules
    assert "index.ts" in graph.modules
    assert "pkg/service.go" in graph.modules
    assert "src/main.rs" in graph.modules

    # Cross-file direct caller edges verified
    assert "helper.py" in graph.edges.get("app.py", set())
    assert "app.py" in graph.reverse_edges.get("helper.py", set())

    assert "ui.ts" in graph.edges.get("index.ts", set())
    assert "index.ts" in graph.reverse_edges.get("ui.ts", set())

    assert "src/worker.rs" in graph.edges.get("src/main.rs", set())
    assert "src/main.rs" in graph.reverse_edges.get("src/worker.rs", set())

    # Impact prediction shows caller blast radius
    impact = predict_change_impact(str(tmp_path / "ui.ts"), root_dir=str(tmp_path))
    assert impact["direct_callers_count"] >= 1
    assert "index.ts" in impact["direct_callers"]
