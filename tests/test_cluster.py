"""
Unit tests for Devrith's Architectural Cluster Mapping & Change Impact Predictor.
"""

import os
import json
import tempfile
import pytest

from arch_sidecar.cluster import (
    DependencyGraph,
    build_repository_dependency_graph,
    partition_into_subsystems,
    predict_change_impact,
    format_cluster_map_terminal,
    format_change_impact_terminal,
)


def test_dependency_graph_python():
    """Verify directed dependency graph extraction across Python modules."""
    with tempfile.TemporaryDirectory() as tmpdir:
        os.makedirs(os.path.join(tmpdir, "pkg", "sub"), exist_ok=True)
        
        leaf = os.path.join(tmpdir, "pkg", "sub", "leaf.py")
        mid = os.path.join(tmpdir, "pkg", "mid.py")
        root = os.path.join(tmpdir, "pkg", "main.py")

        with open(leaf, "w") as f:
            f.write("CONST_A = 10\ndef leaf_func(): return CONST_A\n")

        with open(mid, "w") as f:
            f.write("from .sub.leaf import leaf_func\ndef mid_func(): return leaf_func() * 2\n")

        with open(root, "w") as f:
            f.write("from .mid import mid_func\nresult = mid_func()\n")

        graph = build_repository_dependency_graph(tmpdir)
        
        assert len(graph.modules) == 3
        rel_leaf = os.path.join("pkg", "sub", "leaf.py")
        rel_mid = os.path.join("pkg", "mid.py")
        rel_root = os.path.join("pkg", "main.py")

        assert rel_leaf in graph.edges[rel_mid]
        assert rel_mid in graph.edges[rel_root]
        assert rel_mid in graph.reverse_edges[rel_leaf]
        assert rel_root in graph.reverse_edges[rel_mid]


def test_subsystem_clustering_and_metrics():
    """Verify clustering cohesion and Robert C. Martin's instability metric."""
    with tempfile.TemporaryDirectory() as tmpdir:
        os.makedirs(os.path.join(tmpdir, "core", "models"), exist_ok=True)
        os.makedirs(os.path.join(tmpdir, "api", "routes"), exist_ok=True)

        # Core models (highly cohesive internal cluster)
        m1 = os.path.join(tmpdir, "core", "models", "user.py")
        m2 = os.path.join(tmpdir, "core", "models", "account.py")
        with open(m1, "w") as f: f.write("class User: pass\n")
        with open(m2, "w") as f: f.write("from .user import User\nclass Account: user: User\n")

        # API route (unstable consumer cluster depending on core)
        r1 = os.path.join(tmpdir, "api", "routes", "user_routes.py")
        with open(r1, "w") as f: f.write("from core.models.user import User\ndef get_user(): return User()\n")

        graph = build_repository_dependency_graph(tmpdir)
        clusters = partition_into_subsystems(graph)

        assert "core.models" in clusters
        assert "api.routes" in clusters

        core_c = clusters["core.models"]
        assert core_c["modules_count"] == 2
        # Core has afferent coupling from API (Ca > 0)
        assert core_c["afferent_coupling_ca"] >= 1
        # Core instability should be low (stable foundation)
        assert core_c["instability"] < 0.5


def test_predict_change_impact():
    """Verify pre-change impact prediction, blast radius, and risk classification."""
    with tempfile.TemporaryDirectory() as tmpdir:
        os.makedirs(os.path.join(tmpdir, "app", "db"), exist_ok=True)
        os.makedirs(os.path.join(tmpdir, "app", "services"), exist_ok=True)
        os.makedirs(os.path.join(tmpdir, "app", "controllers"), exist_ok=True)
        os.makedirs(os.path.join(tmpdir, "tests"), exist_ok=True)

        target_file = os.path.join(tmpdir, "app", "db", "schema.py")
        svc_file = os.path.join(tmpdir, "app", "services", "user_service.py")
        ctrl_file = os.path.join(tmpdir, "app", "controllers", "user_ctrl.py")
        test_file = os.path.join(tmpdir, "tests", "test_schema.py")

        with open(target_file, "w") as f: f.write("class Schema: pass\n")
        with open(svc_file, "w") as f: f.write("from app.db.schema import Schema\ndef svc(): return Schema()\n")
        with open(ctrl_file, "w") as f: f.write("from app.services.user_service import svc\ndef ctrl(): return svc()\n")
        with open(test_file, "w") as f: f.write("from app.db.schema import Schema\ndef test_schema(): pass\n")

        impact = predict_change_impact(target_file, root_dir=tmpdir)

        assert impact["success"] is True
        assert impact["target"] == os.path.join("app", "db", "schema.py")
        assert impact["direct_callers_count"] >= 2  # svc_file and test_file
        # Transitive reachability reaches ctrl_file through svc_file
        assert impact["transitive_blast_radius_count"] >= 3
        # Recommended tests should include test_schema.py
        assert any("test_schema" in t for t in impact["recommended_tests"])


def test_terminal_formatting_smoke():
    """Ensure terminal formatters run without error."""
    clusters = {
        "core.db": {
            "cluster_id": "core.db",
            "modules_count": 5,
            "cohesion_ratio": 0.85,
            "instability": 0.15,
            "top_bridge_modules": ["core/db/base.py"]
        }
    }
    rendered = format_cluster_map_terminal(clusters, "demo_repo")
    assert "core.db" in rendered
    assert "0.85" in rendered

    dummy_impact = {
        "target": "core/db/base.py",
        "cluster": "core.db",
        "lines_of_code": 120,
        "language": "python",
        "risk_level": "MEDIUM_RISK",
        "risk_factors": ["High fan-in"],
        "direct_callers_count": 3,
        "direct_callers": ["a.py", "b.py", "c.py"],
        "transitive_blast_radius_count": 5,
        "touched_clusters_count": 2,
        "touched_clusters": ["core.db", "api"],
        "recommended_tests": ["tests/test_db.py"],
        "duration_ms": 12.5,
    }
    rendered_impact = format_change_impact_terminal(dummy_impact)
    assert "MEDIUM_RISK" in rendered_impact
    assert "core/db/base.py" in rendered_impact
