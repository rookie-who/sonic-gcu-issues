FROM python:3.9-slim-bullseye

# Install build dependencies for libyang v1.0-r4
RUN apt-get update && apt-get install -y \
    git cmake build-essential libpcre3-dev swig \
    python3-dev libcmocka-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Build libyang v1.0-r4 (same version SONiC uses)
WORKDIR /build
RUN git clone https://github.com/CESNET/libyang.git && \
    cd libyang && \
    git checkout v1.0-r4 && \
    mkdir build && cd build && \
    cmake .. \
        -DCMAKE_INSTALL_PREFIX=/usr \
        -DGEN_LANGUAGE_BINDINGS=ON \
        -DGEN_PYTHON_BINDINGS=ON \
        -DPYTHON_MODULE_PATH=/usr/lib/python3/dist-packages \
        -DGEN_CPP_BINDINGS=ON \
        -DCMAKE_BUILD_TYPE=Release && \
    make -j$(nproc) && \
    make install && \
    ldconfig

# Make the Python yang bindings importable
ENV PYTHONPATH=/usr/lib/python3/dist-packages

# Install sonic-yang-mgmt dependencies
RUN pip install xmltodict==0.12.0 ijson jsonpointer jsondiff tabulate jsonpatch jinja2

# Get sources and prepare yang models
WORKDIR /sonic
COPY render_yang.py /sonic/render_yang.py

RUN git clone --depth=1 https://github.com/sonic-net/sonic-buildimage.git && \
    cp sonic-buildimage/src/sonic-yang-mgmt/sonic_yang.py . && \
    cp sonic-buildimage/src/sonic-yang-mgmt/sonic_yang_ext.py . && \
    cp sonic-buildimage/src/sonic-yang-mgmt/sonic_yang_path.py . && \
    sed -i 's/syslog.syslog(debug, msg)/syslog.syslog(debug, str(msg))/' sonic_yang.py && \
    mkdir -p yang-models && \
    cp sonic-buildimage/src/sonic-yang-models/yang-models/*.yang yang-models/ && \
    python3 render_yang.py

# Get sonic-utilities GCU code
RUN git clone --depth=1 --branch 202412 https://github.com/Azure/sonic-utilities.msft.git sonic-utilities && \
    cp -r sonic-utilities/generic_config_updater/ .

# Copy repro script
COPY repro_real.py /sonic/repro_real.py

WORKDIR /sonic
CMD ["python3", "repro_real.py"]
