#!/usr/bin/env bash
#
# arch-sidecar (Devrith Engine) Installer
# Universal Architectural Co-Processor & Context Decoupling Engine
#
# One-liner remote install:
#   curl -fsSL https://raw.githubusercontent.com/Hackedghost64/arch-sidecar/main/install.sh | bash
#
# Local install:
#   ./install.sh
#   ./install.sh --remote
#

set -eo pipefail

DEVRITH_REPO="${DEVRITH_REPO:-https://github.com/Hackedghost64/arch-sidecar.git}"
DEVRITH_REF="${DEVRITH_REF:-main}"
FORCE_REMOTE=false

# ANSI color formatting
if [[ -t 1 ]]; then
    BOLD=$'\e[1m'
    GREEN=$'\e[32m'
    BLUE=$'\e[34m'
    YELLOW=$'\e[33m'
    RED=$'\e[31m'
    CYAN=$'\e[36m'
    RESET=$'\e[0m'
else
    BOLD=""
    GREEN=""
    BLUE=""
    YELLOW=""
    RED=""
    CYAN=""
    RESET=""
fi

show_help() {
    cat <<EOF
${BOLD}arch-sidecar (Devrith) Installer${RESET}

Usage:
  ./install.sh [options]
  curl -fsSL https://raw.githubusercontent.com/Hackedghost64/arch-sidecar/main/install.sh | bash

Options:
  --remote            Force remote installation even if inside a local clone
  --ref, -r <ref>     Git branch or tag to install (default: main)
  --repo <url>        Git repository URL to install from
  -h, --help          Show this help message

Environment Variables:
  DEVRITH_REPO        Override git repository URL (default: https://github.com/Hackedghost64/arch-sidecar.git)
  DEVRITH_REF         Override git branch or tag (default: main)
EOF
}

# Parse command line options
while [[ $# -gt 0 ]]; do
    case "$1" in
        --remote)
            FORCE_REMOTE=true
            shift
            ;;
        --ref|-r)
            if [[ -z "${2:-}" ]]; then
                echo "${RED}Error: --ref requires an argument${RESET}" >&2
                exit 1
            fi
            DEVRITH_REF="$2"
            shift 2
            ;;
        --repo)
            if [[ -z "${2:-}" ]]; then
                echo "${RED}Error: --repo requires an argument${RESET}" >&2
                exit 1
            fi
            DEVRITH_REPO="$2"
            shift 2
            ;;
        -h|--help)
            show_help
            exit 0
            ;;
        *)
            echo "${RED}Unknown option: $1${RESET}" >&2
            echo "Run with --help for usage." >&2
            exit 1
            ;;
    esac
done

echo "${BOLD}${CYAN}"
echo "   ___          __  ____   _    __     "
echo "  / _ | _______/ /_/ __/  (_)__/ /__   "
echo " / __ |/ __/ __/ _ /\ \  / / _  / -_)  "
echo "/_/ |_/_/  \__/_//_/___//_/\_,_/\__/   "
echo "  Devrith Architectural Co-Processor   "
echo "${RESET}"

# Verify Python 3 presence
if ! command -v python3 >/dev/null 2>&1; then
    echo "${RED}Error: python3 is not installed or not in PATH.${RESET}" >&2
    echo "Please install Python 3.9+ and try again." >&2
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "0.0")
PY_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [[ "$PY_MAJOR" -lt 3 ]] || { [[ "$PY_MAJOR" -eq 3 ]] && [[ "$PY_MINOR" -lt 9 ]]; }; then
    echo "${RED}Error: Python 3.9 or higher is required (detected Python ${PYTHON_VERSION}).${RESET}" >&2
    exit 1
fi

# Fallback helper for pip install (handles PEP 668 externally managed environment & system python quirks)
run_pip_install() {
    if python3 -m pip install "$@"; then
        return 0
    fi
    echo "${YELLOW}Standard pip install failed. Retrying with PEP 668 / build isolation fallbacks...${RESET}"
    if python3 -m pip install --break-system-packages "$@" 2>/dev/null; then
        return 0
    fi
    if python3 -m pip install --no-build-isolation "$@"; then
        return 0
    fi
    python3 -m pip install --no-build-isolation --break-system-packages "$@"
}

