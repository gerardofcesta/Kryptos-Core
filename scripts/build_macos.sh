#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${KRYPTOS_VERSION:-${LOCAL_LLM_VERSION:-3.0.0}}"
ARCH="$(uname -m)"
WEB_DIR="${REPO_ROOT}/workspaces/offline-chat/web"
DIST_ROOT="${REPO_ROOT}/dist"
OUT_DIR="${REPO_ROOT}/installer/out"

mkdir -p "${OUT_DIR}"

echo "=== Kryptos Core macOS Build Pipeline (v${VERSION}, ${ARCH}) ==="
echo "[1/4] Building zero-CDN React/Vite SPA..."
pushd "${WEB_DIR}" >/dev/null
if [[ ! -d node_modules ]]; then
  npm ci
fi
npm run build
popd >/dev/null

echo "[2/4] Packaging KryptosCore.app via PyInstaller..."
PY_BIN="${REPO_ROOT}/.venv/bin/python"
if [[ ! -x "${PY_BIN}" ]]; then
  PY_BIN="$(command -v python3)"
fi

"${PY_BIN}" -m PyInstaller \
  --noconfirm \
  --clean \
  --onedir \
  --windowed \
  --name "KryptosCore" \
  --distpath "${DIST_ROOT}" \
  --workpath "${REPO_ROOT}/build/pyinstaller-macos" \
  --specpath "${REPO_ROOT}/build" \
  --paths "${REPO_ROOT}" \
  --add-data "${WEB_DIR}/dist:web/dist" \
  --add-data "${REPO_ROOT}/config:config" \
  --add-data "${REPO_ROOT}/prompts:prompts" \
  --hidden-import "workspaces.local_llm_core.app" \
  --hidden-import "workspaces.local_llm_core.desktop" \
  --hidden-import "workspaces.local_llm_core.cli" \
  "${REPO_ROOT}/workspaces/local_llm_core/desktop.py"

DMG_NAME="KryptosCore-v${VERSION}-macos-${ARCH}.dmg"
echo "[3/4] Creating macOS disk image (${DMG_NAME})..."
if command -v hdiutil >/dev/null 2>&1; then
  STAGING="${REPO_ROOT}/build/dmg-staging"
  rm -rf "${STAGING}"
  mkdir -p "${STAGING}"
  cp -R "${DIST_ROOT}/KryptosCore.app" "${STAGING}/KryptosCore.app"
  ln -s /Applications "${STAGING}/Applications"
  hdiutil create -volname "Kryptos Core ${VERSION}" -srcfolder "${STAGING}" -ov -format UDZO "${OUT_DIR}/${DMG_NAME}"
else
  echo "hdiutil not found (non-macOS host); creating zip archive fallback."
  ditto -c -k --keepParent "${DIST_ROOT}/KryptosCore.app" "${OUT_DIR}/KryptosCore-v${VERSION}-macos-${ARCH}.zip" || true
fi

echo "[4/4] Generating SHA256SUMS.txt and CycloneDX SBOM..."
"${PY_BIN}" "${REPO_ROOT}/scripts/generate_release_manifest.py" \
  --artifacts-dir "${OUT_DIR}" \
  --repo-root "${REPO_ROOT}" \
  --version "${VERSION}"
