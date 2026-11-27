#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_SHARE="${HOME}/.local/share/applications"
BIN_DIR="${HOME}/.local/bin"
DESKTOP_FILE="${APP_SHARE}/kryptos-core.desktop"

mkdir -p "${APP_SHARE}" "${BIN_DIR}"

echo "=== Kryptos Core Linux User-Space Installer ==="

if command -v uv >/dev/null 2>&1 && [[ -f "${REPO_ROOT}/pyproject.toml" ]]; then
  echo "[1/3] Installing kryptos-core & kryptos-desktop via 'uv tool install'..."
  uv tool install --force "${REPO_ROOT}"
elif [[ -x "${REPO_ROOT}/KryptosCore" ]]; then
  echo "[1/3] Linking extracted KryptosCore bundle into ${BIN_DIR}..."
  ln -sf "${REPO_ROOT}/KryptosCore" "${BIN_DIR}/kryptos-desktop"
else
  echo "[1/3] Installing with python3 -m pip --user..."
  python3 -m pip install --user --no-build-isolation "${REPO_ROOT}"
fi

EXEC_CMD="${BIN_DIR}/kryptos-desktop"
cat > "${DESKTOP_FILE}" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Kryptos Core
Comment=100% Offline Sovereign Loopback AI Workstation
Exec=${EXEC_CMD}
Categories=Development;Utility;
Terminal=false
DESKTOP

echo "[2/3] Desktop entry registered at ${DESKTOP_FILE}"
echo "[3/3] Probing GUI / Browser fallback runtime:"
if python3 -c "import gi; gi.require_version('WebKit2', '4.1')" >/dev/null 2>&1; then
  echo "  - Native WebKit2GTK detected (native window mode ready)."
elif command -v chromium >/dev/null 2>&1 || command -v google-chrome >/dev/null 2>&1 || command -v chromium-browser >/dev/null 2>&1; then
  echo "  - Chromium/Chrome detected (isolated '--app=' standalone window fallback ready)."
else
  echo "  - Headless environment detected: use 'kryptos-core serve' and 'kryptos-core chat'."
fi

echo ""
echo "Ready! Quick commands:"
echo "  kryptos-core doctor          # Check GPU/RAM sizing"
echo "  kryptos-core models list     # Inspect Hardware-Fit model catalog"
echo "  kryptos-core serve           # Run loopback OpenAI/MCP gateway headless"
echo "  kryptos-desktop              # Launch desktop surface"
