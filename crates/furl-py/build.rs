// Compile a tiny C shim that provides local definitions of glibc
// symbols introduced after the manylinux_2_28 floor that static
// dependencies compiled with a newer toolchain may reference:
//
//   - C23 strtol family (`__isoc23_strtol`, `__isoc23_strtoll`, ...)
//     introduced in glibc 2.38 — see `glibc_compat.c` Section A.
//   - `__libc_single_threaded` introduced in glibc 2.32 — see
//     `glibc_compat.c` Section B (caught by X1 smoke gate on PR
//     #396 X2 dry-run).
//
// HISTORICAL ORIGIN + CURRENT STATUS: the references originally came
// from the prebuilt ONNX Runtime static archives (compiled with gcc
// 14.x). ORT and the whole ML plane have since been EXCISED — the core
// is ML-free and every remaining Rust dependency builds from source
// with the manylinux toolchain — so the shim is most likely inert now.
// It is kept defensively because the failure class it guards (any
// gcc-14-built static archive entering the link set referencing
// post-2.28 glibc symbols) breaks `import` on older-glibc user machines
// at RUNTIME, invisible to the build host. Delete only after an `nm`
// symbol audit of a fresh Linux wheel shows no UND `__isoc23_*` /
// `__libc_single_threaded` references (macOS boxes cannot run this
// audit — it needs the Linux release artifact).
//
// The shim is Linux/glibc-only — macOS, Windows, and musl don't ship
// glibc and don't reference any of these symbols.
//
// Symbols shimmed:
//   - the `__isoc23_*` family
//   - the `__libc_single_threaded` symbol

fn main() {
    println!("cargo:rerun-if-changed=glibc_compat.c");
    println!("cargo:rerun-if-changed=build.rs");

    // The shim is glibc-specific. Skip on every other target: macOS
    // uses Darwin libc, Windows has MSVCRT, musl handles strtoll
    // identically and never emits __isoc23_* / __libc_single_threaded.
    let target_os = std::env::var("CARGO_CFG_TARGET_OS").unwrap_or_default();
    let target_env = std::env::var("CARGO_CFG_TARGET_ENV").unwrap_or_default();
    if target_os != "linux" || target_env != "gnu" {
        return;
    }

    cc::Build::new()
        .file("glibc_compat.c")
        // -fPIC because we link into a cdylib. -O2 for size — the
        // file is ~10 lines but every byte counts in a wheel that's
        // already 35 MiB.
        .flag_if_supported("-fPIC")
        .opt_level(2)
        .compile("furl_glibc_compat");

    // Force the linker to pull our shim's objects into _core.so even
    // if at archive-scan time no UND `__isoc23_*` reference exists
    // yet. Historical example (ORT era, PR #386's release run): the
    // prebuilt ORT static archives linked AFTER our shim's archive on
    // aarch64, leaving `__isoc23_*` unresolved at the .so level even
    // though our archive defined them — the audit then rightly
    // rejected the wheel. ORT is excised now, but the archive-order
    // hazard is generic to any late-linking static archive.
    //
    // `-u <sym>` (a.k.a. `--undefined`) tells the linker: "treat
    // this symbol as undefined at the start of linking, which forces
    // any archive defining it to be scanned and its members pulled
    // in." Once our archive's objects are in, the shim's strong
    // definitions are present in `_core.so` and any later archive's
    // references resolve to them regardless of scan order.
    for sym in [
        "__isoc23_strtol",
        "__isoc23_strtoll",
        "__isoc23_strtoul",
        "__isoc23_strtoull",
        // glibc 2.32+ — see glibc_compat.c Section B. Force-undefined
        // here for the same reason as the __isoc23_* family: archives
        // that DEFINE the symbol must be scanned before archives that
        // REFERENCE it, otherwise our shim's archive is dropped and
        // the .so ships with a UND `__libc_single_threaded` that
        // breaks import on glibc < 2.32.
        "__libc_single_threaded",
    ] {
        println!("cargo:rustc-link-arg=-Wl,-u,{sym}");
    }
}
