# Lingkungan uji deployment: Debian Bookworm (basis Raspberry Pi OS Bookworm,
# Python 3.11) dengan systemd sungguhan sebagai PID 1.
FROM debian:bookworm
ENV container=docker DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends systemd systemd-sysv dbus curl git ca-certificates \
       python3 util-linux procps \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /lib/systemd/system/multi-user.target.wants/getty* \
    && useradd -m tester
STOPSIGNAL SIGRTMIN+3
CMD ["/lib/systemd/systemd"]
