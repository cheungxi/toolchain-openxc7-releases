"""Binary relocation: executables, dynamic libraries and python deps.

Linux backend: the ELF closure is read with ``ldd`` and the binaries are
re-executed through the bundled ``ld-linux`` loader (see the wrappers in
pack.components). Darwin backend: the dylib closure + @rpath relocation
is resolved globally at the end of the flow by ``macpack.relocate_dist()``
(``macpack.py`` at the repo root), followed by the ad-hoc codesign --
signing must come after relocation.
"""

import importlib.util
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
from pathlib import Path

import ansi

from . import DIST, BIN, LIBEXEC, LIB, PRIVATE_LIB
from .platform import IS_DARWIN, LINUX_LOADERS, LOONGARCH64, plat_token

# -- The macOS (Mach-O) packaging backend. Only imported on Darwin; the
# -- CLI shim calls macpack.relocate_dist() once, after all binaries,
# -- libraries and python files have been copied into dist/.
if IS_DARWIN:
    import macpack  # noqa: F401  (darwin relocation backend, used by the shim)


IS_LOONGARCH64 = sys.platform == "linux" and plat_token() == LOONGARCH64
LOADER_NAME = LINUX_LOADERS[LOONGARCH64 if IS_LOONGARCH64 else "linux-x86-64"]
LINUX_LOADER = f"{LIBEXEC}/{LOADER_NAME}"
PYTHON_NAME = (
    f"python{sys.version_info.major}.{sys.version_info.minor}"
    if IS_LOONGARCH64 else "python3.12"
)


def python_stdlib(executable: Path) -> Path:
    if IS_LOONGARCH64:
        return Path(sysconfig.get_path("stdlib"))
    return executable.parent.parent / LIB / PYTHON_NAME


def linux_loader_exec(program: str) -> str:
    """Shell text that execs *program* through the bundled Linux loader.

    *program* is one shell word, quotes included when it has any.
    ``--library-path`` is ``PRIVATE_LIB``: that is the process search
    path, including the later dlopen of libffi from ``_ctypes`` and of
    libantlr4.
    It is the file the kernel executes, so ``/proc/self/exe`` points at
    it, and nextpnr resolves ``share/`` as ``../share/nextpnr`` from that
    directory (``common/kernel/command.cc``). ``libexec/`` is where the
    binaries already live, so the relative path is the same one an
    unwrapped binary would use. A loader under ``lib/openxc7/`` would
    look for ``lib/share/``. ``libexec/`` is not a directory apio puts
    on PATH (apio#1116).
    """
    lib = PRIVATE_LIB
    return (
        f'exec "$release_topdir_abs"/{LINUX_LOADER} '
        f"--inhibit-cache "
        f'--inhibit-rpath "" '
        f'--library-path "$release_topdir_abs"/{lib} '
        f'{program} "$@"\n'
    )


