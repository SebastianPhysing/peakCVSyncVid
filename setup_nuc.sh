# Sets up ~/pose-rec/.venv on each Nuc. It will be copied to all nucs and then created a venv.
set -e
cd "$(dirname "$0")"

[ -x .venv/bin/python ] || python3 -m venv .venv
PY=.venv/bin/python
CHECK="from ids_peak import ids_peak, ids_peak_ipl_extension"

# prefer the wheels shipped with the installed IDS peak SDK, PyPI as fallback
if ! $PY -c "$CHECK" 2>/dev/null; then
    for whl in $(find /usr /opt -name 'ids_peak*.whl' 2>/dev/null); do
        $PY -m pip install --quiet "$whl" || true
    done
fi
$PY -c "$CHECK" 2>/dev/null || $PY -m pip install --quiet ids_peak ids_peak_ipl
$PY -m pip install --quiet numpy opencv-python-headless

$PY -c "$CHECK; import cv2; print('setup ok')"
