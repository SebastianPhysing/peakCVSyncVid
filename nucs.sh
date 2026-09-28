# This bash script helps you to controll all Nucs at once.
# You have to connect your laptop to the robothall network.
set -u


# ip ending --> camera name
declare -A NUCS=([12]=S [13]=N [14]=NW [15]=SW [16]=NE [17]=SE)
KEY="$HOME/.ssh/camera-nuc-stud.key"
SSH_OPTS=(-i "$KEY" -o BatchMode=yes -o ConnectTimeout=5)
REMOTE_DIR="pose-rec"
HERE="$(cd "$(dirname "$0")" && pwd)"

host() { echo "stud@192.168.0.$1"; }

# prints the usage lines at the top of this file
usage() { sed -n '5,10p' "$0"; exit 1; }

# run a command on all NUCs at the same time, every output line starts with the nuc name
run_all() {
    for ip in "${!NUCS[@]}"; do
        ssh "${SSH_OPTS[@]}" "$(host "$ip")" "$1" 2>&1 | sed -u "s/^/[nuc${NUCS[$ip]}] /" &
    done
    wait
}

case "${1:-}" in
deploy)
    for ip in "${!NUCS[@]}"; do
        ssh "${SSH_OPTS[@]}" "$(host "$ip")" "mkdir -p $REMOTE_DIR"
        scp -q "${SSH_OPTS[@]}" "$HERE/record.py" "$HERE/setup_nuc.sh" "$(host "$ip"):$REMOTE_DIR/"
    done
    run_all "bash $REMOTE_DIR/setup_nuc.sh"
    ;;

record)

    [ $# -eq 4 ] || usage
    run_all "cd $REMOTE_DIR && exec .venv/bin/python -u record.py recordings/$2 $3 $4"
    ;;
stop)
    run_all "pkill -INT -f 'record[.]py' || true"
    ;;


fetch)
    [ $# -eq 2 ] || usage
    for ip in "${!NUCS[@]}"; do
        dest="$HERE/recordings/$2/nuc${NUCS[$ip]}"
        mkdir -p "$dest"
        echo "fetching nuc${NUCS[$ip]}"
        ssh "${SSH_OPTS[@]}" "$(host "$ip")" "tar -C $REMOTE_DIR/recordings/$2 -cf - ." | tar -C "$dest" -xf -
    done
    ;;
clean)

    [ $# -eq 2 ] && [ -n "$2" ] || usage
    run_all "rm -rf $REMOTE_DIR/recordings/$2"
    ;;
*)
    usage
    ;;
esac
