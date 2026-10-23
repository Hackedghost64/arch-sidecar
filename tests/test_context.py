import os
import tempfile
from arch_sidecar.context import locate_surgical_context

def test_locate_surgical_context():
    code_main = """
from helper import utility_calc

def run_service():
    val = utility_calc(10)
    return val
"""
    code_helper = """
import math

def utility_calc(x: int) -> int:
    \"\"\"Calculates square root and returns int.\"\"\"
    return int(math.isqrt(x))
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "main.py"), "w") as f:
            f.write(code_main)
        with open(os.path.join(tmpdir, "helper.py"), "w") as f:
            f.write(code_helper)

        res = locate_surgical_context("utility_calc", root_dir=tmpdir)
        assert "utility_calc" in res["matched_symbols"]
        assert "math.isqrt" in res["context"]
        assert "helper.py" in res["symbol_details"]["utility_calc"]["file"]


def test_locate_surgical_context_typescript():
    ts_service = """
import { Database } from './db';

export interface UserProfile {
    id: string;
    username: string;
}

export class UserService {
    private db: Database;

    constructor(db: Database) {
        this.db = db;
    }

    async getUser(id: string): Promise<UserProfile> {
        return { id, username: 'admin' };
    }
}
"""
    ts_client = """
import { UserService } from './service';

export async function handleRequest() {
    const service = new UserService(null);
    return await service.getUser('123');
}
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "service.ts"), "w", encoding="utf-8") as f:
            f.write(ts_service)
        with open(os.path.join(tmpdir, "client.ts"), "w", encoding="utf-8") as f:
            f.write(ts_client)

        res = locate_surgical_context("UserService", root_dir=tmpdir)
        assert "UserService" in res["matched_symbols"]
        assert "service.ts" in res["symbol_details"]["UserService"]["file"]
        assert res["symbol_details"]["UserService"]["type"] == "class"
        assert res["symbol_details"]["UserService"]["start_line"] == 9
        assert "```typescript" in res["context"]
        assert "client.ts" in res["references"]


def test_locate_surgical_context_go():
    go_server = """
package main

import (
\t"context"
\t"net/http"
)

type ServerConfig struct {
\tPort int
\tHost string
}

type APIServer struct {
\tcfg *ServerConfig
}

func (s *APIServer) Start(ctx context.Context) error {
\treturn http.ListenAndServe(":8080", nil)
}
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "server.go"), "w", encoding="utf-8") as f:
            f.write(go_server)

        res = locate_surgical_context("APIServer", root_dir=tmpdir)
        assert "APIServer" in res["matched_symbols"]
        assert "server.go" in res["symbol_details"]["APIServer"]["file"]
        assert res["symbol_details"]["APIServer"]["type"] == "struct"
        assert "```go" in res["context"]


def test_locate_surgical_context_rust():
    rs_app = """
use std::sync::Arc;

pub struct AppEngine {
    pub workers: usize,
}

impl AppEngine {
    pub fn new(workers: usize) -> Self {
        Self { workers }
    }
}

pub async fn run_engine(engine: Arc<AppEngine>) -> bool {
    engine.workers > 0
}
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "lib.rs"), "w", encoding="utf-8") as f:
            f.write(rs_app)

        res = locate_surgical_context("AppEngine", root_dir=tmpdir)
        assert "AppEngine" in res["matched_symbols"]
        assert "lib.rs" in res["symbol_details"]["AppEngine"]["file"]
        assert res["symbol_details"]["AppEngine"]["type"] == "struct"
        assert "```rust" in res["context"]

