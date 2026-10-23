import os
import shutil
import tempfile
import pytest

from arch_sidecar.dart_ast import parse_dart_ast, format_dart_digest_skeleton
from arch_sidecar.jvm_ast import parse_jvm_ast, format_jvm_digest_skeleton
from arch_sidecar.context import locate_surgical_context
from arch_sidecar.polyglot_resolver import (
    resolve_dart_import_path,
    resolve_jvm_import_path,
    detect_dart_circular_imports,
    detect_jvm_circular_imports,
    check_dart_lexical_scope,
    check_jvm_lexical_scope,
)
from arch_sidecar.cluster import build_repository_dependency_graph
from arch_sidecar.guard import check_code_health
from arch_sidecar.polyglot_decouple import decouple_polyglot_symbol
from arch_sidecar.feature_workflow import run_feature_workflow


# ============================================================================
# 1. Dart AST & Lexical Parsing
# ============================================================================

def test_dart_ast_parsing_and_skeleton():
    dart_code = """
library my_library;

import 'dart:async';
import 'package:flutter/material.dart' as material;
import 'models/user.dart' show User;
export 'models/user.dart';

/// User service contract
abstract class UserService {
  Future<User> getUser(String id);
}

mixin Timestamped {
  DateTime createdAt = DateTime.now();
}

extension StringUtils on String {
  String capitalize() => "${this[0].toUpperCase()}${substring(1)}";
}

enum Role { admin, user, guest }

typedef VoidCallback = void Function();

class UserServiceImpl with Timestamped implements UserService {
  @override
  Future<User> getUser(String id) async {
    return User(id: id);
  }

  void _internalHelper() {
    print("Internal helper");
  }
}

Future<void> topLevelRunner() async {
  print("Running top level");
}
"""
    with tempfile.NamedTemporaryFile("w", suffix=".dart", delete=False) as f:
        f.write(dart_code)
        f.flush()
        dart_path = f.name

    try:
        parsed = parse_dart_ast(dart_code, filename=dart_path)
        assert parsed["library"] == "my_library"
        assert len(parsed["imports"]) == 3
        assert len(parsed["exports"]) == 1

        decl_names = {d["name"] for d in parsed["declarations"]}
        assert "UserService" in decl_names
        assert "Timestamped" in decl_names
        assert "Role" in decl_names
        assert "UserServiceImpl" in decl_names
        assert "UserServiceImpl.getUser" in decl_names
        assert "UserServiceImpl._internalHelper" in decl_names
        assert "topLevelRunner" in decl_names

        # Method export visibility check
        pub_methods = [d for d in parsed["declarations"] if d["name"] == "UserServiceImpl.getUser"]
        assert pub_methods[0]["is_exported"] is True

        priv_methods = [d for d in parsed["declarations"] if d["name"] == "UserServiceImpl._internalHelper"]
        assert priv_methods[0]["is_exported"] is False

        # Digest skeleton
        skeleton = format_dart_digest_skeleton(dart_path)
        assert "library my_library;" in skeleton
        assert "UserService" in skeleton
        assert "UserServiceImpl" in skeleton
    finally:
        os.remove(dart_path)


# ============================================================================
# 2. JVM (Kotlin & Java) AST Parsing
# ============================================================================