def place_linux_loader():
    """Move the bundled loader next to the binaries.

    The closure copier drops it in ``PRIVATE_LIB`` with every other
    ``DT_NEEDED``. It has to be exec'd from ``libexec/`` (see
    ``linux_loader_exec``). One copy: a second one in ``PRIVATE_LIB``
    would be the file a wrapper must not exec.
    """
    if IS_DARWIN:
        return
    src = Path.cwd() / DIST / PRIVATE_LIB / LOADER_NAME
    dst = Path.cwd() / DIST / LINUX_LOADER
    if not src.is_file():
        raise SystemExit(
            f"❌ Linux loader was not copied to {PRIVATE_LIB}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    shutil.move(src, dst)
    print(f"➡️  Dep: ✅{LINUX_LOADER}")


def render_tabbypy3(template: str) -> str:
    """Fill the ``store/tabbypy3`` marker with ``linux_loader_exec``."""
    marker = "@LINUX_LOADER_EXEC@"
    if marker not in template:
        raise SystemExit(f"❌ store/tabbypy3 has no {marker} marker")
    program = f'"$release_topdir_abs"/libexec/{PYTHON_NAME}'
    rendered = template.replace(marker, linux_loader_exec(program).rstrip("\n"))
    if marker in rendered:
        raise SystemExit(f"❌ store/tabbypy3 still contains {marker}")
    return rendered + ("" if rendered.endswith("\n") else "\n")


# ------------------------------------------------------------------
# -- Get the dynamic libraries the given executable file
# -- depends on
# --
# -- INPUT:
# --   * binary: Name of the executable file
# --
# -- OUTPUT:
# --   * A dictionary with the libraries and their paths
# ------------------------------------------------------------------
def get_dependencies(binary: str) -> dict:

    # -- Get the path of the binary file
    binary_path = shutil.which(binary)

    # -- Get its dependencies (running the ldd command)
    # -- The raw text output is obtained
    deps_raw = subprocess.run(["ldd", str(binary_path)],
                              capture_output=True, text=True, check=True)

    # -- Dictionary to store the dependencies
    deps = {}

    # -- Walk the output, line by line
    for line in deps_raw.stdout.splitlines():
        line = line.strip()

        # Look for the pattern: libname.so.X => /path/to/libname.so.X (0x0000...)
        match = re.search(r'(\S+)\s+=>\s+(\S+)', line)
        if match:
            # -- Store the library and its path in the dictionary
            lib_name = match.group(1)
            lib_path = match.group(2)

            # Nix may report the loader with its full path as the name.
            if "ld-linux" in lib_name:
                lib_name = Path(lib_name).name
            deps[lib_name] = lib_path

        # Special case: the dynamic loader (e.g. /lib64/ld-linux-x86-64.so.2)
        # usually appears at the end without the '=>' symbol
        elif "ld-linux" in line or "ld.so" in line:
            match_ld = re.search(r'(/[^ ]+)', line)
            if match_ld:
                ld_path = match_ld.group(1)
                ld_name = ld_path.split("/")[-1]
                deps[ld_name] = ld_path

        # Special case: linux-vdso.so.1 (has no physical path)
        elif "linux-vdso" in line:
            match_vdso = re.search(r'(\S+)', line)
            if match_vdso:
                deps[match_vdso.group(1)] = ""

    # -- Return the dictionary
    return deps


# ------------------------------------------------
# -- Copy only the given executable file,
# -- without its dependencies
# ------------------------------------------------
def copy_exec(binary: str, target_dir: str = LIBEXEC):
    # -- Get the path of the executable
    executable_path = Path(str(shutil.which(binary)))

    # -- Copy the executable to the distribution directory
    executable_target_dir = Path.cwd() / DIST / target_dir
    executable_target = executable_target_dir / binary

    # -- Mark indicating the file type
    mark = ""

    # -- If it does not exist, copy it!
    if not executable_target.exists():
        shutil.copy(executable_path, executable_target)
        # -- Mark indicating it has been copied
        mark = "✅"
    else:
        # -- If it exists, print only the name, without copying
        # -- Mark indicating it was already there
        mark = "📌"

    # -- Print name of the executable
    print(f"{ansi.GREEN}  ⚙️  Ejecutable: ",
          end='', flush=True)
    print(f"{ansi.DEFAULT}{mark}{binary}")


# ------------------------------------------------------
# -- Copy the given executable into the distribution
# -- along with ALL its libraries
# ------------------------------------------------------
def copy_with_deps(binary: str):

    # -- On macOS ldd is not used: only the executable is copied, and the
    # -- dylib closure + @rpath relocation is resolved globally at the end
    # -- with macpack.relocate_dist().
    if IS_DARWIN:
        copy_exec(binary)
        return

    # -- Copy the executable first
    copy_exec(binary)

    # -- Read the libraries the executable depends on
    executable_deps = get_dependencies(binary)

    # -- Target directory for the libraries. PRIVATE_LIB, not LIB:
    # -- a file at the first level of lib/ is on apio's PATH (apio#1116).
    libs_target_dir = Path.cwd() / DIST / PRIVATE_LIB
    libs_target_dir.mkdir(parents=True, exist_ok=True)

    # -- Mark indicating whether the file has been copied (✅)
    # -- or it was not necessary because it was already there (📌)
    mark = ""

    # -- Copy all the dependencies of yosys
    for lib_name, libs_path in executable_deps.items():

        if libs_path != "":
            # -- Full path of the file at the target
            lib_target = libs_target_dir / Path(libs_path).name

            # -- Copy the library if it does not exist yet...
            if not lib_target.exists():
                shutil.copy(libs_path, libs_target_dir)
                # -- Mark indicating it did not exist
                mark = "✅"
            # -- It already exists. Do not copy, just report
            else:
                # -- Mark indicating it already exists
                mark = "📌"

            # -- Print name of the library
            print(f"{ansi.BLUE}  🧾 Lib: ",
                  end='', flush=True)
            print(f"{ansi.DEFAULT}{mark}{lib_name}")


# -----------------------------------------------------------
# -- Copy all the python dependencies
# --
# -- store/tabbypy3 --> dist/libexec
# -- nix-python/bin/python3.12 --> dist/libexec
# -- nix-python/lib/python3.12/* --> dist/lib/python3.12/
# -----------------------------------------------------------
def copy_python():

    # --- Copy the wrapper (tabbypy3)
    # -- Linux only: tabbypy3 invokes the packaged ELF loader. On
    # -- macOS the python wrapper runs the bundled interpreter directly.
    # -- libexec/, not bin/. apio scans the first-level files of bin/
    # -- (apio#1116) and this name collides with oss-cad-suite. The
    # -- python wrappers call it by an absolute path.
    if not IS_DARWIN:
        src = Path.cwd() / "store" / "tabbypy3"
        dst = Path.cwd() / DIST / LIBEXEC / "tabbypy3"
        # -- Always rewrite: the loader line is filled from PRIVATE_LIB.
        # -- The packer reuses dist/, so a tabbypy3 left in bin/ would
        # -- still be the name apio scans.
        stale = Path.cwd() / DIST / BIN / "tabbypy3"
        if stale.is_file() or stale.is_symlink():
            stale.unlink()
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(render_tabbypy3(src.read_text(encoding="utf-8")),
                       encoding="utf-8")
        dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        print(f"➡️  Dep: ✅libexec/tabbypy3")

    # -- Copy the python executable
    src = Path(str(shutil.which(PYTHON_NAME)))
    dst = Path.cwd() / DIST / LIBEXEC / PYTHON_NAME
    if dst.exists():
        mark = "📌"
    else:
        shutil.copy(src, dst)
        mark = "✅"
    print(f"➡️  Dep: {mark}libexec/{src.name}")

    # -- Copy the whole python directory
    src = python_stdlib(src)
    dst = Path.cwd() / DIST / LIB / PYTHON_NAME
    if dst.exists():
        mark = "📌"
    else:
        shutil.copytree(src, dst, dirs_exist_ok=True)
        mark = "✅"
    write_access(dst)
    print(f"➡️  Dep: {mark}lib/{src.name}/")

    # The interpreter's own shared libraries. tabbypy3 runs it through
    # the bundled loader with a library path of only PRIVATE_LIB.
    # nextpnr used to bring libpython along because it linked it; the
    # package builds nextpnr without an embedded interpreter, so the
    # closure has to be copied with the interpreter or fasm2frames
    # cannot start.
    if not IS_DARWIN:
        libs_target_dir = Path.cwd() / DIST / PRIVATE_LIB
        libs_target_dir.mkdir(parents=True, exist_ok=True)
        for lib_name, libs_path in get_dependencies(PYTHON_NAME).items():
            if libs_path == "":
                continue
            lib_target = libs_target_dir / Path(libs_path).name
            if lib_target.exists():
                mark = "📌"
            else:
                shutil.copy(libs_path, libs_target_dir)
                mark = "✅"
            print(f"➡️  Dep: {mark}{PRIVATE_LIB}/{lib_target.name} ({lib_name})")


# ------------------------------------------------------------------
# -- Locate the python package the devShell interpreter imports
# --
# -- The devShell's PYTHONPATH points at the site-packages of every
# -- python derivation in the shell's closure, so what THIS interpreter
# -- resolves is by construction what the shell linked. The store glob
# -- this replaces answered with a property of the build HOST's store:
# -- with more than one version of a package present, its first match
# -- could be (and was) a stranger.
# --
# -- Returns the package directory (the file, for a single-file
# -- module). E.g. resolve_python_package("textx") ->
# -- /nix/store/...-python3.12-textx-4.0.1/lib/python3.12/site-packages/textx
# ------------------------------------------------------------------
def resolve_python_package(modname: str) -> Path:

    spec = importlib.util.find_spec(modname)

    if spec is None:
        raise SystemExit(
            f"❌ {modname}: no lo encuentra el intérprete "
            "(¿se empaqueta fuera de `nix develop .#pack`?)")

    # -- Regular/namespace package: the package directory
    if spec.submodule_search_locations:
        return Path(spec.submodule_search_locations[0])

    # -- Single-file module
    return Path(spec.origin)


# ------------------------------------------------------------------
# -- Runtime dependencies of a native object, as the loader resolves
# -- them: {dependency name: file the loader would open}.
# --
# -- Linux: the DT_NEEDED entries resolved through the object's RUNPATH
# -- (ldd). Darwin: the absolute LC_LOAD_DYLIB entries keyed by basename
# -- (otool -L via macpack, which already filters out the system and
# -- @rpath references). Entries the loader cannot resolve ("not
# -- found") are left out.
# ------------------------------------------------------------------
def needed_deps(obj: Path) -> dict:

    if IS_DARWIN:
        import macpack
        return {Path(dep).name: Path(dep)
                for dep in macpack._otool_deps(obj)}

    out = subprocess.run(["ldd", str(obj)],
                         capture_output=True, text=True, check=True).stdout

    deps = {}
    for line in out.splitlines():
        match = re.search(r'(\S+)\s+=>\s+(\S+)', line.strip())
        if match and match.group(2).startswith("/"):
            deps[match.group(1)] = Path(match.group(2))

    return deps


# ------------------------------------------------------------------
# -- Resolve one runtime dependency of a native object to the file the
# -- loader would actually open for it (its RUNPATH / install names
# -- decide, so the answer is the copy this object was linked against).
# --
# -- `pattern` is a regex FULLY matched against the dependency name
# -- (the soname on Linux, the dylib basename on Darwin).
# --
# -- Use this instead of globbing /nix/store whenever the library is
# -- loaded at RUNTIME and therefore has to be copied by hand: the glob
# -- answers with a property of the build HOST's store, this answers
# -- with a property of the thing that needs the library.
# ------------------------------------------------------------------
def resolve_needed(obj: Path, pattern: str) -> Path:

    for name, path in needed_deps(obj).items():
        if re.fullmatch(pattern, name):
            return path

    raise SystemExit(
        f"❌ {pattern}: no aparece entre las dependencias de {obj.name} "
        "(¿cambió como se enlaza?)")


# ------------------------------------------------------------------
# -- Like resolve_needed, but trying each object in turn: which file
# -- carries a dependency can differ by platform (the fasm parser's
# -- libantlr4-runtime is linked by libparse_fasm, next to the cython
# -- extension that dlopens it).
# ------------------------------------------------------------------
def resolve_needed_in(objects: list, pattern: str) -> Path:

    for obj in objects:
        for name, path in needed_deps(obj).items():
            if re.fullmatch(pattern, name):
                return path

    names = ", ".join(obj.name for obj in objects)
    raise SystemExit(
        f"❌ {pattern}: no aparece entre las dependencias de {names} "
        "(¿cambió como se enlaza?)")


# ------------------------------------------------------------------
# -- The _ctypes extension of the python3.12 the package ships (the
# -- interpreter copy_python() bundles): the libffi it loads is the
# -- libffi the bundled interpreter needs at runtime.
# --
# -- The name filter is "_ctypes." with the dot: lib-dynload also
# -- carries _ctypes_test, which is NOT the ctypes runtime.
# ------------------------------------------------------------------
def python_ctypes() -> Path:

    python = Path(str(shutil.which(PYTHON_NAME)))
    dynload = python_stdlib(python) / "lib-dynload"

    objects = sorted(p for p in dynload.glob("_ctypes*.so")
                     if p.name.startswith("_ctypes."))
    if not objects:
        raise SystemExit(f"❌ _ctypes: ningun _ctypes.*.so en {dynload}")

    return objects[0]


# -----------------------------------------------------------------------
# -- Copy a python package into the distribution
# --
# -- The package copied is the one the devShell interpreter imports
# -- (resolve_python_package), never a /nix/store glob match. It lands in
# -- dist/lib/python3.12/site-packages
# -----------------------------------------------------------------------
def copy_python_dep(modname: str):

    # -- The package/module the devShell interpreter resolves
    src = resolve_python_package(modname)

    # -- Target directory
    dst_site_pack = Path.cwd() / DIST / LIB / PYTHON_NAME / "site-packages"
    dst = dst_site_pack / src.name

    # -- Give write permissions to the "site-packages" directory
    # -- of the distribution
    if dst_site_pack.exists():
        write_access(dst_site_pack)

    mark = ""

    if dst.exists():
        mark = "📌"
    else:
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            dst_site_pack.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        mark = "✅"

    print(f"➡️  Dep: {mark}{modname} <- {src}")


# ------------------------------------
# -- Run the command "file -b <path>"
# -- The processed, lowercased string
# -- is returned
# -------------------------------------
def cmd_file(path: Path) -> str:

    # -- Run "file -b <path>"
    result = subprocess.run(
        ['file', '-b', path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True
    )

    # -- Get the raw output
    output_cmd = result.stdout.strip()

    # -- Lowercase it
    output_cmd = output_cmd.lower()

    # -- Return result
    return output_cmd


# ----------------------------------------------------
# -- Check whether the file is an ELF executable
# -- Done by calling the "file" command
# ----------------------------------------------------
def is_elf(path: Path) -> bool:

    # -- Run the "file -b <path>" command
    # -- to learn the file type
    output = cmd_file(path)

    # -- On macOS the native executable is Mach-O (not ELF)
    if IS_DARWIN:
        return ("mach-o" in output) and ("executable" in output)

    # -- Detect the "elf" pattern
    return "elf " in output


# -----------------------------------------------------
# -- Check whether it is a PYTHON program
# -----------------------------------------------------
def is_python_script(path: Path) -> bool:

    # -- Run the "file -b <path>" command
    # -- to learn the file type
    output = cmd_file(path)

    # -- Detect whether it is a python script
    return "python script" in output


# -----------------------------------------------------
# -- Check whether it is a shell script
# -----------------------------------------------------
def is_shell_script(path: Path) -> bool:

    # -- Run the "file -b <path>" command
    # -- to learn the file type
    output = cmd_file(path)

    # -- Detect whether it is a shell script
    return ("bash script" in output) or ("bash -e script" in output)


# -----------------------------------------
# --  Add a shebang to a python file
# -----------------------------------------
def python_shebang_add(file_path: Path):

    try:
        # -- Read python file
        contents = file_path.read_text(encoding="utf-8")

        # -- Shebang to add
        shebang = "#!/usr/bin/env python3\n"

        # -- Add shebang!
        contents = shebang + contents

        # -- Write new contents
        file_path.write_text(contents, encoding="utf-8")
        # print(f"✔️ Shebang añadido con éxito a: {file_path}")

    except PermissionError:
        print(f"❌ Error: Sin permisos '{file_path}'.")
    except Exception as e:
        print(f"❌ Ocurrió un error inesperado: {e}")


# -----------------------------------------
# -- Add a shebang to a bash file
# -----------------------------------------
def bash_shebang_add(file_path: Path):

    try:
        # -- Read bash file
        contents = file_path.read_text(encoding="utf-8")

        # -- Shebang to add
        shebang = "#!/usr/bin/env bash\n"

        # -- Add shebang!
        contents = shebang + contents

        # -- Write new contents
        file_path.write_text(contents, encoding="utf-8")
        # print(f"✔️ Shebang añadido con éxito a: {file_path}")

    except PermissionError:
        print(f"❌ Error: Sin permisos '{file_path}'.")
    except Exception as e:
        print(f"❌ Ocurrió un error inesperado: {e}")


# -----------------------------------------
# -- Give write permissions to the file
# -----------------------------------------
def write_access(file_path: Path):

    try:
        # Get the permissions
        permissions = file_path.stat().st_mode

        # Enable write permission
        permissions = permissions | stat.S_IWUSR

        # Apply the changes
        file_path.chmod(permissions)
        # print(f"✔️ Permiso de escritura añadido a: {file_path}")

    except PermissionError:
        print("❌ Error: No tienes permiso")
