#!/usr/bin/env sh
# Runs the EQOA server and restarts it when the admin panel asks (exit code 75),
# e.g. after "Update & restart". Pass your usual options, for example:
#   ./start_server.sh --host 0.0.0.0 --public-host 192.168.1.51
# Set PYTHON to use a specific interpreter (default: python3).

# Everything lives in a function so the whole script is parsed before it runs,
# which keeps it safe if an update changes this file.
main() {
    cd "$(dirname "$0")" || exit 1
    export EQOA_LAUNCHER=1
    python="${PYTHON:-python3}"
    while true; do
        "$python" -m eqoa_server "$@"
        code=$?
        [ "$code" -eq 75 ] || return "$code"
        echo "Restarting the server..."
    done
}

main "$@"
exit $?