def test_jvm_ast_parsing_and_skeleton():
    kotlin_code = """
package com.example.service

import com.example.models.User
import java.time.Instant

data class UserProfile(val id: String, val username: String)

sealed class Result<out T> {
    data class Success<out T>(val data: T) : Result<T>()
    data class Error(val message: String) : Result<Nothing>()
}

class UserService(private val repo: UserRepository) {
    suspend fun fetchProfile(id: String): UserProfile {
        return UserProfile(id, "user_" + id)
    }

    companion object {
        const val TIMEOUT_MS: Long = 5000L
        fun defaultService(): UserService = UserService(UserRepository())
    }
}

fun String.toSlug(): String = this.lowercase().replace(" ", "-")
"""
    java_code = """
package com.example.domain;

import java.util.List;
import java.util.Optional;

public record AccountDto(String id, String email, double balance) {}

public interface AccountService {
    Optional<AccountDto> findById(String id);
    void updateBalance(String id, double delta);
}

public class DefaultAccountService implements AccountService {
    private final AccountRepository repo;

    public DefaultAccountService(AccountRepository repo) {
        this.repo = repo;
    }

    @Override
    public Optional<AccountDto> findById(String id) {
        return repo.findById(id);
    }
}
"""
    with tempfile.NamedTemporaryFile("w", suffix=".kt", delete=False) as f_kt:
        f_kt.write(kotlin_code)
        f_kt.flush()
        kt_path = f_kt.name

    with tempfile.NamedTemporaryFile("w", suffix=".java", delete=False) as f_java:
        f_java.write(java_code)
        f_java.flush()
        java_path = f_java.name

    try:
        # Kotlin AST
        parsed_kt = parse_jvm_ast(kotlin_code, filename=kt_path)
        assert parsed_kt["package"] == "com.example.service"
        assert len(parsed_kt["imports"]) == 2
        kt_decls = {d["name"] for d in parsed_kt["declarations"]}
        assert "UserProfile" in kt_decls
        assert "UserService" in kt_decls
        assert "UserService.fetchProfile" in kt_decls
        assert "String.toSlug" in kt_decls

        # Java AST
        parsed_java = parse_jvm_ast(java_code, filename=java_path)
        assert parsed_java["package"] == "com.example.domain"
        assert len(parsed_java["imports"]) == 2
        java_decls = {d["name"] for d in parsed_java["declarations"]}
        assert "AccountDto" in java_decls
        assert "AccountService" in java_decls
        assert "DefaultAccountService" in java_decls
        assert "DefaultAccountService.findById" in java_decls

        # Skeletons
        kt_skel = format_jvm_digest_skeleton(kt_path)
        assert "package com.example.service;" in kt_skel
        assert "UserProfile" in kt_skel

        java_skel = format_jvm_digest_skeleton(java_path)
        assert "package com.example.domain;" in java_skel
        assert "AccountDto" in java_skel
    finally:
        os.remove(kt_path)
        os.remove(java_path)


# ============================================================================
# 3. Polyglot Surgical Context Slicing (Dart & JVM)
# ============================================================================

def test_surgical_context_slicing_dart_jvm():
    temp_dir = tempfile.mkdtemp()
    try:
        dart_file = os.path.join(temp_dir, "auth_provider.dart")
        with open(dart_file, "w", encoding="utf-8") as f:
            f.write("""
class AuthProvider {
  bool isAuthenticated = false;

  Future<void> signIn(String email, String password) async {
    isAuthenticated = true;
  }
}
""")

        kt_file = os.path.join(temp_dir, "OrderProcessor.kt")
        with open(kt_file, "w", encoding="utf-8") as f:
            f.write("""
package com.shop

data class Order(val orderId: String, val amount: Double)

class OrderProcessor {
    fun process(order: Order): Boolean {
        return order.amount > 0
    }
}
""")

        # Dart surgical slice
        ctx_dart = locate_surgical_context("locate AuthProvider in auth_provider.dart", root_dir=temp_dir)
        assert "AuthProvider" in ctx_dart["matched_symbols"]
        auth_details = ctx_dart["symbol_details"]["AuthProvider"]
        assert auth_details["start_line"] == 2
        assert "AuthProvider" in ctx_dart["context"]
        assert "Future<void> signIn" in ctx_dart["context"]

        # Kotlin surgical slice
        ctx_kt = locate_surgical_context("examine OrderProcessor logic", root_dir=temp_dir)
        assert "OrderProcessor" in ctx_kt["matched_symbols"]
        order_details = ctx_kt["symbol_details"]["OrderProcessor"]
        assert "OrderProcessor" in ctx_kt["context"]
        assert "fun process" in ctx_kt["context"]
    finally:
        shutil.rmtree(temp_dir)


# ============================================================================
# 4. Circular Import Cycle Detection (Dart & JVM)
# ============================================================================

