# Headless PipeWire for measuring the spatial renderer: a null sink stands in for the AirPods.
FROM archlinux:latest
RUN pacman -Syu --noconfirm pipewire pipewire-pulse wireplumber pipewire-audio libpulse libmysofa \
    python python-gobject python-numpy python-scipy python-h5py python-netcdf4 python-pip dbus && \
    pip install --break-system-packages sofar && pacman -Scc --noconfirm
ENV XDG_RUNTIME_DIR=/run/user/0 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/0/bus
WORKDIR /repo/tools/lab
CMD ["/repo/tools/lab/start.sh"]
