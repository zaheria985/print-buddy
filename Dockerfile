# syntax=docker/dockerfile:1
# Source-build variant replacing the reviewed zaheria985 Docker contribution's
# binary download approach. See docs/DOCKER-117-PROGRESS.md for attribution.
FROM ubuntu:24.04 AS orca-build
ARG WITH_ORCA=false
ARG ORCA_CHANNEL=stable
ARG ORCA_STABLE_REF=8500fcdccaa10b5099ac20d252af3a7c560046f1
ARG ORCA_ALPHA_REF=3880f2c58ec392ed2bd5938ab31c9c7c496c0eb2
ENV DEBIAN_FRONTEND=noninteractive
RUN mkdir -p /out && \
    case "$WITH_ORCA" in true|false) ;; *) echo 'WITH_ORCA must be true or false' >&2; exit 2;; esac && \
    case "$ORCA_CHANNEL" in stable|alpha) ;; *) echo 'ORCA_CHANNEL must be stable or alpha' >&2; exit 2;; esac
RUN if [ "$WITH_ORCA" = true ]; then \
      apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates git cmake ninja-build build-essential autoconf automake \
        libtool pkg-config gettext texinfo curl wget file m4 \
        eglexternalplatform-dev extra-cmake-modules libcurl4-openssl-dev \
        libdbus-1-dev libglew-dev libgstreamerd-3-dev libgtk-3-dev \
        libmspack-dev libsecret-1-dev libspnav-dev libssl-dev libudev-dev \
        libwebkit2gtk-4.1-dev gstreamer1.0-gtk3 && \
      case "$ORCA_CHANNEL" in stable) ref="$ORCA_STABLE_REF";; alpha) ref="$ORCA_ALPHA_REF";; esac && \
      git init /src/orca && cd /src/orca && \
      git remote add origin https://github.com/OrcaSlicer/OrcaSlicer.git && \
      git fetch --depth 1 origin "$ref" && git checkout --detach FETCH_HEAD && \
      test "$(git rev-parse HEAD)" = "$ref" && \
      cmake -S deps -B deps/build -G Ninja -DCMAKE_BUILD_TYPE=Release && \
      cmake --build deps/build --parallel 1 && \
      cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release \
        -DSLIC3R_STATIC=ON -DSLIC3R_FHS=ON -DSLIC3R_PCH=OFF \
        -DCMAKE_INSTALL_PREFIX=/usr && \
      cmake --build build --parallel 1 && \
      DESTDIR=/out cmake --install build && \
      test -x /out/usr/bin/orca-slicer && \
      printf '%s\n' "$ref" > /out/orca-source-revision; \
    fi

FROM ubuntu:24.04 AS runtime
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv ca-certificates ffmpeg xvfb xauth tini \
    libwebkit2gtk-4.1-0 libgstreamer1.0-0 libgstreamer-plugins-base1.0-0 \
    libgtk-3-0t64 libglew2.2 libsecret-1-0 libspnav0 libgstreamerd-3-0 \
    fonts-liberation && rm -rf /var/lib/apt/lists/* && \
    python3 -m venv /opt/venv && useradd --uid 1000 --create-home buddy
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