def test_dart_and_jvm_circular_import_detection():
    temp_dir = tempfile.mkdtemp()
    try:
        # 1. Dart circular dependency (A -> B -> A)
        d_a = os.path.join(temp_dir, "service_a.dart")
        d_b = os.path.join(temp_dir, "service_b.dart")
        with open(d_a, "w", encoding="utf-8") as f:
            f.write("import 'service_b.dart';\nclass ServiceA {}\n")
        with open(d_b, "w", encoding="utf-8") as f:
            f.write("import 'service_a.dart';\nclass ServiceB {}\n")

        dart_cycles = detect_dart_circular_imports(temp_dir)
        assert len(dart_cycles) >= 1
        assert dart_cycles[0]["language"] == "dart"
        assert len(dart_cycles[0]["cycle_path"]) >= 2

        # 2. JVM package circular dependency (pkg.alpha -> pkg.beta -> pkg.alpha)
        alpha_dir = os.path.join(temp_dir, "pkg", "alpha")
        beta_dir = os.path.join(temp_dir, "pkg", "beta")
        os.makedirs(alpha_dir, exist_ok=True)
        os.makedirs(beta_dir, exist_ok=True)

        with open(os.path.join(alpha_dir, "AlphaService.kt"), "w", encoding="utf-8") as f:
            f.write("package pkg.alpha;\nimport pkg.beta.BetaService;\nclass AlphaService {}\n")
        with open(os.path.join(beta_dir, "BetaService.java"), "w", encoding="utf-8") as f:
            f.write("package pkg.beta;\nimport pkg.alpha.AlphaService;\npublic class BetaService {}\n")

        jvm_cycles = detect_jvm_circular_imports(temp_dir)
        assert len(jvm_cycles) >= 1
        assert jvm_cycles[0]["language"] == "jvm"
        assert "pkg.alpha" in jvm_cycles[0]["cycle_path"]
        assert "pkg.beta" in jvm_cycles[0]["cycle_path"]
    finally:
        shutil.rmtree(temp_dir)


# ============================================================================
# 5. In-Process Lexical Scope Binding Preflight (Dart & JVM)
# ============================================================================

def test_dart_and_jvm_lexical_scope_preflight():
    # Valid Dart code
    valid_dart = """
import 'dart:math';

class Calculator {
  double computeArea(double radius) {
    return pi * radius * radius;
  }
}
"""
    dart_health = check_code_health("nonexistent_file.dart", enable_ruff=False) # file not found check
    assert dart_health["success"] is False

    with tempfile.NamedTemporaryFile("w", suffix=".dart", delete=False) as f:
        f.write(valid_dart)
        f.flush()
        valid_dart_path = f.name

    try:
        health = check_code_health(valid_dart_path)
        assert health["success"] is True
        assert health["language"] == "dart"
        assert health["health_score"] == 100.0
    finally:
        os.remove(valid_dart_path)

    # Invalid Dart code: unbalanced braces
    invalid_dart = "class Broken { void run() { print('open'); "
    with tempfile.NamedTemporaryFile("w", suffix=".dart", delete=False) as f:
        f.write(invalid_dart)
        f.flush()
        invalid_dart_path = f.name

    try:
        health = check_code_health(invalid_dart_path)
        assert health["success"] is False
        assert any(i["type"] == "UNBALANCED_BRACES" for i in health["issues"])
    finally:
        os.remove(invalid_dart_path)

    # Valid Kotlin code
    valid_kt = """
package com.example

import java.util.UUID

class TokenGenerator {
    fun generate(): String = UUID.randomUUID().toString()
}
"""
    with tempfile.NamedTemporaryFile("w", suffix=".kt", delete=False) as f:
        f.write(valid_kt)
        f.flush()
        valid_kt_path = f.name

    try:
        health = check_code_health(valid_kt_path)
        assert health["success"] is True
        assert health["language"] == "kotlin"
        assert health["health_score"] == 100.0
    finally:
        os.remove(valid_kt_path)

    # Invalid Kotlin code: unresolved class reference
    invalid_kt = """
package com.example

class ClientService {
    fun run() {
        val client = HallucinatedUnimportedClient.create()
    }
}
"""
    scope_issues = check_jvm_lexical_scope(invalid_kt, filename="ClientService.kt")
    assert any("HallucinatedUnimportedClient" in s for s in scope_issues)


# ============================================================================
# 6. Polyglot Decouple & Rollback (Dart & JVM)
# ============================================================================

