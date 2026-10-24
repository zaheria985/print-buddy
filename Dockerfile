# syntax=docker/dockerfile:1
# Source-build variant replacing the reviewed zaheria985 Docker contribution's
# binary download approach. See docs/DOCKER-117-PROGRESS.md for attribution.
FROM ubuntu:24.04 AS orca-build
ARG WITH_ORCA=false
ARG ORCA_CHANNEL=stable
ARG ORCA_STABLE_REF=8500fcdccaa10b5099ac20d252af3a7c560046f1
ARG ORCA_ALPHA_REF=3880f2c58ec392ed2bd5938ab31c9c7c496c0eb2
# Parallel compile jobs: auto (memory-aware, below the CPU count; see docker/orca-build-jobs.sh)
# or an explicit 1-64. Applied to every dependency and to the application build.
ARG ORCA_BUILD_JOBS=auto
ARG ORCA_BUILD_MEM_PER_JOB_MIB=2560
ARG ORCA_CCACHE_MAXSIZE=5G
# The fixed CMake option set below. Rename it when those options change.
ARG ORCA_BUILD_PROFILE=release-static-fhs-nopch
ARG TARGETARCH
ENV DEBIAN_FRONTEND=noninteractive
COPY docker/orca-build-jobs.sh /usr/local/bin/orca-build-jobs
RUN mkdir -p /out && \
    case "$WITH_ORCA" in true|false) ;; *) echo 'WITH_ORCA must be true or false' >&2; exit 2;; esac && \
    case "$ORCA_CHANNEL" in stable|alpha) ;; *) echo 'ORCA_CHANNEL must be stable or alpha' >&2; exit 2;; esac && \
    orca-build-jobs "$ORCA_BUILD_JOBS" >/dev/null
# The cache mount keeps the checkout, downloaded dependency sources, finished dependencies, Ninja
# objects and ccache across builds, including failed or cancelled ones. Its id separates channels,
# pinned revisions, architectures and option profiles, so none of them can share a cache. Compiler,
# CMake and OS changes reset the build trees (ccache validates every object itself).
RUN --mount=type=cache,id=print-buddy-orca-${TARGETARCH}-${ORCA_CHANNEL}-${ORCA_STABLE_REF}-${ORCA_ALPHA_REF}-${ORCA_BUILD_PROFILE},target=/cache,sharing=locked \
    if [ "$WITH_ORCA" = true ]; then \
      apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates git cmake ninja-build build-essential autoconf automake \
        libtool pkg-config gettext texinfo curl wget file m4 ccache \
        eglexternalplatform-dev extra-cmake-modules libcurl4-openssl-dev \
        libdbus-1-dev libglew-dev libgstreamerd-3-dev libgtk-3-dev \
        libmspack-dev libsecret-1-dev libspnav-dev libssl-dev libudev-dev \
        libwebkit2gtk-4.1-dev gstreamer1.0-gtk3 && \
      case "$ORCA_CHANNEL" in stable) ref="$ORCA_STABLE_REF";; alpha) ref="$ORCA_ALPHA_REF";; esac && \
      jobs="$(orca-build-jobs "$ORCA_BUILD_JOBS")" && \
      export CMAKE_BUILD_PARALLEL_LEVEL="$jobs" CC=/usr/lib/ccache/gcc CXX=/usr/lib/ccache/g++ \
        CCACHE_DIR=/cache/ccache CCACHE_MAXSIZE="$ORCA_CCACHE_MAXSIZE" && \
      toolchain="$(. /etc/os-release && printf '%s\n' "$ID-$VERSION_ID" "$(gcc -dumpfullversion)" \
        "$(cmake --version | head -n 1)" "$CC" "$CXX" "$ORCA_BUILD_PROFILE" | sha256sum | cut -c1-16)" && \
      if [ "$(cat /cache/toolchain 2>/dev/null)" != "$toolchain" ]; then \
        rm -rf /cache/orca/deps/build /cache/orca/build && printf '%s\n' "$toolchain" > /cache/toolchain; \
      fi && \
      if [ "$(git -C /cache/orca rev-parse HEAD 2>/dev/null)" != "$ref" ]; then \
        rm -rf /cache/orca && git init /cache/orca && \
        git -C /cache/orca remote add origin https://github.com/OrcaSlicer/OrcaSlicer.git && \
        git -C /cache/orca fetch --depth 1 origin "$ref" && git -C /cache/orca checkout --detach FETCH_HEAD; \
      fi && \
      cd /cache/orca && \
      test "$(git rev-parse HEAD)" = "$ref" && \
      test -z "$(git status --porcelain --untracked-files=no)" && \
      cmake -S deps -B deps/build -G Ninja -DCMAKE_BUILD_TYPE=Release && \
      cmake --build deps/build --parallel 1 && \
      cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release \
        -DSLIC3R_STATIC=ON -DSLIC3R_FHS=ON -DSLIC3R_PCH=OFF \
        -DCMAKE_INSTALL_PREFIX=/usr && \
      cmake --build build --parallel "$jobs" && \
      DESTDIR=/out cmake --install build && \
      test -x /out/usr/bin/orca-slicer && \
      printf '%s\n' "$ref" > /out/orca-source-revision && \
      printf 'channel=%s\nrevision=%s\narch=%s\nprofile=%s\ntoolchain=%s\njobs=%s\n' \
        "$ORCA_CHANNEL" "$ref" "$TARGETARCH" "$ORCA_BUILD_PROFILE" "$toolchain" "$jobs" > /out/orca-build-info && \
      ccache --show-stats; \
    fi

FROM ubuntu:24.04 AS runtime
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv ca-certificates ffmpeg xvfb xauth tini \
    libwebkit2gtk-4.1-0 libgstreamer1.0-0 libgstreamer-plugins-base1.0-0 \
    libgtk-3-0t64 libglew2.2 libsecret-1-0 libspnav0 libgstreamerd-3-0 \
    fonts-liberation && rm -rf /var/lib/apt/lists/* && \
    python3 -m venv /opt/venv && \
    # ubuntu:24.04 ships an `ubuntu` user with UID 1000; free the UID for buddy.
    if id -u ubuntu >/dev/null 2>&1; then userdel --remove ubuntu; fi && \
    useradd --uid 1000 --create-home buddy
COPY --from=orca-build /out/ /
ARG APP_CHANNEL=development
RUN case "$APP_CHANNEL" in stable|development) ;; *) echo 'APP_CHANNEL must be stable or development' >&2; exit 2;; esac
LABEL org.print-buddy.app-channel=$APP_CHANNEL
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PRINT_BUDDY_CONFIG=/app/config/docker.yaml HOME=/data \
    PRINT_BUDDY__BOT__MODE=standalone
RUN mkdir -p /data/hermes/state /data/archive /data/records /data/cache && \
    chown -R buddy:buddy /data && chmod +x /app/docker/entrypoint.sh
USER buddy
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
CMD ["python3", "-m", "print_buddy.bot"]