# Detect if running inside local arch-sidecar repository checkout
is_local_checkout() {
    if [[ "$FORCE_REMOTE" == "true" ]]; then
        return 1
    fi
    if [[ -f "pyproject.toml" ]] && grep -q 'name *= *"arch-sidecar"' "pyproject.toml" 2>/dev/null; then
        return 0
    fi
    if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
        local dir
        dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
        if [[ -f "${dir}/pyproject.toml" ]] && grep -q 'name *= *"arch-sidecar"' "${dir}/pyproject.toml" 2>/dev/null; then
            cd "${dir}" || return 1
            return 0
        fi
    fi
    return 1
}

# Package installation dispatch
TARGET_PKG="git+${DEVRITH_REPO}@${DEVRITH_REF}"

if is_local_checkout; then
    echo "${BLUE}Detected local arch-sidecar repository checkout.${RESET}"
    echo "${BLUE}Installing in editable mode: python3 -m pip install -e .${RESET}"
    run_pip_install -e .
elif command -v uv >/dev/null 2>&1; then
    echo "${BLUE}Detected 'uv'. Installing via uv tool...${RESET}"
    echo "${BLUE}Running: uv tool install --force \"${TARGET_PKG}\"${RESET}"
    uv tool install --force "${TARGET_PKG}"
elif command -v pipx >/dev/null 2>&1; then
    echo "${BLUE}Detected 'pipx'. Installing via pipx...${RESET}"
    echo "${BLUE}Running: pipx install --force \"${TARGET_PKG}\"${RESET}"
    pipx install --force "${TARGET_PKG}"
else
    echo "${BLUE}Installing via user pip...${RESET}"
    echo "${BLUE}Running: python3 -m pip install --user --upgrade \"${TARGET_PKG}\"${RESET}"
    run_pip_install --user --upgrade "${TARGET_PKG}"
fi

# Check PATH for ~/.local/bin
check_path() {
    local bin_dir="${HOME}/.local/bin"
    case ":${PATH}:" in
        *":${bin_dir}:"*) ;;
        *)
            echo ""
            echo "${YELLOW}⚠️  Notice: ${bin_dir} is not in your current PATH.${RESET}"
            echo "   To use 'arch-sidecar' directly from any terminal session, add it to your profile:"
            echo "   ${BOLD}export PATH=\"\$HOME/.local/bin:\$PATH\"${RESET}"
            echo "   Add that line to ~/.bashrc or ~/.zshrc, then reload your shell."
            echo ""
            export PATH="${bin_dir}:${PATH}"
            ;;
    esac
}
check_path

# Verify installation
echo ""
echo "${BLUE}Verifying installation...${RESET}"
VERSION_OUTPUT=""
if command -v arch-sidecar >/dev/null 2>&1; then
    VERSION_OUTPUT="$(arch-sidecar --version 2>&1 || true)"
elif [[ -x "${HOME}/.local/bin/arch-sidecar" ]]; then
    VERSION_OUTPUT="$("${HOME}/.local/bin/arch-sidecar" --version 2>&1 || true)"
fi

if [[ -n "$VERSION_OUTPUT" ]]; then
    echo "${GREEN}✓ Verified:${RESET} ${VERSION_OUTPUT}"
else
    echo "${YELLOW}Notice: 'arch-sidecar' command installed, but not currently found on active PATH.${RESET}"
    echo "Make sure ${HOME}/.local/bin is added to your PATH."
fi

# Print clean summary banner
echo ""
echo "${BOLD}${GREEN}====================================================================${RESET}"
echo "${BOLD}${GREEN}   🚀 arch-sidecar (Devrith) is ready!${RESET}"
echo "${BOLD}${GREEN}====================================================================${RESET}"
echo ""
echo "${BOLD}Quick Start & CLI Commands:${RESET}"
echo "  • Feature Plan & Verification:   arch-sidecar feature \"<task>\" <repo>"
echo "  • Architectural Digest:          arch-sidecar digest <repo>"
echo "  • Preflight Scope Check:         arch-sidecar check <repo>"
echo "  • Quality & Complexity Audit:    arch-sidecar audit <repo>"
echo "  • Update to Latest Version:      arch-sidecar update"
echo ""
echo "${BOLD}Model Context Protocol (MCP) Server Setup:${RESET}"
echo "  Add arch-sidecar to your Claude Desktop, Cursor, or Antigravity config:"
echo ""
echo "  {"
echo "    \"mcpServers\": {"
echo "      \"arch-sidecar\": {"
echo "        \"command\": \"arch-sidecar\","
echo "        \"args\": [\"serve\"]"
echo "      }"
echo "    }"
echo "  }"
echo ""
echo "${BOLD}${GREEN}====================================================================${RESET}"
