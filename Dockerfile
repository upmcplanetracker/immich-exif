FROM python:3.13-slim

# tini: clean SIGTERM for child processes
# curl/perl/make: build ExifTool from source (Debian's packaged version lags
#                 significantly behind upstream; we always build the latest)
# perl stays in the image — the installed exiftool is a Perl script
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        tini \
        ca-certificates \
        curl \
        perl \
        make && \
    rm -rf /var/lib/apt/lists/*

# Install the latest ExifTool from source.
# exiftool.org only hosts the current tarball (not an archive), so we fetch
# the authoritative version number from ver.txt first, then build from that.
# Default install target is /usr/local/bin, which takes precedence over
# /usr/bin on Debian's default PATH.
RUN EXIFTOOL_VERSION=$(curl -fsSL https://exiftool.org/ver.txt) \
 && echo "Building ExifTool ${EXIFTOOL_VERSION}" \
 && curl -fsSL "https://sourceforge.net/projects/exiftool/files/Image-ExifTool-${EXIFTOOL_VERSION}.tar.gz/download" -o /tmp/et.tgz \
 && tar -xzf /tmp/et.tgz -C /tmp \
 && cd /tmp/Image-ExifTool-${EXIFTOOL_VERSION} \
 && perl Makefile.PL \
 && make install \
 && rm -rf /tmp/Image-ExifTool-* /tmp/et.tgz

# Confirm exiftool is on PATH and report the installed version.
RUN exiftool -ver

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

RUN mkdir -p /app/logs /app/state

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-u", "-m", "src.main"]