def test_dart_and_jvm_decouple_and_rollback():
    temp_dir = tempfile.mkdtemp()
    try:
        # Dart decoupling
        dart_a = os.path.join(temp_dir, "lib", "manager.dart")
        dart_b = os.path.join(temp_dir, "lib", "session.dart")
        os.makedirs(os.path.dirname(dart_a), exist_ok=True)

        with open(dart_a, "w", encoding="utf-8") as f:
            f.write("""
class SessionInfo {
  final String id;
  SessionInfo(this.id);
}

class SessionManager {
  SessionInfo current = SessionInfo('abc');
}
""")

        # Move SessionInfo from manager.dart to session.dart
        res_dart = decouple_polyglot_symbol(
            symbol="SessionInfo",
            from_file=dart_a,
            to_file=dart_b,
            project_root=temp_dir,
            add_backward_compat_alias=True,
        )
        assert res_dart["success"] is True
        assert res_dart["language"] == "dart"
        assert os.path.isfile(dart_b)

        # Verify backwards-compat export
        with open(dart_a, "r", encoding="utf-8") as f:
            content_a = f.read()
        assert "export" in content_a
        assert "SessionInfo" in content_a

        with open(dart_b, "r", encoding="utf-8") as f:
            content_b = f.read()
        assert "class SessionInfo" in content_b

        # Kotlin decoupling
        kt_a = os.path.join(temp_dir, "Repository.kt")
        kt_b = os.path.join(temp_dir, "Models.kt")
        with open(kt_a, "w", encoding="utf-8") as f:
            f.write("""
package com.demo

data class RecordEntity(val id: Long, val value: String)

class Repository {
    fun fetch(): RecordEntity = RecordEntity(1L, "ok")
}
""")

        res_kt = decouple_polyglot_symbol(
            symbol="RecordEntity",
            from_file=kt_a,
            to_file=kt_b,
            project_root=temp_dir,
        )
        assert res_kt["success"] is True
        assert res_kt["language"] == "kotlin"
        assert os.path.isfile(kt_b)

        with open(kt_b, "r", encoding="utf-8") as f:
            content_b = f.read()
        assert "package com.demo;" in content_b
        assert "data class RecordEntity" in content_b

        # Transactional rollback test on failing test command
        res_rollback = decouple_polyglot_symbol(
            symbol="Repository",
            from_file=kt_a,
            to_file=kt_b,
            project_root=temp_dir,
            test_cmd="false", # simulated failing test
        )
        assert res_rollback["success"] is False
        assert "rolled back" in res_rollback["error"].lower()
    finally:
        shutil.rmtree(temp_dir)


# ============================================================================
# 7. Cluster Graph Construction for Dart & JVM
# ============================================================================

def test_cluster_graph_dart_jvm():
    temp_dir = tempfile.mkdtemp()
    try:
        # Create pubspec.yaml and Dart files
        with open(os.path.join(temp_dir, "pubspec.yaml"), "w", encoding="utf-8") as f:
            f.write("name: my_app\n")

        lib_dir = os.path.join(temp_dir, "lib")
        os.makedirs(lib_dir, exist_ok=True)
        with open(os.path.join(lib_dir, "database.dart"), "w", encoding="utf-8") as f:
            f.write("class Database {}\n")
        with open(os.path.join(lib_dir, "app.dart"), "w", encoding="utf-8") as f:
            f.write("import 'package:my_app/database.dart';\nclass App {}\n")

        graph = build_repository_dependency_graph(temp_dir)
        assert "lib/database.dart" in graph.modules
        assert "lib/app.dart" in graph.modules
        assert graph.module_languages["lib/database.dart"] == "dart"
        assert graph.module_languages["lib/app.dart"] == "dart"
        assert "lib/database.dart" in graph.edges["lib/app.dart"]
    finally:
        shutil.rmtree(temp_dir)


# ============================================================================
# 8. Feature Workflow Test Discovery & Runner Inference
# ============================================================================

def test_feature_workflow_runner_inference_dart_jvm():
    temp_dir = tempfile.mkdtemp()
    try:
        # Dart project with Flutter pubspec
        with open(os.path.join(temp_dir, "pubspec.yaml"), "w", encoding="utf-8") as f:
            f.write("name: my_flutter_app\ndependencies:\n  flutter:\n    sdk: flutter\n")

        test_dir = os.path.join(temp_dir, "test")
        os.makedirs(test_dir, exist_ok=True)
        with open(os.path.join(test_dir, "widget_test.dart"), "w", encoding="utf-8") as f:
            f.write("void main() {}\n")

        wf = run_feature_workflow("update widget test in widget_test.dart", project_root=temp_dir)
        impact = wf["impact_map"]
        assert len(impact["related_tests"]) >= 1
        assert "flutter test" in impact["inferred_test_command"]
    finally:
        shutil.rmtree(temp_dir)
