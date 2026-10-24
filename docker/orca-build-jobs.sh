#!/bin/sh
# Print the number of parallel compile jobs for the OrcaSlicer source build.
#
#   orca-build-jobs.sh [auto | N]      (default: $ORCA_BUILD_JOBS, else auto)
#
# N must be a whole number from 1 to 64. "auto" is conservative and memory-aware: one job per
# ORCA_BUILD_MEM_PER_JOB_MIB of memory (default 2560 MiB, the order of a large libslic3r translation
# unit), at most the CPUs this build may use minus one, and never fewer than 1. It never simply
# takes every host CPU. CPUs come from nproc and a cgroup v2 cpu.max quota; memory from a cgroup
# v2 memory.max limit, else MemAvailable. The reasoning goes to stderr, the number to stdout.
#
# ORCA_JOBS_ROOT prefixes /proc and /sys for tests; ORCA_JOBS_NPROC replaces nproc.
set -eu

if [ "$#" -ge 1 ]; then request=$1; else request=${ORCA_BUILD_JOBS:-auto}; fi
per_job_mib=${ORCA_BUILD_MEM_PER_JOB_MIB:-2560}
root=${ORCA_JOBS_ROOT:-}

fail() {
    echo "orca-build-jobs: $*" >&2
    exit 2
}

case "$per_job_mib" in
    '' | *[!0-9]*) fail "ORCA_BUILD_MEM_PER_JOB_MIB must be a whole number of MiB, got '$per_job_mib'" ;;
esac
[ "$per_job_mib" -ge 256 ] || fail "ORCA_BUILD_MEM_PER_JOB_MIB must be at least 256, got $per_job_mib"

case "$request" in
    auto) ;;
    '' | *[!0-9]* | 0*) fail "ORCA_BUILD_JOBS must be auto or a whole number from 1 to 64, got '$request'" ;;
    *)
        [ "$request" -le 64 ] || fail "ORCA_BUILD_JOBS must be at most 64, got $request"
        echo "orca-build-jobs: $request (explicit)" >&2
        echo "$request"
        exit 0
        ;;
esac

cpus=${ORCA_JOBS_NPROC:-$(nproc)}
if [ -r "$root/sys/fs/cgroup/cpu.max" ]; then
    read -r quota period < "$root/sys/fs/cgroup/cpu.max"
    if [ "$quota" != max ] && [ "$period" -gt 0 ]; then
        quota_cpus=$(( (quota + period - 1) / period ))
        [ "$quota_cpus" -lt "$cpus" ] && cpus=$quota_cpus
    fi
fi
[ "$cpus" -gt 1 ] && cpus=$((cpus - 1))

mem_kib=
if [ -r "$root/sys/fs/cgroup/memory.max" ]; then
    limit=$(cat "$root/sys/fs/cgroup/memory.max")
    [ "$limit" != max ] && mem_kib=$((limit / 1024))
fi
if [ -z "$mem_kib" ]; then
    mem_kib=$(awk '/^MemAvailable:/ {print $2}' "$root/proc/meminfo")
fi
[ -n "$mem_kib" ] || fail "cannot read available memory"
by_mem=$((mem_kib / (per_job_mib * 1024)))

jobs=$cpus
[ "$by_mem" -lt "$jobs" ] && jobs=$by_mem
[ "$jobs" -ge 1 ] || jobs=1
echo "orca-build-jobs: $jobs (auto: $cpus usable CPUs, $((mem_kib / 1024)) MiB memory / $per_job_mib MiB per job)" >&2
echo "$jobs"
