#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${KRYPTOS_VERSION:-${LOCAL_LLM_VERSION:-3.0.0}}"
ARCH="$(uname -m)"
WEB_DIR="${REPO_ROOT}/workspaces/offline-chat/web"
DIST_ROOT="${REPO_ROOT}/dist"
OUT_DIR="${REPO_ROOT}/installer/out"
APP_BUNDLE="${DIST_ROOT}/KryptosCore"

mkdir -p "${OUT_DIR}"

echo "=== Kryptos Core Linux Build Pipeline (v${VERSION}, ${ARCH}) ==="
echo "[1/4] Building zero-CDN React/Vite SPA..."
pushd "${WEB_DIR}" >/dev/null
if [[ ! -d node_modules ]]; then
  npm ci
fi
npm run build
popd >/dev/null

echo "[2/4] Packaging PyInstaller onedir bundle..."
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
  --workpath "${REPO_ROOT}/build/pyinstaller-linux" \
  --specpath "${REPO_ROOT}/build" \
  --paths "${REPO_ROOT}" \
  --add-data "${WEB_DIR}/dist:web/dist" \
  --add-data "${REPO_ROOT}/config:config" \
  --add-data "${REPO_ROOT}/prompts:prompts" \
  --hidden-import "workspaces.local_llm_core.app" \
  --hidden-import "workspaces.local_llm_core.desktop" \
  --hidden-import "workspaces.local_llm_core.cli" \
  "${REPO_ROOT}/workspaces/local_llm_core/desktop.py"

TAR_NAME="KryptosCore-v${VERSION}-linux-${ARCH}.tar.gz"
tar -C "${DIST_ROOT}" -czf "${OUT_DIR}/${TAR_NAME}" "KryptosCore"
echo "[3/4] Created tarball: ${OUT_DIR}/${TAR_NAME}"

if command -v appimagetool >/dev/null 2>&1; then
  APPDIR="${REPO_ROOT}/build/KryptosCore.AppDir"
  rm -rf "${APPDIR}"
  mkdir -p "${APPDIR}/usr/bin"
  cp -a "${APP_BUNDLE}/." "${APPDIR}/usr/bin/"
  cat > "${APPDIR}/kryptos-core.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=Kryptos Core
Comment=Sovereign Offline Loopback AI Workstation & Gateway
Exec=KryptosCore
Icon=kryptos-core
Categories=Development;Utility;
Terminal=false
DESKTOP
  cat > "${APPDIR}/AppRun" <<'APPRUN'
#!/usr/bin/env bash
HERE="$(dirname "$(readlink -f "$0")")"
exec "${HERE}/usr/bin/KryptosCore" "$@"
APPRUN
  chmod +x "${APPDIR}/AppRun"
  ARCH="${ARCH}" appimagetool "${APPDIR}" "${OUT_DIR}/KryptosCore-${ARCH}.AppImage"
  echo "[3/4] Created AppImage: ${OUT_DIR}/KryptosCore-${ARCH}.AppImage"
else
  echo "[3/4] appimagetool not found on PATH; skipping optional .AppImage step (tar.gz ready)."
fi

echo "[4/4] Generating SHA256SUMS.txt and CycloneDX SBOM..."
"${PY_BIN}" "${REPO_ROOT}/scripts/generate_release_manifest.py" \
  --artifacts-dir "${OUT_DIR}" \
  --repo-root "${REPO_ROOT}" \
  --version "${VERSION}"
