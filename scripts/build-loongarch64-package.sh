#!/usr/bin/env bash
# Build inside the Debian loong64 container with the chipdb artifacts at hand.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

test "$(dpkg --print-architecture)" = loong64
test -f "${OPENXC7_PARTS_INDEX:?set the parts inventory path}"
test -d chipdb-bins

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    build-essential ca-certificates cmake curl cython3 file git ninja-build pkg-config \
    libantlr4-runtime-dev libboost-iostreams-dev libboost-program-options-dev \
    libboost-thread-dev libyaml-dev openjdk-21-jre-headless python-is-python3 \
    python3-dev python3-pip python3-setuptools python3-wheel uuid-dev yosys
git config --global --add safe.directory "$PWD"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
export CMAKE_POLICY_VERSION_MINIMUM=3.5
export CMAKE_BUILD_PARALLEL_LEVEL="$(nproc)"

# Keep Eigen 3.4 from the main release's Nix build. Forky also carries Eigen 5.
eigen_deb="$work/libeigen3-dev_3.4.0-5_all.deb"
curl -fsSL --retry 3 -o "$eigen_deb" \
    https://deb.debian.org/debian/pool/main/e/eigen3/libeigen3-dev_3.4.0-5_all.deb
echo "123ff96e6929713b852f58a53727379149fc4a2b682bd04605e784f6620ac08e  $eigen_deb" | sha256sum -c
dpkg -i "$eigen_deb"

python3 -c '
import json
with open("nix/revisions.json", encoding="utf-8") as source:
    for name, entry in json.load(source).items():
        print(name, "{owner}/{repo}".format(**entry), entry["rev"], entry["submodules"])
' > "$work/revisions"
while read -r name repository rev submodules; do
    src="$work/$name"
    git init -q "$src"
    git -C "$src" remote add origin "https://github.com/$repository.git"
    git -C "$src" fetch -q --depth=1 origin "$rev"
    git -C "$src" checkout -q --detach FETCH_HEAD
    if [ "$submodules" = True ]; then
        git -C "$src" submodule update --init --recursive --depth=1
    fi
done < "$work/revisions"

nextpnr="$work/nextpnr"
db="$work/prjxray-db"
xray="$work/prjxray"
nextpnr_rev=$(git -C "$nextpnr" rev-parse HEAD)
cmake -S "$nextpnr" -B "$work/nextpnr-build" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DARCH=himbaechel -DHIMBAECHEL_UARCH=xilinx \
    -DHIMBAECHEL_PRJXRAY_DB="$db" -DHIMBAECHEL_XILINX_DEVICES= \
    -DBUILD_GUI=OFF -DBUILD_TESTS=OFF -DBUILD_PYTHON=OFF -DUSE_IPO=OFF \
    -DCURRENT_GIT_VERSION="${nextpnr_rev:0:8}"
cmake --build "$work/nextpnr-build" --target nextpnr-himbaechel bbasm
install -Ds "$work/nextpnr-build/nextpnr-himbaechel" "$work/nextpnr-tools/bin/nextpnr-xilinx"
install -Ds "$work/nextpnr-build/bba/bbasm" "$work/nextpnr-tools/bin/bbasm"
mkdir -p "$work/nextpnr-tools/share/nextpnr/external"
ln -s "$db" "$work/nextpnr-tools/share/nextpnr/external/prjxray-db"

cmake -S "$xray" -B "$work/prjxray-build" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_FLAGS=-Wno-deprecated
cmake --build "$work/prjxray-build" --target xc7frames2bit bitread xc7patch
mkdir -p "$work/prjxray-tools/bin"
for tool in xc7frames2bit bitread xc7patch; do
    install -s "$work/prjxray-build/tools/$tool" "$work/prjxray-tools/bin/$tool"
done
for tool in fasm2frames bit2fasm; do
    install "$xray/prjxray/$tool.py" "$work/prjxray-tools/bin/$tool"
done

# Build the same native ANTLR parser as the Nix package.
sed -i "s/self.antlr_runtime = 'static'/self.antlr_runtime = 'shared'/" "$work/fasm/setup.py"
ANTLR4_RUNTIME_INCLUDE=/usr/include/antlr4-runtime python3 -m pip install \
    --no-build-isolation --target "$work/python" "$work/fasm" \
    pyyaml simplejson intervaltree sortedcontainers
mkdir -p "$work/fasm-tools/bin"
mv "$work/python/bin/fasm" "$work/fasm-tools/bin/"

platform=linux-loongarch64
dateid="${OPENXC7_PACK_DATE//-/}"
package="openxc7-toolchain-$platform-$dateid.tgz"
EIGEN_VERSION=$(pkg-config --modversion eigen3) \
    YOSYS_VERSION=$(dpkg-query -W -f='${Version}' yosys) \
    bash scripts/build-info.sh "$platform" "$OPENXC7_PACK_DATE" "$package" BUILD-INFO.json

PATH="$work/nextpnr-tools/bin:$work/prjxray-tools/bin:$work/fasm-tools/bin:$PATH" \
    PYTHONPATH="$work/python:$xray" \
    OPENXC7_BUILD_INFO="$PWD/BUILD-INFO.json" OPENXC7_CHIPDB_SEED="$PWD/chipdb-bins" \
    python3 openxc7-pack.py

bash scripts/validate-package.sh "$package" --expect-date "$dateid"
bash scripts/fetch-demos.sh
bash scripts/regress.sh "$package" \
    --json regress-report.json --markdown regress-report.md
